/* vqa-evaluation-report · 图表脚本（ECharts + Mermaid 初始化） */
(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var warn = style.getPropertyValue('--warn').trim();

  var charts = [];
  function make(id, option) {
    var el = document.getElementById(id);
    if (!el || typeof echarts === 'undefined') return;
    var c = echarts.init(el, null, { renderer: 'svg' });
    c.setOption(option);
    charts.push(c);
  }

  /* ---------- 图 A · 分层指标雷达 ---------- */
  make('chart-radar', {
    animation: false,
    tooltip: { appendToBody: true },
    radar: {
      indicator: [
        { name: '检索排序', max: 100 },
        { name: '防误放行', max: 100 },
        { name: '防漏召回', max: 100 },
        { name: '生成一致性', max: 100 },
        { name: '敏感拦截', max: 100 },
        { name: '回归健壮性', max: 100 }
      ],
      radius: '62%',
      center: ['50%', '52%'],
      axisName: { color: muted, fontSize: 12 },
      splitLine: { lineStyle: { color: rule } },
      splitArea: { show: false },
      axisLine: { lineStyle: { color: rule } }
    },
    series: [{
      type: 'radar',
      symbolSize: 5,
      data: [{
        value: [100, 100, 91.7, 100, 100, 100],
        name: '本次评估得分',
        lineStyle: { color: accent, width: 2 },
        itemStyle: { color: accent },
        areaStyle: { color: accent, opacity: 0.18 },
        label: { show: true, color: ink, fontSize: 11, formatter: '{c}' }
      }]
    }]
  });

  /* ---------- 图 B · 双路检索对比 ---------- */
  make('chart-dual', {
    animation: false,
    tooltip: { appendToBody: true, formatter: function (p) { return p.name + '：hit@1 = ' + p.value + '%'; } },
    grid: { top: 30, bottom: 40, left: 50, right: 20 },
    xAxis: {
      type: 'category',
      data: ['仅 FTS（BM25）', '仅向量路', 'RRF 融合（现行）'],
      axisLabel: { color: ink, fontSize: 12, interval: 0 },
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false }
    },
    yAxis: {
      type: 'value', max: 100, min: 0,
      axisLabel: { color: muted, formatter: '{value}%' },
      splitLine: { lineStyle: { color: rule } }
    },
    series: [{
      type: 'bar',
      barWidth: '46%',
      data: [
        { value: 83.3, itemStyle: { color: muted } },
        { value: 100, itemStyle: { color: accent2 } },
        { value: 100, itemStyle: { color: accent } }
      ],
      label: { show: true, position: 'top', color: ink, fontWeight: 700, fontFamily: 'Outfit, sans-serif', formatter: '{c}%' }
    }]
  });

  /* ---------- 图 C · 16 问 Top1 相似度 vs T1 ---------- */
  var SIM_Q = {
    a1: '申请低保需要准备哪些材料', a2: '低保标准每个月是多少钱', a3: '耕地地力保护补贴怎么申领',
    a4: '种粮补贴什么时候发放', a5: '村里的财务收支在哪里可以查到', a6: '宅基地申请流程是什么',
    a7: '危房改造每户能补多少钱', a8: '危房改造申请到批复要多长时间', a9: '医保今年要交多少钱',
    a10: '医保异地就医怎么报销', a11: '低保多长时间核查一次', a12: '危房改造哪些人可以申请',
    b1: '明年居民养老金涨多少', b2: '村里路灯不亮了找谁反映', b3: '农村养老保险怎么缴费', b4: '孩子上学学区怎么划分'
  };
  var SIM_G = {
    a1: '向量达标放行', a2: '向量达标放行', a3: '向量达标放行', a4: '向量达标放行',
    a5: '向量达标放行', a6: '向量达标放行',
    a7: 'FTS 旁证放行', a8: 'FTS 旁证放行', a10: 'FTS 旁证放行', a11: 'FTS 旁证放行', a12: 'FTS 旁证放行',
    a9: '漏召回 FN', b1: '正确拒答', b2: '正确拒答', b3: '正确拒答', b4: '正确拒答'
  };
  var SIM_V = [0.8761, 0.617, 0.9104, 0.5948, 0.6756, 0.8434, 0.4004, 0.3944, 0.5389, 0.5282, 0.5363, 0.3652, 0.3301, 0.4336, 0.4509, 0.313];
  var SIM_IDS = ['a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7', 'a8', 'a9', 'a10', 'a11', 'a12', 'b1', 'b2', 'b3', 'b4'];
  var simData = SIM_IDS.map(function (id, i) {
    var g = SIM_G[id];
    var color = g === '向量达标放行' ? accent : (g === 'FTS 旁证放行' ? accent2 : (g === '漏召回 FN' ? warn : muted));
    return { value: SIM_V[i], itemStyle: { color: color, opacity: g === '正确拒答' ? 0.55 : 1 } };
  });
  make('chart-sim', {
    animation: false,
    tooltip: {
      appendToBody: true,
      formatter: function (p) {
        var id = SIM_IDS[p.dataIndex];
        return id + ' · ' + SIM_Q[id] + '<br/>Top1 相似度：' + SIM_V[p.dataIndex] + '<br/>归类：' + SIM_G[id];
      }
    },
    grid: { top: 36, bottom: 30, left: 50, right: 16 },
    xAxis: {
      type: 'category', data: SIM_IDS,
      axisLabel: { color: muted, fontSize: 11, interval: 0, fontFamily: 'JetBrainsMono, monospace' },
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false }
    },
    yAxis: {
      type: 'value', min: 0, max: 1,
      axisLabel: { color: muted, formatter: function (v) { return v.toFixed(2); } },
      splitLine: { lineStyle: { color: rule } }
    },
    series: [{
      type: 'bar',
      barWidth: '62%',
      data: simData,
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { color: ink, type: 'dashed', width: 1.5 },
        label: { color: ink, fontSize: 11, formatter: 'T1 = 0.55', position: 'insideEndTop' },
        data: [{ yAxis: 0.55 }]
      }
    }]
  });

  /* ---------- 图 D · T1 混淆矩阵 ---------- */
  var CONF_NAME = { '0,1': 'TP', '1,1': 'FN', '0,0': 'FP', '1,0': 'TN' };
  make('chart-confusion', {
    animation: false,
    tooltip: {
      appendToBody: true,
      formatter: function (p) {
        return CONF_NAME[p.value[0] + ',' + p.value[1]] + ' = ' + p.value[2] + ' 问';
      }
    },
    grid: { top: 24, bottom: 36, left: 96, right: 24 },
    xAxis: {
      type: 'category', data: ['判定：放行', '判定：拒答'],
      axisLabel: { color: ink, fontSize: 12 },
      axisLine: { show: false }, axisTick: { show: false }, splitArea: { show: false }
    },
    yAxis: {
      type: 'category', data: ['应未命中 ×4', '应命中 ×12'],
      axisLabel: { color: ink, fontSize: 12 },
      axisLine: { show: false }, axisTick: { show: false }, splitArea: { show: false }
    },
    visualMap: {
      show: false, min: 0, max: 12,
      inRange: { color: [bg2, accent] },
      outOfRange: { color: 'transparent' }
    },
    series: [{
      type: 'heatmap',
      splitArea: { show: false },
      data: [[0, 1, 11], [1, 1, 1], [0, 0, 0], [1, 0, 4]],
      label: {
        show: true,
        fontFamily: 'Outfit, sans-serif',
        fontSize: 18,
        fontWeight: 700,
        color: ink,
        formatter: function (p) { return CONF_NAME[p.value[0] + ',' + p.value[1]] + '\n' + p.value[2]; }
      },
      itemStyle: { borderColor: bg2, borderWidth: 3, borderRadius: 4 },
      emphasis: { itemStyle: { shadowBlur: 6 } }
    }]
  });

  /* ---------- 图 E · 阈值敏感度 ---------- */
  make('chart-threshold', {
    animation: false,
    tooltip: { appendToBody: true },
    legend: { top: 0, textStyle: { color: muted, fontSize: 12 } },
    grid: { top: 40, bottom: 32, left: 44, right: 16 },
    xAxis: {
      type: 'category', data: ['T1 = 0.45', 'T1 = 0.53', 'T1 = 0.55 · 现行'],
      axisLabel: { color: ink, fontSize: 11, interval: 0 },
      axisLine: { lineStyle: { color: rule } }, axisTick: { show: false }
    },
    yAxis: {
      type: 'value', min: 0, max: 12, interval: 3,
      axisLabel: { color: muted },
      splitLine: { lineStyle: { color: rule } }
    },
    series: [
      {
        name: 'TP（应命中·放行）', type: 'bar', barWidth: '30%',
        data: [12, 12, 11], itemStyle: { color: accent },
        label: { show: true, position: 'top', color: ink, fontWeight: 700 }
      },
      {
        name: 'FP（应拒答·误放行）', type: 'bar', barWidth: '30%',
        data: [1, 0, 0], itemStyle: { color: warn },
        label: { show: true, position: 'top', color: warn, fontWeight: 700 }
      }
    ]
  });

  /* ---------- 图 F · 三态分布 ---------- */
  make('chart-tristate', {
    animation: false,
    tooltip: { appendToBody: true, formatter: '{b}：{c} 问（{d}%）' },
    title: {
      text: '11/12',
      subtext: '三态一致率 91.7%',
      left: 'center', top: '36%',
      textStyle: { color: ink, fontSize: 26, fontWeight: 700, fontFamily: 'Outfit, sans-serif' },
      subtextStyle: { color: muted, fontSize: 12 }
    },
    legend: { bottom: 0, textStyle: { color: muted, fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['48%', '68%'],
      center: ['50%', '46%'],
      itemStyle: { borderColor: bg2, borderWidth: 3 },
      label: { color: ink, fontSize: 12, formatter: '{b}\n{c} 问' },
      labelLine: { lineStyle: { color: rule } },
      data: [
        { value: 4, name: 'answered 已答', itemStyle: { color: accent } },
        { value: 4, name: 'missed 未命中', itemStyle: { color: accent2 } },
        { value: 4, name: 'out_of_scope 敏感拦截', itemStyle: { color: muted } }
      ]
    }]
  });

  /* ---------- 统一 resize ---------- */
  window.addEventListener('resize', function () {
    charts.forEach(function (c) { c.resize(); });
  });

  /* ---------- Mermaid 初始化 ---------- */
  if (typeof mermaid !== 'undefined') {
    mermaid.initialize({
      startOnLoad: true,
      theme: 'base',
      securityLevel: 'loose',
      themeVariables: {
        primaryColor: '#e8f2ee',
        primaryBorderColor: accent,
        primaryTextColor: ink,
        lineColor: muted,
        fontSize: '14px'
      }
    });
  }
})();
