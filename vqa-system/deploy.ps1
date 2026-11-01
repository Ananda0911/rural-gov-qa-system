﻿﻿﻿<#
.SYNOPSIS
  乡村智能问答系统 一键部署脚本（Windows，PowerShell 5.1+）
.DESCRIPTION
  用法（在项目根目录执行）：
    .\deploy.ps1 -Check        仅环境自检（只读，不改任何东西）
    .\deploy.ps1 -Install      创建 venv 并安装依赖
    .\deploy.ps1 -Init         初始化数据库与首个管理员（幂等，可重复执行）
    .\deploy.ps1 -SetupBoot    注册开机自启计划任务（需管理员权限）
    .\deploy.ps1 -Start        启动服务并验证可访问
    .\deploy.ps1 -Stop          停止服务
    .\deploy.ps1 -All           全流程：Check → Install → Init → SetupBoot → Start
#>
param(
  [switch]$Check, [switch]$Install, [switch]$Init,
  [switch]$SetupBoot, [switch]$Start, [switch]$Stop, [switch]$All
)
$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$Py = Join-Path $Dir "venv\Scripts\python.exe"
$TaskName = "vqa-system"
$BatFile = Join-Path $Dir "start.bat"
$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"

function Ok($m)   { Write-Host "  [OK]  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Step($m) { Write-Host "`n===== $m =====" -ForegroundColor Cyan }

function Get-EnvValue([string]$key, [string]$default) {
  $envFile = Join-Path $Dir ".env"
  if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
      if ($line -match ("^\s*" + $key + "\s*=\s*(.*)$")) { return $Matches[1].Trim() }
    }
  }
  return $default
}

function Test-PyVersion([string]$ver) {
  try {
    $p = $ver.Split('.')
    return ([int]$p[0] -gt 3) -or (([int]$p[0] -eq 3) -and ([int]$p[1] -ge 11))
  } catch { return $false }
}

function Get-SysPython {
  $pyl = Get-Command py -ErrorAction SilentlyContinue
  if ($pyl) {
    $v = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if (($LASTEXITCODE -eq 0) -and (Test-PyVersion "$v")) { return @{ Exe = "py"; Args = @("-3") } }
  }
  $pc = Get-Command python -ErrorAction SilentlyContinue
  if ($pc) {
    $v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if (($LASTEXITCODE -eq 0) -and (Test-PyVersion "$v")) { return @{ Exe = "python"; Args = @() } }
  }
  return $null
}

# ---------- 各步骤 ----------

function Step-Check {
  Step "1/5 环境自检"
  $issues = 0
  if (Test-Path $Py) {
    Ok "虚拟环境已存在（venv）"
  } else {
    $sys = Get-SysPython
    if ($sys) { Ok "系统 Python 可用（$($sys.Exe) $($sys.Args -join ' ')）" }
    else { Fail "未找到 Python 3.11+，请先安装 Python（勾选 Add to PATH）"; $issues++ }
  }
  $port = Get-EnvValue "PORT" "8000"
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    Warn "端口 $port 已被占用（PID $($conn[0].OwningProcess)）——若是旧服务实例请先停止，避免冲突"
  } else { Ok "端口 $port 空闲" }
  $drive = Get-PSDrive -Name ($Dir.Substring(0, 1)) -ErrorAction SilentlyContinue
  if ($drive -and ($drive.Free -gt 500MB)) { Ok "磁盘剩余 $([math]::Round($drive.Free / 1MB)) MB" }
  else { Warn "磁盘剩余空间不足 500MB" }
  if (-not (Test-Path (Join-Path $Dir ".env"))) {
    if (Test-Path (Join-Path $Dir ".env.example")) {
      Copy-Item (Join-Path $Dir ".env.example") (Join-Path $Dir ".env")
      Warn ".env 不存在，已从 .env.example 生成——请编辑填写 LLM_API_KEY 等真实配置后重新执行"; $issues++
    } else { Fail "缺少 .env 与 .env.example"; $issues++ }
  } else {
    $key = Get-EnvValue "LLM_API_KEY" ""
    if (-not $key) { Warn "LLM_API_KEY 为空：将运行在 mock 演示模式（LLM_MOCK）" }
    else { Ok "LLM_API_KEY 已配置" }
    $mock = Get-EnvValue "LLM_MOCK" "1"
    if ($mock -eq "0") { Ok "LLM_MOCK=0（真实大模型）" } else { Warn "LLM_MOCK=1（mock 演示模式）" }
  }
  $bindHost = Get-EnvValue "HOST" "127.0.0.1"
  if ($bindHost -ne "0.0.0.0") {
    Warn "HOST=$bindHost：仅本机可访问。要让局域网村民/工作人员访问，请在 .env 中设置 HOST=0.0.0.0"
  } else { Ok "HOST=0.0.0.0（局域网可访问）" }
  if ($issues -gt 0) { Fail "自检发现 $issues 个必须处理的问题（见上方 WARN/FAIL）" } else { Ok "自检通过" }
  return ($issues -eq 0)
}

function Step-Install {
  Step "2/5 安装依赖"
  if (-not (Test-Path $Py)) {
    $sys = Get-SysPython
    if (-not $sys) { Fail "未找到 Python 3.11+，无法安装"; exit 1 }
    Write-Host "  创建虚拟环境 venv ..."
    & $sys.Exe @($sys.Args + @("-m", "venv", (Join-Path $Dir "venv")))
    if (-not (Test-Path $Py)) { Fail "venv 创建失败"; exit 1 }
    Ok "venv 创建完成"
  } else { Ok "venv 已存在，跳过创建" }
  Write-Host "  安装依赖（清华镜像，首次约 2-5 分钟）..."
  & $Py -m pip install --upgrade pip -i $PipMirror --quiet
  & $Py -m pip install -r (Join-Path $Dir "requirements.txt") -i $PipMirror --quiet
  if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败，请检查网络后重试"; exit 1 }
  & $Py -c "import fastapi, uvicorn, jieba, sqlalchemy, apscheduler, sqlite_vec, docx, pypdf; print('  [OK] 依赖导入自检通过')"
  if ($LASTEXITCODE -ne 0) { Fail "依赖导入自检失败"; exit 1 }
}

function Step-Init {
  Step "3/5 初始化数据库与管理员"
  Write-Host "  执行 init-db（幂等：已有数据库会自动跳过）..."
  & $Py (Join-Path $Dir "manage.py") init-db
  if ($LASTEXITCODE -ne 0) { Fail "init-db 失败：若报 embedding/网络错误，请先在 .env 配置真实 LLM_API_KEY 后重试"; exit 1 }
  Ok "数据库初始化完成（初始密码仅打印一次，请抄录）"
}

function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Step-SetupBoot {
  Step "4/5 注册开机自启"
  if (-not (Test-Admin)) { Fail "注册计划任务需要管理员权限：请右键 PowerShell 以管理员身份运行后再执行"; exit 1 }
  # 生成启动包装（计划任务在 SYSTEM 会话无控制台，输出重定向到日志文件）
  $bat = @"
@echo off
rem vqa-system startup wrapper (created by deploy.ps1)
cd /d "%~dp0"
if not exist data mkdir data
venv\Scripts\python.exe manage.py serve >> data\serve.log 2>&1
"@
  [IO.File]::WriteAllText($BatFile, $bat, [Text.Encoding]::Default)
  & schtasks /Create /TN $TaskName /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "`"$BatFile`"" /F | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "计划任务注册失败"; exit 1 }
  Ok "计划任务已注册：开机自动启动（任务名 $TaskName）"
  Ok "服务日志：data\serve.log"
}

