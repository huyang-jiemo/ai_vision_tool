/* ============================================================
   pages/cmp.js — 变化比对
   字段：新成果 GDB / 存量 GDB / 底图 TIF（可选）
        （无输出目录：自动留档到存量 GDB 同级 / 变化比对_时间戳）
   轮询：setInterval 500ms → get_cmp_status
   ============================================================ */
(function (AVT) {
  'use strict';
  var U = AVT.ui, A = AVT.api, el = U.el;

  var CHIP = {
    idle: ['idle', '待机', ''],
    run: ['run', '比对中', 'dot'],
    done: ['done', '已完成', 'check'],
    fail: ['fail', '失败', 'bang']
  };
  var KINDS = [['lines', '标线'], ['areas', '面状标线'], ['points', '点要素']];
  var EMPTY_TIP = '尚未运行';
  var EMPTY_ICON = '<path d="M9 3H4v18h5M15 3h5v18h-5M12 7v10"/>';

  var log = null, syncReady = null, pollTimer = null;
  var lastCmpOut = '', finished = false;

  /* ─────────── 结果 ─────────── */
  function statHtml(tone, k, v) {
    return '<div class="stat stat--' + tone + '"><div class="stat__k">' + U.esc(k) + '</div>' +
      '<div class="stat__v"><span class="u-num">' + U.esc(v) + '</span></div></div>';
  }
  function showCmpResult(r) {
    var t = r.totals || {}, by = r.by_kind || {};
    var html = '<div class="stat4">' +
      statHtml('same', '匹配', t.matched || 0) +
      statHtml('new', '疑似新增', t.new || 0) +
      statHtml('miss', '缺失', t.missing || 0) +
      statHtml('mod', '疑似改动', t.modified || 0) +
      '</div>';

    html += '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      '<th>类别</th><th class="c-num">匹配</th><th class="c-num">疑似新增</th>' +
      '<th class="c-num">缺失</th><th class="c-num col-opt">疑似改动</th>' +
      '</tr></thead><tbody>';
    KINDS.forEach(function (kk) {
      var v = by[kk[0]];
      if (!v) return;
      if (v.skipped) {
        html += '<tr><td class="c-main">' + kk[1] + '</td><td colspan="4" class="u-dim">新成果无此类要素，跳过</td></tr>';
        return;
      }
      html += '<tr><td class="c-main">' + kk[1] + '</td>' +
        '<td class="c-num"><span class="u-num">' + v.matched + '</span></td>' +
        '<td class="c-num"><span class="u-num">' + v.new + '</span></td>' +
        '<td class="c-num"><span class="u-num">' + v.missing + '</span></td>' +
        '<td class="c-num col-opt"><span class="u-num">' + v.modified + '</span></td></tr>';
    });
    html += '</tbody></table></div>';

    if (r.change_gdb) {
      var outDir = String(r.change_gdb).replace(/[\\/]变化比对\.gdb$/, '');
      if (outDir) lastCmpOut = outDir;
    }
    el('cmpResult').innerHTML = html;
    U.setPath('cmpResPath', r.change_gdb ? r.change_gdb + ' · 比对报告.json' : '');
    /* 预览图单独占右侧整块区域 */
    var pv = el('cmpPrev');
    if (r.preview) {
      U.previewData(r.preview).then(function (node) {
        if (!node) return;
        pv.innerHTML = '';
        pv.appendChild(node);
      });
    } else {
      pv.innerHTML = U.emptyHtml(EMPTY_ICON, '本次比对未生成预览图');
    }
    el('cmpOpenOut').hidden = !lastCmpOut;
  }
  function showEmpty() {
    el('cmpResult').innerHTML = U.emptyHtml(EMPTY_ICON, EMPTY_TIP);
    el('cmpPrev').innerHTML = U.emptyHtml(EMPTY_ICON, EMPTY_TIP);
  }

  /* ─────────── 快照应用 ─────────── */
  function apply(s) {
    log.push(s.logs);
    if (s.state === 'error') {
      finished = false;
      U.setChip(el('cmpChip'), CHIP, 'fail');
      U.setPath('cmpResPath', '');
      el('cmpResult').innerHTML = '<p class="hint hint--err">失败：' + U.esc(s.error || '') + '</p>';
      el('cmpPrev').innerHTML = U.emptyHtml(EMPTY_ICON, '比对失败，无预览图');
    } else if (s.state === 'done') {
      finished = true;
      U.setChip(el('cmpChip'), CHIP, 'done');
      if (s.result) showCmpResult(s.result);
    } else if (s.state === 'running') {
      finished = false;
      U.setChip(el('cmpChip'), CHIP, 'run');
      U.setPath('cmpResPath', '');
      el('cmpResult').innerHTML = U.emptyHtml(EMPTY_ICON, '正在比对，完成后显示结果');
      el('cmpPrev').innerHTML = U.emptyHtml(EMPTY_ICON, '正在比对，完成后显示预览图');
    } else {
      U.setChip(el('cmpChip'), CHIP, 'idle');
    }
    syncSteps();
  }

  function syncSteps() {
    var n = 0;
    if (U.isFilled('cmpNew')) n = 1;
    if (n === 1 && U.isFilled('cmpStock')) n = 2;
    if (n === 2 && finished) n = 3;
    U.setSteps(el('tab-cmp').querySelector('.steps'), n);
  }

  /* ─────────── 轮询 ─────────── */
  function poll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      A.call('get_cmp_status').then(function (s) {
        apply(s);
        if (s.state === 'done' || s.state === 'error') {
          clearInterval(pollTimer);
          if (syncReady) syncReady();
          U.toast(s.state === 'error' ? '比对失败' : '变化比对完成', s.state === 'error' ? 'err' : 'good');
        }
      }).catch(function () { /* 后端未就绪：忽略本次轮询 */ });
    }, 500);
  }

  /* ─────────── 启动 ─────────── */
  function run() {
    var ng = el('cmpNew').value.trim(), sg = el('cmpStock').value.trim();
    if (!ng) { U.toast('请选择新成果 GDB', 'err'); return; }
    if (!sg) { U.toast('请选择存量 GDB', 'err'); return; }
    A.call('start_compare', ng, sg, el('cmpTif').value.trim() || null)
      .then(function (r) {
        if (!r || !r.started) { U.toast((r && r.error) || '启动失败', 'err'); return; }
        if (r.output_dir) lastCmpOut = r.output_dir;
        finished = false;
        U.setChip(el('cmpChip'), CHIP, 'run');
        el('cmpRunBtn').disabled = true;
        syncSteps();
        poll();
      })
      .catch(function (e) { U.toast('启动失败：' + e, 'err'); });
  }

  /* ─────────── 初始化 ─────────── */
  function init() {
    log = U.LogPanel('cmpLog');

    el('browseCmpNew').onclick = function () { A.pickDir('cmpNew', '选择新成果 GDB 目录'); };
    el('browseCmpStock').onclick = function () { A.pickDir('cmpStock', '选择存量 GDB 目录'); };
    el('browseCmpTif').onclick = function () { A.pickFile('cmpTif', '选择底图 TIF', ['.tif', '.tiff']); };
    el('cmpOpenOut').onclick = function () { A.openDir(lastCmpOut); };
    el('cmpRunBtn').onclick = run;

    syncReady = U.bindReady('cmpRunBtn', ['cmpNew', 'cmpStock']);

    showEmpty();
    syncSteps();
    U.setChip(el('cmpChip'), CHIP, 'idle');
  }

  AVT.pages = AVT.pages || {};
  AVT.pages.cmp = {
    init: init,
    demoApply: function () {
      A.call('get_cmp_status').then(apply).catch(function () { /* noop */ });
    }
  };
})(window.AVT = window.AVT || {});
