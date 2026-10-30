/* ============================================================
   mock.js — 演示态后端（浏览器直接打开时生效）
   仅在 window.pywebview 不存在时安装；真实 pywebview 环境下本文件完全不生效。
   对外暴露：
     window.AVT.isDemo      布尔
     window.AVT.demo.set()  切换「运行中 / 已完成」
     window.AVT.demo.mode
   接口与真实后端保持一致的 14 个方法。
   ============================================================ */
(function () {
  'use strict';

  /* 真实环境：只要 pywebview 对象已存在就完全退出，不覆盖、不注入 */
  if (window.pywebview) return;

  var AVT = window.AVT = window.AVT || {};
  AVT.isDemo = true;

  /* ─────────── 演示数据 ─────────── */

  var CAR_TOTAL = 128, CAR_STEP = 320;   // 每张影像推进 320ms
  var DOM_TOTAL = 48,  DOM_STEP = 520;   // 每个分块推进 520ms
  var CMP_STEP  = 380;                   // 每条日志推进 380ms

  var SESSIONS = ['BATCH_0412', 'BATCH_0413', 'BATCH_0415'];
  var CATS = ['动态障碍', '交通标志', '信号灯', '路侧设施'];
  var FAIL_IDX = { 37: 1, 88: 1 };       // 两张失败影像

  var LOGS_CAR = [
    '[09:31:02] 开始提取任务 · 数据源 D:\\采集车\\20250412',
    '[09:31:02] 扫描到 3 个批次，影像 128 张',
    '[09:31:03] 位姿文件 128 条，配对成功 128 张',
    '[09:31:03] 加载检测模型 YOLOv8s-seg（权重 21.8 MB）',
    '[09:31:05] 模型加载完成，置信度阈值 0.15',
    '[09:31:05] 开始处理批次 BATCH_0412（48 张）',
    '[09:31:12] BATCH_0412 要素写入 SHP：动态障碍 61 · 交通标志 44',
    '[09:31:12] BATCH_0412 完成，车道线 128 条',
    'WARN 跳过无位姿影像 frame_000137.jpg',
    '[09:31:40] 开始处理批次 BATCH_0413（44 张）',
    'ERR 读取位姿失败：frame_000512.json 格式异常',
    '[09:32:02] 已跳过 1 张异常影像，继续处理',
    '[09:32:20] BATCH_0413 完成，车道线 96 条',
    '[09:32:20] 开始处理批次 BATCH_0415（36 张）',
    '[09:32:48] 图层 LaneLine 写入完成',
    '[09:33:10] 输出目录：D:\\众源解译\\输出\\车载提取_20250412_0931',
    '[09:33:10] 提取完成，成功 126 张，失败 2 张',
    '[09:33:10] 结果已写入 SHP，标注预览图 128 张'
  ];

  var LOGS_DOM = [
    '[10:02:11] 打开正射影像 D:\\DOM\\城南片区_0.05m.tif',
    '[10:02:12] 影像尺寸 24000 × 18000，分辨率 0.05 m',
    '[10:02:13] 加载存量 GDB：D:\\存量\\城南存量.gdb',
    '[10:02:14] 存量范围裁剪已启用',
    '[10:02:14] 道路走廊约束 ±8m 已启用，建筑不提取',
    '[10:02:15] 分块切分：48 块（1024 × 1024）',
    '[10:02:20] 分块推理开始，共 48 块',
    '[10:03:02] 分块 24/48 完成，累计标线 412 条',
    'WARN 分块 31 无有效标线，跳过',
    '[10:03:40] 分块推理完成，合并相邻标线',
    '[10:03:44] 面状标线识别：132 个',
    '[10:03:45] 写入 GDB：D:\\众源解译\\输出\\正射提取.gdb',
    '[10:03:46] 正射提取完成，耗时 92.4 秒',
    '[10:03:46] 叠加预览图已生成'
  ];

  var LOGS_CMP = [
    '[10:21:03] 读取新成果 GDB：D:\\众源解译\\输出\\正射提取.gdb',
    '[10:21:04] 读取存量 GDB：D:\\存量\\城南存量.gdb',
    '[10:21:05] 图层配对完成：线↔线 · 面↔面 · 点↔点',
    '[10:21:06] 构建空间索引（新成果 1378 要素）',
    '[10:21:08] 匹配阈值 1.5 m，重叠度阈值 0.65',
    '[10:21:10] 比对标线图层：新 1246 / 存 1180',
    '[10:21:14] 匹配 1102 · 疑似新增 96 · 疑似缺失 42 · 疑似改动 48',
    '[10:21:16] 比对面状标线图层：新 132 / 存 128',
    '[10:21:18] 匹配 120 · 疑似新增 9 · 疑似缺失 3 · 疑似改动 6',
    '[10:21:20] 比对点要素图层：新 214 / 存 208',
    'WARN 点要素坐标系与存量不一致，已做动态投影',
    '[10:21:24] 匹配 196 · 疑似新增 12 · 疑似缺失 4 · 疑似改动 8',
    '[10:21:26] 生成变化要素 GDB',
    '[10:21:28] 生成比对报告.json',
    '[10:21:28] 变化比对完成',
    '[10:21:28] 留档目录：D:\\存量\\变化比对_20250412_1021'
  ];

  var CAR_OUT = 'D:\\众源解译\\输出\\车载提取_20250412_0931';
  var DOM_OUT = 'D:\\众源解译\\输出\\正射提取.gdb';
  var CMP_OUT = 'D:\\存量\\变化比对_20250412_1021\\变化比对.gdb';

  /* ─────────── 工具 ─────────── */
  function rnd(i, k) {
    var x = Math.sin(i * 12.9898 + k * 78.233) * 43758.5453;
    return Math.floor(Math.abs(x - Math.floor(x)) * 10) % 7;
  }
  function svgUri(svg) {
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }

  /* 占位预览图：纯内联 SVG，零外部资源 */
  function domPreview() {
    var g = '', i;
    for (i = 0; i < 13; i++) g += '<line x1="' + (i * 50) + '" y1="0" x2="' + (i * 50) + '" y2="400" stroke="#DCE1EA" stroke-width="1"/>';
    for (i = 0; i < 9; i++) g += '<line x1="0" y1="' + (i * 50) + '" x2="640" y2="' + (i * 50) + '" stroke="#DCE1EA" stroke-width="1"/>';
    return svgUri(
      '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">' +
      '<rect width="640" height="400" fill="#EEF1F6"/>' + g +
      '<path d="M0 250 L640 130 L640 400 L0 400 Z" fill="#E2E7EF"/>' +
      '<path d="M0 250 L640 130" stroke="#C6CDD9" stroke-width="26" fill="none"/>' +
      '<path d="M20 244 L620 132" stroke="#3E63D8" stroke-width="3" fill="none"/>' +
      '<path d="M20 268 L620 156" stroke="#3E63D8" stroke-width="3" stroke-dasharray="18 14" fill="none"/>' +
      '<path d="M120 232 L140 210 L180 210 L200 232 Z" fill="#3E63D8" opacity=".38"/>' +
      '<path d="M420 180 L440 158 L480 158 L500 180 Z" fill="#3E63D8" opacity=".38"/>' +
      '<text x="16" y="24" font-family="monospace" font-size="13" fill="#64748B">正射提取 · 叠加预览（演示占位）</text>' +
      '</svg>');
  }
  function cmpPreview() {
    return svgUri(
      '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">' +
      '<rect width="640" height="400" fill="#EEF1F6"/>' +
      '<path d="M0 260 L640 140 L640 400 L0 400 Z" fill="#E2E7EF"/>' +
      '<path d="M0 260 L640 140" stroke="#C6CDD9" stroke-width="26" fill="none"/>' +
      '<path d="M30 254 L610 144" stroke="#94A1B5" stroke-width="3" fill="none"/>' +
      '<path d="M40 286 L300 232" stroke="#12A150" stroke-width="4" fill="none"/>' +
      '<path d="M330 226 L600 166" stroke="#E5484D" stroke-width="4" stroke-dasharray="16 12" fill="none"/>' +
      '<path d="M60 306 L280 262" stroke="#D97A06" stroke-width="4" fill="none"/>' +
      '<circle cx="90" cy="352" r="7" fill="#12A150"/><text x="106" y="358" font-family="monospace" font-size="13" fill="#0A7A42">新增</text>' +
      '<circle cx="200" cy="352" r="7" fill="#E5484D"/><text x="216" y="358" font-family="monospace" font-size="13" fill="#C42A2F">缺失</text>' +
      '<circle cx="310" cy="352" r="7" fill="#D97A06"/><text x="326" y="358" font-family="monospace" font-size="13" fill="#9E5A00">改动</text>' +
      '<text x="16" y="30" font-family="monospace" font-size="13" fill="#64748B">变化比对 · 叠加预览（演示占位）</text>' +
      '</svg>');
  }

  /* ─────────── 车载影像：文件队列 / 结果 ─────────── */
  function carFiles(prog) {
    var out = [], per = Math.ceil(CAR_TOTAL / SESSIONS.length);
    for (var i = 0; i < CAR_TOTAL; i++) {
      var sess = SESSIONS[Math.min(SESSIONS.length - 1, Math.floor(i / per))];
      var f = {
        name: 'frame_' + String(i * 7 + 101).padStart(6, '0') + '.jpg',
        session: sess,
        status: i < prog ? (FAIL_IDX[i] ? 'failed' : 'done') : 'pending'
      };
      if (f.status === 'done') {
        f.cats = {};
        for (var c = 0; c < CATS.length; c++) f.cats[CATS[c]] = rnd(i, c + 1);
      }
      out.push(f);
    }
    return out;
  }

  function carResult() {
    var perSession = {}, per = Math.ceil(CAR_TOTAL / SESSIONS.length);
    for (var s = 0; s < SESSIONS.length; s++) {
      var cats = {}, lane = 0;
      for (var i = s * per; i < Math.min(CAR_TOTAL, (s + 1) * per); i++) {
        if (FAIL_IDX[i]) continue;
        for (var c = 0; c < CATS.length; c++) cats[CATS[c]] = (cats[CATS[c]] || 0) + rnd(i, c + 1);
        lane += 3 + rnd(i, 9);
      }
      perSession[SESSIONS[s]] = { cats: cats, lane: lane };
    }
    return { per_session: perSession, output_dir: CAR_OUT };
  }

  var DOM_RESULT = {
    lines: 1246, areas: 132, seconds: 92.4,
    gdb: DOM_OUT, preview: domPreview()
  };

  var CMP_RESULT = {
    totals: { matched: 1418, new: 117, missing: 49, modified: 62 },
    by_kind: {
      lines:  { matched: 1102, new: 96, missing: 42, modified: 48 },
      areas:  { matched: 120,  new: 9,  missing: 3,  modified: 6  },
      points: { matched: 196,  new: 12, missing: 4,  modified: 8  }
    },
    change_gdb: CMP_OUT,
    preview: cmpPreview()
  };

  /* ─────────── 状态机 ─────────── */
  function newRun(frac) {
    return { startedAt: Date.now() - Math.round(frac), forcedDone: false };
  }
  var st = {
    car: newRun(CAR_TOTAL * CAR_STEP * 0.62),
    dom: newRun(DOM_TOTAL * DOM_STEP * 0.62),
    cmp: newRun(LOGS_CMP.length * CMP_STEP * 0.60)
  };

  function carSnap() {
    var s = st.car;
    var el = Date.now() - s.startedAt;
    var prog = Math.min(CAR_TOTAL, Math.floor(el / CAR_STEP));
    var done = s.forcedDone || prog >= CAR_TOTAL;
    if (done) prog = CAR_TOTAL;
    var files = carFiles(prog);
    var emit = done ? LOGS_CAR.length : Math.max(1, Math.round(prog / CAR_TOTAL * LOGS_CAR.length));
    return {
      state: done ? 'done' : 'running',
      progress: prog, total: CAR_TOTAL,
      files: files,
      current: done ? '' : files[Math.min(prog, CAR_TOTAL - 1)].name,
      logs: LOGS_CAR.slice(0, emit),
      result: done ? carResult() : null,
      error: '',
      elapsed_ms: done ? CAR_TOTAL * CAR_STEP : el
    };
  }

  function domSnap() {
    var s = st.dom;
    var el = Date.now() - s.startedAt;
    var prog = Math.min(DOM_TOTAL, Math.floor(el / DOM_STEP));
    var done = s.forcedDone || prog >= DOM_TOTAL;
    if (done) prog = DOM_TOTAL;
    var emit = done ? LOGS_DOM.length : Math.max(1, Math.round(prog / DOM_TOTAL * LOGS_DOM.length));
    return {
      state: done ? 'done' : 'running',
      progress: prog, total: DOM_TOTAL,
      current: done ? '' : '分块 ' + (prog + 1) + ' / ' + DOM_TOTAL,
      logs: LOGS_DOM.slice(0, emit),
      result: done ? DOM_RESULT : null,
      error: '',
      elapsed_ms: done ? DOM_TOTAL * DOM_STEP : el
    };
  }

  function cmpSnap() {
    var s = st.cmp;
    var el = Date.now() - s.startedAt;
    var n = Math.min(LOGS_CMP.length, Math.floor(el / CMP_STEP));
    var done = s.forcedDone || n >= LOGS_CMP.length;
    if (done) n = LOGS_CMP.length;
    return {
      state: done ? 'done' : 'running',
      progress: n, total: LOGS_CMP.length,
      logs: LOGS_CMP.slice(0, n),
      result: done ? CMP_RESULT : null,
      error: '',
      elapsed_ms: done ? LOGS_CMP.length * CMP_STEP : el
    };
  }

  /* ─────────── 14 个接口（与真实后端契约一致） ─────────── */
  function ok(v) { return Promise.resolve(v); }

  var API = {
    ping: function () { return ok('pong'); },
    model_status: function () { return ok({ available: true, name: 'YOLOv8s-seg' }); },

    pick_folder: function (title) {
      var p = 'D:\\众源解译\\输出';
      if (/数据源|采集/.test(title || '')) p = 'D:\\采集车\\20250412';
      else if (/存量/.test(title || '')) p = 'D:\\存量\\城南存量.gdb';
      else if (/新成果/.test(title || '')) p = 'D:\\众源解译\\输出\\正射提取.gdb';
      return ok({ ok: true, path: p });
    },
    pick_file: function (title) {
      var p = /底图/.test(title || '') ? 'D:\\DOM\\城南片区_0.05m.tif'
        : 'D:\\DOM\\城南片区_0.05m.tif';
      return ok({ ok: true, path: p });
    },
    open_folder: function () { return ok({ ok: true }); },

    scan_directory: function () { return ok({ images: 128, poses: 128, matched: 128 }); },

    start_pipeline: function () { st.car = newRun(0); return ok({ started: true }); },
    get_status: function () { return ok(carSnap()); },

    start_dom: function () { st.dom = newRun(0); return ok({ started: true }); },
    get_dom_status: function () { return ok(domSnap()); },

    start_compare: function () {
      st.cmp = newRun(0);
      return ok({ started: true, output_dir: 'D:\\存量\\变化比对_20250412_1021' });
    },
    get_cmp_status: function () { return ok(cmpSnap()); },

    preview_image: function () { return ok(CMP_OUT); },
    read_text_file: function () { return ok(''); }
  };

  window.pywebview = { api: API };

  /* ─────────── 演示态切换 ─────────── */
  AVT.demo = {
    mode: 'running',
    set: function (mode) {
      AVT.demo.mode = mode;
      if (mode === 'done') {
        st.car = newRun(CAR_TOTAL * CAR_STEP); st.car.forcedDone = true;
        st.dom = newRun(DOM_TOTAL * DOM_STEP); st.dom.forcedDone = true;
        st.cmp = newRun(LOGS_CMP.length * CMP_STEP); st.cmp.forcedDone = true;
      } else {
        st.car = newRun(CAR_TOTAL * CAR_STEP * 0.62);
        st.dom = newRun(DOM_TOTAL * DOM_STEP * 0.62);
        st.cmp = newRun(LOGS_CMP.length * CMP_STEP * 0.60);
      }
      if (AVT.pages && AVT.pages.car) AVT.pages.car.demoApply();
      if (AVT.pages && AVT.pages.dom) AVT.pages.dom.demoApply();
      if (AVT.pages && AVT.pages.cmp) AVT.pages.cmp.demoApply();
    }
  };

  /* 兼容 pywebviewready：同步已无意义，异步补发一次 */
  setTimeout(function () {
    try { window.dispatchEvent(new Event('pywebviewready')); } catch (e) { /* noop */ }
  }, 0);
})();
