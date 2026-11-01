# 乡村政务智能问答系统

面向村务场景的检索增强问答（RAG）系统：村民提问 → 混合检索政务知识库 → 大模型基于条目生成带引用的回答，回答口径供基层工作人员向村民复述。

## 技术亮点

- **八环节问答管线**：校验 → 敏感拦截 → 混合检索 → 相关性判定 → Prompt 组装 → 生成 → 引用校验 → 单事务落库，技术失败重试有明确出口，不留半成品
- **混合检索**：BM25（SQLite FTS5 + jieba）与向量双路各取 Top20，RRF(k=60) 融合 Top5；embedding 不可用自动降级纯关键词
- **向量检索三级后端**：FAISS 内存索引 → sqlite-vec → 纯 Python 余弦，自动降级且结果口径一致
- **三态结果设计**：answered（已答）/ missed（未命中，进入未答聚合清单）/ out_of_scope（敏感拦截），拒答路径与幻觉风险显式建模
- **防幻觉措施**：Prompt 限定仅依据条目作答、sufficient 自判不足即拒答、cited_ids 服务端校验、引用四要素（标题/机构/时间/160 字定位片段）由服务端组装不采信模型复述
- **质量保障**：67 个回归测试全绿；28 问分层效果评估（真实 LLM API）：检索 hit@1 = 100%、MRR = 1.000、三态零误放行、生成事实一致性 4/4（详见《系统效果评估报告》）

## 目录结构

```
├── vqa-system/               # 应用代码
│   ├── app/                  # FastAPI 应用（pipeline 问答管线 / search 检索 / vector_store FAISS…）
│   ├── tests/                # 67 个回归测试（Mock 模式，零外呼）
│   ├── eval/                 # 效果评估脚本与逐问明细（可复现）
│   ├── data/app.db           # 演示数据库（7 条目 / 11 切片 / 向量已就位）
│   ├── requirements.txt
│   ├── manage.py             # init-db / reindex / backup / serve
│   └── start.bat             # Windows 一键启动
├── 测试文件/                  # 知识库入库素材（政务文件 docx 原文）
├── vqa-evaluation-report/    # 效果评估报告（可视化 HTML 版）
├── 乡村智能问答系统-PRD.html          # 产品需求文档
├── 乡村智能问答系统-技术方案.html      # 技术方案设计
└── 乡村智能问答系统-技术清单.md        # 技术选型清单
```

## 快速开始

```bash
cd vqa-system
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# （可选）真实大模型：复制 .env.example 为 .env，填入 OpenAI 兼容服务的
# BASE_URL / API_KEY / 模型名，并将 LLM_MOCK 改为 0
# 不配置 .env 时默认 Mock 模式，可完整体验流程（本地模拟 embedding 与生成）

start.bat          # 或: venv\Scripts\python.exe manage.py serve
# 浏览器访问 http://127.0.0.1:8000
```

> Windows 用户也可双击项目根目录的 **一键启动.bat**：自动启动服务并打开浏览器。
> 首次使用可在登录页点「注册一个」自助创建账号（staff 角色），或使用管理员账号。

> 演示数据库 `data/app.db` 已含真实向量；若切换 embedding 服务商，运行
> `venv\Scripts\python.exe manage.py reindex` 重建向量。

## 运行测试与评估

```bash
cd vqa-system
venv\Scripts\python.exe -m pytest tests -q        # 67 个回归测试（无需 API Key）
venv\Scripts\python.exe eval\l1_retrieval.py      # 检索层评估（消耗少量 embedding 配额）
venv\Scripts\python.exe eval\l2_end2end.py        # 端到端评估（消耗少量 LLM 配额）
```

## 文档索引

| 文档 | 内容 |
|---|---|
| 乡村智能问答系统-PRD.html | 产品定位、用户故事、功能范围 |
| 乡村智能问答系统-技术方案.html | 架构设计、八环节管线、三态出口、阈值体系 |
| 乡村智能问答系统-技术清单.md | 技术选型与理由（国产模型、轻量零依赖） |
| 系统效果评估报告.md / vqa-evaluation-report/ | 分层效果评估：指标、混淆矩阵、问题与改进建议 |