function Step-StartSvc {
  Step "5/5 启动服务并验证"
  $exists = & schtasks /Query /TN $TaskName 2>$null
  if (-not $exists) { Warn "计划任务未注册，改用当前窗口前台启动（关闭窗口服务即停）"; exit 1 }
  & schtasks /Run /TN $TaskName | Out-Null
  $port = Get-EnvValue "PORT" "8000"
  Write-Host "  等待服务就绪（最长 45 秒）..."
  $deadline = (Get-Date).AddSeconds(45)
  $ready = $false
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 3
      if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Seconds 2 }
  }
  if (-not $ready) {
    Fail "服务启动超时，请查看 data\serve.log 排查"
    Fail "排查后可手动执行：schtasks /Run /TN $TaskName"
    exit 1
  }
  Ok "服务已就绪：http://127.0.0.1:$port （本机访问）"
  $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -ne "127.0.0.1" } | Select-Object -ExpandProperty IPAddress
  if ($ips) {
    $bindHost = Get-EnvValue "HOST" "127.0.0.1"
    if ($bindHost -eq "0.0.0.0") {
      foreach ($ip in $ips) { Ok "局域网访问地址：http://$ip`:$port" }
    } else { Warn "HOST=127.0.0.1，局域网暂不可访问（修改 .env 后重启服务生效）" }
  }
}

