/* ============================================================
   pages/dom.js — 正射影像提取
   字段：正射影像 TIF / 输出目录 / 存量 GDB（可选）/ 最短标线（米）
        + 按存量范围裁剪 / 仅提取存量道路走廊
   轮询：setInterval 500ms → get_dom_status
   ============================================================ */
(function (AVT) {
  'use strict';
  var U = AVT.ui, A = AVT.api, el = U.el;

  var CHIP = {
    idle: ['idle', '待机', ''],
    run: ['run', '提取中', 'dot'],
    done: ['done', '已完成', 'check'],
    warn: ['warn', '部分完成', 'bang'],
    fail: ['fail', '失败', 'bang']
  };
  var EMPTY_TIP = '尚未运行';
  var EMPTY_ICON = '<path d="M2 16l4-4 4 3 5-6 4 4 3-3"/><path d="M2 20h20"/>';

  var log = null, syncReady = null;
  var runStartTs = 0, pollTimer = null;
  var finished = false, lastDomResult = null;

  /* ─────────── 结果 ─────────── */
  function statHtml(k, v) {
    return '<div class="stat"><div class="stat__k">' + U.esc(k) + '</div>' +
      '<div class="stat__v"><span class="u-num">' + U.esc(v) + '</span></div></div>';
  }
  function showDomResult(r) {
    var html = '<div class="stat4">' +
      statHtml('标线（条）', r.lines) +
      statHtml('面状标线（个）', r.areas) +
      statHtml('耗时（秒）', r.seconds) +
      '</div>';
    el('domResult').innerHTML = html;
    U.setPath('domResPath', r.gdb || '');
    if (r.preview) {
      U.previewData(r.preview).then(function (img) {
        if (img) { el('domResult').appendChild(img); }
        else {
          var p = document.createElement('p');
          p.className = 'hint hint--err';
          p.textContent = '预览图加载失败，请打开输出目录查看';
          el('domResult').appendChild(p);
        }
      });
    }
  }
  function showEmpty() {
    el('domResult').innerHTML = U.emptyHtml(EMPTY_ICON, EMPTY_TIP);
  }

  /* ─────────── 快照应用 ─────────── */
  function apply(s) {
    var total = s.total || 0, prog = s.progress || 0;
    var pctv = total ? Math.round(prog / total * 100) : 0;

    var p = el('domPct');
    p.innerHTML = pctv + '<span class="pct__u">%</span>';
    p.classList.toggle('is-full', total > 0 && pctv >= 100);
    el('domPctSub').textContent = prog + ' / ' + total;
    el('domBar').style.width = pctv + '%';
    el('domCurFile').textContent = s.current || '';

    var ms = (s.elapsed_ms != null) ? s.elapsed_ms : (runStartTs ? Date.now() - runStartTs : 0);
    var eta = el('domEtaText');
    if (prog > 0 && prog < total && ms > 0) {
      eta.hidden = false;
      eta.textContent = '剩余约 ' + U.fmtElapsed(Math.round((total - prog) * (ms / prog)));
    } else { eta.hidden = true; }

    log.push(s.logs);

    if (s.state === 'error') {
      finished = false;
      U.setChip(el('domChip'), CHIP, 'fail');
      U.setPath('domResPath', '');
      el('domResult').innerHTML = '<p class="hint hint--err">失败：' + U.esc(s.error || '') + '</p>';
    } else if (s.state === 'done') {
      finished = true;
      lastDomResult = s.result || null;
      U.setChip(el('domChip'), CHIP, 'done');
      if (s.result) showDomResult(s.result);
    } else if (s.state === 'running') {
      finished = false;
      U.setChip(el('domChip'), CHIP, 'run');
      U.setPath('domResPath', '');
      el('domResult').innerHTML = U.emptyHtml(EMPTY_ICON, '正在提取，完成后显示统计与预览图');
    } else {
      U.setChip(el('domChip'), CHIP, 'idle');
    }
    syncSteps();
  }

  function syncSteps() {
    var n = 0;
    if (U.isFilled('domTif')) n = 1;
    if (n === 1 && U.isFilled('domOut')) n = 2;
    if (finished) n = 3;
    U.setSteps(el('tab-dom').querySelector('.steps'), n);
  }

  /* ─────────── 轮询 ─────────── */
  function poll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      A.call('get_dom_status').then(function (s) {
        apply(s);
        if (s.state === 'done' || s.state === 'error') {
          clearInterval(pollTimer);
          if (syncReady) syncReady();
          U.toast(s.state === 'error' ? '正射提取失败' : '正射提取完成', s.state === 'error' ? 'err' : 'good');
        }
      }).catch(function () { /* 后端未就绪：忽略本次轮询 */ });
    }, 500);
  }

  /* ─────────── 启动 ─────────── */
  function run() {
    var tif = el('domTif').value.trim(), out = el('domOut').value.trim();
    if (!tif) { U.toast('请选择正射影像目录', 'err'); return; }
    if (!out) { U.toast('请选择输出目录', 'err'); return; }
    A.call('start_dom', tif, out, el('domStock').value.trim() || null,
      parseFloat(el('domMinLen').value) || 2.0,
      el('domClip').checked, el('domCorridor').checked)
      .then(function (r) {
        if (!r || !r.started) { U.toast((r && r.error) || '启动失败', 'err'); return; }
        runStartTs = Date.now();
        finished = false;
        U.setChip(el('domChip'), CHIP, 'run');
        el('domRunBtn').disabled = true;
        syncSteps();
        poll();
      })
      .catch(function (e) { U.toast('启动失败：' + e, 'err'); });
  }

  /* ─────────── 初始化 ─────────── */
  function init() {
    log = U.LogPanel('domLog');

    el('browseDomTif').onclick = function () {
      A.pickDir('domTif', '选择正射影像目录');
    };
    el('browseDomOut').onclick = function () { A.pickDir('domOut', '选择输出目录'); };
    el('browseDomStock').onclick = function () { A.pickDir('domStock', '选择存量 GDB 目录'); };
    /* 兜底：输出目录为空时用上一次结果的 GDB 路径 */
    el('domOpenOut').onclick = function () {
      A.openDir(el('domOut').value.trim() || (lastDomResult && lastDomResult.gdb) || '');
    };
    el('domRunBtn').onclick = run;

    syncReady = U.bindReady('domRunBtn', ['domTif', 'domOut']);

    showEmpty();
    syncSteps();
    U.setChip(el('domChip'), CHIP, 'idle');
  }

  AVT.pages = AVT.pages || {};
  AVT.pages.dom = {
    init: init,
    demoApply: function () {
      A.call('get_dom_status').then(apply).catch(function () { /* noop */ });
    }
  };
})(window.AVT = window.AVT || {});
