/* ============================================================
   pages/car.js — 车载影像提取
   字段：数据源目录 / 输出目录 / 置信度阈值 / 最大影像数（0 = 不限）
   轮询：setInterval 400ms → get_status
   ============================================================ */
(function (AVT) {
  'use strict';
  var U = AVT.ui, A = AVT.api, el = U.el;

  /* 分类标签已统一为中性灰阶 chip，不再按序号着色 */
  var CATS = ['动态障碍', '交通标志', '信号灯', '路侧设施'];
  var CHIP = {
    idle: ['idle', '待机', ''],
    run: ['run', '提取中', 'dot'],
    done: ['done', '已完成', 'check'],
    warn: ['warn', '部分完成', 'bang'],
    fail: ['fail', '失败', 'bang']
  };
  var SCAN_TIP = '数据源目录需包含按批次同名配对的 照片/ 与 轨迹/ 子目录 · 成果输出 SHP（含标注预览图）';

  var log = null, syncReady = null;
  var runStartTs = 0, elapsedTimer = null, pollTimer = null;
  var fileFilter = 'all', lastFiles = [], lastScan = { dir: '', matched: -1 };
  var finished = false;

  /* ─────────── 文件队列 ─────────── */
  function splitName(name) {
    var i = Math.max(String(name).lastIndexOf('/'), String(name).lastIndexOf('\\'));
    return i < 0 ? ['', String(name)] : [String(name).slice(0, i + 1), String(name).slice(i + 1)];
  }
  function catChips(cats) {
    var parts = CATS.filter(function (c) { return cats && cats[c] > 0; })
      .map(function (c) { return '<span class="catchip">' + c + ' ' + cats[c] + '</span>'; });
    return '<span class="catchips">' + parts.join('') + '</span>';
  }
  function renderFiles(files, current) {
    var box = el('fileList');
    if (!files.length) {
      box.innerHTML = '<div class="frow frow--ph">暂无文件</div>';
      return;
    }
    var nameCount = {};
    files.forEach(function (f) { nameCount[f.name] = (nameCount[f.name] || 0) + 1; });
    var multi = {};
    files.forEach(function (f) { if (f.session) multi[f.session] = 1; });
    var multiSession = Object.keys(multi).length > 1;

    var shown = fileFilter === 'all' ? files : files.filter(function (f) {
      if (fileFilter === 'done') return f.status === 'done';
      if (fileFilter === 'failed') return f.status === 'failed';
      return f.status === 'pending';
    });

    var html = '';
    shown.forEach(function (f) {
      var running = f.status === 'pending' && f.name === current;
      var ic, tag;
      if (f.status === 'done') { ic = U.ICON.done; tag = '<span class="ftag ftag--done">完成</span>'; }
      else if (f.status === 'failed') { ic = U.ICON.failed; tag = '<span class="ftag ftag--failed">失败</span>'; }
      else if (running) { ic = '<span class="spinner"></span>'; tag = '<span class="ftag ftag--running">处理中</span>'; }
      else { ic = U.ICON.pending; tag = '<span class="ftag ftag--pending">等待</span>'; }

      var sess = ((multiSession || nameCount[f.name] > 1) && f.session)
        ? '<span class="fsess" title="批次：' + U.esc(f.session) + '">' + U.esc(f.session) + '</span>' : '';
      var meta = f.status === 'done' ? catChips(f.cats) : '';
      var seg = splitName(f.name);
      var tip = f.session ? f.session + ' / ' + f.name : f.name;

      html += '<div class="frow' + (running ? ' is-current' : '') + '" title="' + U.esc(tip) + '">' +
        '<span class="fic fic--' + (running ? 'running' : f.status) + '">' + ic + '</span>' + sess +
        '<span class="fname"><span class="fname__dir">' + U.esc(seg[0]) + '</span>' +
        '<span class="fname__file">' + U.esc(seg[1]) + '</span></span>' +
        meta + tag + '</div>';
    });
    box.innerHTML = html || '<div class="frow frow--ph">该筛选下暂无文件</div>';
    /* 不做滚动跟随：列表每次刷新后默认停在顶部 */
  }

  /* ─────────── 结果 ─────────── */
  function showResult(r) {
    var html = '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      '<th>批次</th><th class="c-num">动态障碍</th><th class="c-num">交通标志</th>' +
      '<th class="c-num col-opt">信号灯</th><th class="c-num">路侧设施</th><th class="c-num">车道线</th>' +
      '</tr></thead><tbody>';
    Object.keys(r.per_session || {}).forEach(function (k) {
      var v = r.per_session[k] || {}, c = v.cats || {};
      html += '<tr><td class="c-main">' + U.esc(k) + '</td>' +
        CATS.map(function (cc, i) {
          var td = '<td class="c-num"><span class="u-num">' + (c[cc] || 0) + '</span></td>';
          return i === 2 ? td.replace('class="c-num"', 'class="c-num col-opt"') : td;
        }).join('') +
        '<td class="c-num"><span class="u-num">' + (v.lane || 0) + '</span></td></tr>';
    });
    html += '</tbody></table></div>';
    el('resultBody').innerHTML = html;
    U.setPath('carResPath', r.output_dir ? r.output_dir + (r.change_path ? ' · 变化: ' + r.change_path : '') : '');
  }

  /* ─────────── 快照应用（轮询与演示共用） ─────────── */
  function apply(s) {
    var total = s.total || 0, prog = s.progress || 0;
    var files = s.files || [];
    var okN = files.filter(function (f) { return f.status === 'done'; }).length;
    var badN = files.filter(function (f) { return f.status === 'failed'; }).length;
    var pctv = total ? Math.round(prog / total * 100) : 0;

    var p = el('pct');
    p.innerHTML = pctv + '<span class="pct__u">%</span>';
    p.classList.toggle('is-full', total > 0 && pctv >= 100);
    el('pctSub').textContent = prog + ' / ' + total;
    el('bar').style.width = pctv + '%';

    el('tTotal').textContent = total;
    el('tOk').textContent = okN;
    el('tBad').textContent = badN;
    el('tBadTile').classList.toggle('is-zero', badN === 0);
    el('fCount').textContent = total ? (okN + badN) + ' / ' + total : '';

    var ms = (s.elapsed_ms != null) ? s.elapsed_ms : (runStartTs ? Date.now() - runStartTs : 0);
    el('tTime').textContent = U.fmtElapsed(ms);
    el('curFile').textContent = s.current || '—';
    /* 路径可能超长被 ellipsis 截断，补 title 让全名可悬停查看 */
    el('curFile').title = s.current || '';
    var eta = el('etaText');
    if (prog > 0 && prog < total && ms > 0) {
      eta.hidden = false;
      eta.textContent = '剩余约 ' + U.fmtElapsed(Math.round((total - prog) * (ms / prog)));
    } else { eta.hidden = true; }

    log.push(s.logs);
    lastFiles = files;
    renderFiles(files, s.current);

    if (s.state === 'error') {
      finished = false;
      U.setChip(el('stateChip'), CHIP, 'fail');
      U.setPath('carResPath', '');
      U.showErrBox('errBox', '提取中断', s.error || '发生未知错误');
    } else if (s.state === 'done') {
      finished = true;
      U.setChip(el('stateChip'), CHIP, badN ? 'warn' : 'done');
      if (s.result) showResult(s.result);
    } else if (s.state === 'running') {
      finished = false;
      U.setChip(el('stateChip'), CHIP, 'run');
      U.setPath('carResPath', '');
    } else {
      U.setChip(el('stateChip'), CHIP, 'idle');
    }
    syncSteps();
  }

  /* ─────────── 三步流程 ─────────── */
  function syncSteps() {
    var n = 0;
    if (U.isFilled('dataDir')) n = 1;
    if (n === 1 && U.isFilled('outDir')) n = 2;
    if (n === 2 && finished) n = 3;
    U.setSteps(el('tab-car').querySelector('.steps'), n);
  }

  /* ─────────── 扫描 ─────────── */
  function scanDir() {
    var d = el('dataDir').value.trim();
    var info = el('scanInfo');
    if (!d) { U.setHint(info, SCAN_TIP); return Promise.resolve(null); }
    if (lastScan.dir === d && lastScan.matched >= 0) return Promise.resolve(lastScan);
    U.setHint(info, '正在扫描数据源…');
    return A.call('scan_directory', d).then(function (r) {
      lastScan = { dir: d, matched: r.matched };
      if (r.error) {
        U.setHint(info, '扫描失败：' + r.error, 'err');
        return lastScan;
      }
      U.setHint(info, '影像 ' + r.images + ' · 位姿 ' + r.poses + ' · 可配对 ' + r.matched +
        (r.matched === 0 ? '，请检查目录结构' : ''), r.matched > 0 ? 'ok' : '');
      if (syncReady) syncReady();
      return lastScan;
    }).catch(function () {
      U.setHint(info, '后端未就绪', 'err');
      return null;
    });
  }

  /* ─────────── 轮询 ─────────── */
  function poll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      A.call('get_status').then(function (s) {
        var stop = (s.state === 'done' || s.state === 'error');
        if (stop) { clearInterval(pollTimer); clearInterval(elapsedTimer); elapsedTimer = null; }
        apply(s);
        if (stop) {
          if (syncReady) syncReady();
          if (s.state === 'error') {
            U.toast('提取失败', 'err');
          } else {
            var okN = (s.files || []).filter(function (f) { return f.status === 'done'; }).length;
            var badN = (s.files || []).filter(function (f) { return f.status === 'failed'; }).length;
            U.toast('提取完成，成功 ' + okN + ' 张' + (badN ? '，失败 ' + badN + ' 张' : ''), 'good');
          }
        }
      }).catch(function () { /* 后端未就绪：忽略本次轮询 */ });
    }, 400);
  }

  /* ─────────── 启动 ─────────── */
  function run() {
    var dataDir = el('dataDir').value.trim(), outDir = el('outDir').value.trim();
    if (!dataDir || !outDir) { U.toast('请先选择数据源目录与输出目录', 'err'); return; }
    scanDir().then(function () {
      if (lastScan.matched === 0) { U.toast('数据源中没有可配对的影像', 'err'); return; }
      return A.call('start_pipeline', dataDir, outDir,
        parseFloat(el('conf').value), parseInt(el('maxImg').value, 10) || null)
        .then(function (r) {
          if (!r || !r.started) { U.toast((r && r.error) || '启动失败', 'err'); return; }
          runStartTs = Date.now();
          finished = false;
          U.hideErrBox('errBox');
          U.setChip(el('stateChip'), CHIP, 'run');
          el('runBtn').disabled = true;
          clearInterval(elapsedTimer);
          elapsedTimer = setInterval(function () {
            el('tTime').textContent = U.fmtElapsed(Date.now() - runStartTs);
          }, 500);
          syncSteps();
          poll();
        })
        .catch(function (e) { U.toast('启动失败：' + e, 'err'); });
    });
  }

  /* ─────────── 初始化 ─────────── */
  function init() {
    log = U.LogPanel('log');

    el('browseData').onclick = function () {
      A.pickDir('dataDir', '选择数据源目录').then(function (p) { if (p) scanDir(); });
    };
    el('browseOut').onclick = function () { A.pickDir('outDir', '选择输出目录'); };
    el('openOut1').onclick = function () { A.openDir(el('outDir').value.trim()); };
    el('openOut2').onclick = function () { A.openDir(el('outDir').value.trim()); };
    el('runBtn').onclick = run;

    el('dataDir').addEventListener('change', function () { lastScan = { dir: '', matched: -1 }; scanDir(); });
    el('dataDir').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { lastScan = { dir: '', matched: -1 }; scanDir(); }
    });

    syncReady = U.bindReady('runBtn', ['dataDir', 'outDir'], function () {
      return lastScan.matched !== 0;
    });

    var chips = el('tab-car').querySelectorAll('.fchip');
    for (var i = 0; i < chips.length; i++) {
      (function (c) {
        c.onclick = function () {
          for (var k = 0; k < chips.length; k++) chips[k].classList.remove('is-on');
          c.classList.add('is-on');
          fileFilter = c.getAttribute('data-f');
          renderFiles(lastFiles, '');
        };
      })(chips[i]);
    }

    renderFiles([], '');
    syncSteps();
    U.setChip(el('stateChip'), CHIP, 'idle');
  }

  AVT.pages = AVT.pages || {};
  AVT.pages.car = {
    init: init,
    /* 演示态：直接渲染一份快照 */
    demoApply: function () {
      A.call('get_status').then(apply).catch(function () { /* noop */ });
    }
  };
})(window.AVT = window.AVT || {});