function Step-StopSvc {
  Step "停止服务"
  $exists = & schtasks /Query /TN $TaskName 2>$null
  if ($exists) {
    & schtasks /End /TN $TaskName | Out-Null
    Ok "已发送停止指令（计划任务方式启动的服务）"
  } else { Warn "计划任务 $TaskName 不存在（可能以前台方式在运行，直接关闭其窗口即可）" }
}

function Show-NextSteps {
  Write-Host ""
  Write-Host "===== 部署后续清单（上线门槛） =====" -ForegroundColor Cyan
  Write-Host "  1. 管理员首次登录强制改密（初始密码在 init-db 输出中，仅显示一次）"
  Write-Host "  2. 公网访问需在前置 nginx/Caddy 配 HTTPS；纯内网建议保持强密码策略"
  Write-Host "  3. data\backup\ 为每日自动备份（库 + uploads 原件），请纳入服务器整体备份或定期异地拷贝"
  Write-Host "  4. 全量导入正式政策文件（管理端 → 文档导入），抽 20-30 个真实问题做验收"
  Write-Host "  5. 演示条目与测试遗留（未答组）在管理端下线/关闭"
  Write-Host "  6. 换 embedding 服务或维度变更后执行：venv\Scripts\python.exe manage.py reindex"
  Write-Host "  常用命令：启动 schtasks /Run /TN vqa-system | 停止 .\deploy.ps1 -Stop | 手动备份 manage.py backup"
}

# ---------- 主流程 ----------
if ($All) { $Check = $true; $Install = $true; $Init = $true; $SetupBoot = $true; $Start = $true }
$any = $Check -or $Install -or $Init -or $SetupBoot -or $Start -or $Stop
if (-not $any) {
  Write-Host "乡村智能问答系统部署脚本" -ForegroundColor Cyan
  Write-Host "  .\deploy.ps1 -Check       环境自检（只读）"
  Write-Host "  .\deploy.ps1 -Install     安装依赖"
  Write-Host "  .\deploy.ps1 -Init        初始化数据库与管理员"
  Write-Host "  .\deploy.ps1 -SetupBoot   注册开机自启（管理员）"
  Write-Host "  .\deploy.ps1 -Start       启动并验证"
  Write-Host "  .\deploy.ps1 -Stop        停止服务"
  Write-Host "  .\deploy.ps1 -All         全流程一键部署（推荐）"
  exit 0
}
Write-Host "部署目录：$Dir" -ForegroundColor DarkGray
if ($Stop) { Step-StopSvc; exit 0 }
$checkOk = $true
if ($Check) { $checkOk = Step-Check }
if (-not $checkOk) {
  Fail "自检未通过，请先处理上述问题再继续（仅自检可用 -Check 单独执行）"
  if ($Install -or $Init -or $SetupBoot -or $Start) { exit 1 }
}
if ($Install) { Step-Install }
if ($Init) { Step-Init }
if ($SetupBoot) { Step-SetupBoot }
if ($Start) { Step-StartSvc }
Show-NextSteps
Write-Host ""
Ok "deploy.ps1 执行结束"
