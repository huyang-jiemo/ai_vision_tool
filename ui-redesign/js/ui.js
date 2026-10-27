/* ============================================================
   ui.js — 通用 UI 原语
   el / esc / toast / fmtElapsed / ICON / 日志面板 / chip / steps /
   hint / err-box / 预览图
   ============================================================ */
(function (AVT) {
  'use strict';

  /* ─────────── 基础 ─────────── */
  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function pad2(n) { return String(n).padStart(2, '0'); }

  function fmtElapsed(ms) {
    var s = Math.floor((ms || 0) / 1000);
    return pad2(Math.floor(s / 60)) + ':' + pad2(s % 60);
  }

  /* ─────────── 图标（全部内联 SVG，24×24 stroke） ─────────── */
  function svg(body, sw) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + (sw || 2) +
      '" stroke-linecap="round" stroke-linejoin="round">' + body + '</svg>';
  }
  var ICON = {
    check: svg('<path d="M20 6L9 17l-5-5"/>', 2.2),
    checkSm: svg('<path d="M20 6L9 17l-5-5"/>', 3.2),
    bang: svg('<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>', 2.2),
    alert: svg('<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>', 2),
    done: svg('<path d="M20 6L9 17l-5-5"/>', 2.4),
    failed: svg('<path d="M18 6L6 18M6 6l12 12"/>', 2.4),
    pending: svg('<circle cx="12" cy="12" r="8"/>', 2.4),
    folder: svg('<path d="M4 20h16a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1h-8.6a1 1 0 0 1-.83-.45L9.4 4.45A1 1 0 0 0 8.57 4H4a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1z"/>', 2),
    db: svg('<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>', 2)
  };

  /* ─────────── Toast：右下角 ─────────── */
  var toastTimer = null;
  function hideToast() {
    var t = el('toast');
    if (t) t.classList.remove('is-show');
  }
  function toast(msg, kind) {
    var t = el('toast');
    if (!t) return;
    kind = kind === 'err' ? 'err' : 'good';
    el('toastIco').innerHTML = kind === 'err' ? ICON.alert : ICON.check;
    el('toastMsg').textContent = msg;
    el('toastX').hidden = (kind !== 'err');
    t.className = 'toast toast--' + kind + ' is-show';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, kind === 'err' ? 6000 : 3200);
  }

  /* ─────────── 日志语义着色（沿用原正则） ─────────── */
  function logLineClass(s) {
    if (/^ERR|^失败|失败$|中断/.test(s)) return 'err';
    if (/完成|成功|PASS/.test(s)) return 'ok';
    if (/走廊|裁剪|范围|GDB|图层|要素/.test(s)) return 'info';
    if (/警告|WARN|跳过/.test(s)) return 'warn';
    return '';
  }

  /* ─────────── 日志面板：跟随 / 复制 / 清空 / 行数上限 ─────────── */
  var MAX_LOG_NODES = 2000;

  function LogPanel(boxId) {
    var box = el(boxId);
    var wrap = box.parentNode;
    while (wrap && !wrap.classList.contains('log-wrap')) wrap = wrap.parentNode;

    var jump = wrap ? wrap.querySelector('.log-jump') : null;
    var self = { box: box, n: 0, lines: [], follow: true, auto: true };

    function atBottom() { return box.scrollTop + box.clientHeight >= box.scrollHeight - 24; }
    function toBottom() { box.scrollTop = box.scrollHeight; }
    function refreshJump() { if (jump) jump.hidden = !(self.auto && !self.follow); }

    self.push = function (logs) {
      logs = logs || [];
      if (logs.length < self.n) { box.innerHTML = ''; self.n = 0; self.lines = []; }
      if (logs.length <= self.n) return;

      var stick = self.auto ? (self.follow || atBottom()) : false;
      var frag = document.createDocumentFragment();
      for (var i = self.n; i < logs.length; i++) {
        var txt = String(logs[i]);
        var cls = logLineClass(txt);
        var d = document.createElement('span');
        d.className = 'log__l' + (cls ? ' log__l--' + cls : '');
        /* 每行补时间戳（后端日志文件里有，接口未带）：已带 [HH:MM:SS] 的不重复加 */
        if (!/^\[\d{2}:\d{2}:\d{2}\]/.test(txt)) {
          var now = new Date();
          txt = '[' + pad2(now.getHours()) + ':' + pad2(now.getMinutes()) + ':' +
            pad2(now.getSeconds()) + '] ' + txt;
        }
        d.textContent = txt;
        frag.appendChild(d);
      }
      box.appendChild(frag);
      self.n = logs.length;
      self.lines = logs.slice(-MAX_LOG_NODES);
      while (box.childNodes.length > MAX_LOG_NODES) box.removeChild(box.firstChild);

      if (stick) { self.follow = true; toBottom(); } else { self.follow = false; }
      refreshJump();
    };

    self.clear = function () {
      box.innerHTML = ''; self.n = 0; self.lines = [];
      self.follow = true; refreshJump();
    };

    self.copy = function () {
      var txt = self.lines.join('\n');
      if (!txt) { toast('日志为空', 'err'); return; }
      copyText(txt).then(function () { toast('日志已复制', 'good'); },
                         function () { toast('复制失败', 'err'); });
    };

    box.addEventListener('scroll', function () {
      if (atBottom() && !self.follow) { self.follow = true; refreshJump(); }
    });

    if (wrap) {
      var acts = wrap.querySelectorAll('[data-log-act]');
      for (var k = 0; k < acts.length; k++) {
        (function (b) {
          var act = b.getAttribute('data-log-act');
          if (act === 'follow') {
            b.onclick = function () {
              self.auto = !self.auto;
              b.classList.toggle('is-on', self.auto);
              if (self.auto) { self.follow = true; toBottom(); }
              refreshJump();
            };
          } else if (act === 'copy') { b.onclick = self.copy; }
          else if (act === 'clear') { b.onclick = self.clear; }
          else if (act === 'jump') {
            b.onclick = function () { self.follow = true; toBottom(); refreshJump(); };
          }
        })(acts[k]);
      }
    }
    return self;
  }

  function copyText(txt) {
    return new Promise(function (resolve, reject) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(resolve, fallback);
      } else { fallback(); }
      function fallback() {
        try {
          var ta = document.createElement('textarea');
          ta.value = txt;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          var okd = document.execCommand('copy');
          document.body.removeChild(ta);
          okd ? resolve() : reject(new Error('copy failed'));
        } catch (e) { reject(e); }
      }
    });
  }

  /* ─────────── 状态 chip ─────────── */
  function setChip(node, map, state) {
    if (!node) return;                        /* 页面已不展示状态 chip */
    var m = map[state] || map.idle;           // [变体, 文案, 图标类型]
    /* 待机态不显示 chip（界面更干净），运行/完成/失败时才出现 */
    node.hidden = (state === 'idle');
    if (node.hidden) { node.innerHTML = ''; return; }

    var ico = '';
    if (m[2] === 'dot') ico = '<span class="chip__dot"></span>';
    else if (m[2] === 'check') ico = '<span class="chip__ico">' + ICON.checkSm + '</span>';
    else if (m[2] === 'bang') ico = '<span class="chip__ico">' + ICON.bang + '</span>';
    node.className = 'chip chip--' + m[0];
    node.innerHTML = ico + '<span>' + esc(m[1]) + '</span>';
  }

  /* ─────────── 三步流程指示 ─────────── */
  function setSteps(container, doneCount) {
    var items = container.querySelectorAll('.step');
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var n = it.querySelector('.step__n');
      it.classList.remove('is-done', 'is-current');
      if (i < doneCount) {
        it.classList.add('is-done');
        n.innerHTML = ICON.checkSm;
      } else if (i === doneCount) {
        it.classList.add('is-current');
        n.textContent = String(i + 1);
      } else {
        n.textContent = String(i + 1);
      }
    }
  }

  /* ─────────── 提示行 ─────────── */
  function setHint(node, text, kind) {
    var ico = kind === 'ok' ? '<span class="hint__ic">' + ICON.check + '</span>'
      : kind === 'err' ? '<span class="hint__ic">' + ICON.alert + '</span>' : '';
    node.className = 'hint' + (kind ? ' hint--' + kind : '');
    node.innerHTML = ico + '<span>' + esc(text) + '</span>';
  }

  /* ─────────── 错误框：标题红 + 正文等宽中性 ─────────── */
  function showErrBox(boxId, title, text) {
    var box = el(boxId);
    if (!box) return;
    var body = box.querySelector('.err-box__b');
    var more = box.querySelector('.err-box__more');
    var ttl = box.querySelector('.err-box__t span');
    if (ttl) ttl.textContent = title || '提取中断';
    body.textContent = String(text || '');
    body.classList.add('is-clamp');
    more.hidden = false;
    more.textContent = '展开完整信息';
    more.onclick = function () {
      var clamped = body.classList.toggle('is-clamp');
      more.textContent = clamped ? '展开完整信息' : '收起';
    };
    box.hidden = false;
  }
  function hideErrBox(boxId) {
    var box = el(boxId);
    if (box) box.hidden = true;
  }

  /* ─────────── 预览图 ─────────── */
  function localImg(path) {
    var img = new Image();
    var p = String(path).replace(/\\/g, '/');
    img.src = /^https?:|^file:|^data:/.test(p) ? p
      : (p.charAt(0) === '/' ? 'file://' + p : 'file:///' + p);
    img.className = 'prev-img';
    img.alt = '结果预览';
    return img;
  }

  /* 可缩放预览容器：默认宽度铺满，滚轮缩放（以光标为中心）、拖动平移、双击还原 */
  function zoomableWrap(img) {
    var wrap = document.createElement('div');
    wrap.className = 'prev-wrap';
    wrap.title = '滚轮缩放 · 拖动平移 · 双击还原';
    wrap.appendChild(img);
    var scale = 1, pan = null;
    function apply() { img.style.width = (scale * 100) + '%'; }
    function setScale(s) {
      var ns = Math.max(1, Math.min(10, s));
      if (ns === scale) return false;
      scale = ns;
      if (scale === 1) { wrap.scrollTop = 0; wrap.scrollLeft = 0; }
      apply();
      return true;
    }
    wrap.addEventListener('wheel', function (e) {
      /* 已是最小倍数时向下滚不劫持，保留原生纵向滚动 */
      if (e.deltaY > 0 && scale <= 1) return;
      e.preventDefault();
      var old = scale;
      if (!setScale(scale * (e.deltaY < 0 ? 1.2 : 1 / 1.2))) return;
      var r = wrap.getBoundingClientRect();
      var cx = e.clientX - r.left, cy = e.clientY - r.top;
      var ratio = scale / old;
      wrap.scrollLeft = (cx + wrap.scrollLeft) * ratio - cx;
      wrap.scrollTop = (cy + wrap.scrollTop) * ratio - cy;
    }, { passive: false });
    wrap.addEventListener('pointerdown', function (e) {
      pan = { x: e.clientX, y: e.clientY, sl: wrap.scrollLeft, st: wrap.scrollTop };
      try { wrap.setPointerCapture(e.pointerId); } catch (err) {}
      wrap.classList.add('is-pan');
      e.preventDefault();
    });
    wrap.addEventListener('pointermove', function (e) {
      if (!pan) return;
      wrap.scrollLeft = pan.sl - (e.clientX - pan.x);
      wrap.scrollTop = pan.st - (e.clientY - pan.y);
    });
    function endPan() { pan = null; wrap.classList.remove('is-pan'); }
    wrap.addEventListener('pointerup', endPan);
    wrap.addEventListener('pointercancel', endPan);
    wrap.addEventListener('dblclick', function () { setScale(1); });
    return wrap;
  }

  /* 预览图经后端 base64 通道加载：中文路径的 file:// 直连在 WKWebView 里会静默失败 */
  function previewData(path) {
    return new Promise(function (resolve) {
      if (!path) { resolve(null); return; }
      AVT.api.call('preview_image', String(path)).then(function (data) {
        if (!data) { resolve(null); return; }
        var img = new Image();
        img.className = 'prev-img';
        img.alt = '结果预览';
        img.src = data;
        resolve(zoomableWrap(img));
      }).catch(function () { resolve(null); });
    });
  }

  /* ─────────── 卡片头路径（置于「打开输出目录」按钮之前） ─────────── */
  function setPath(id, text) {
    var n = el(id);
    if (!n) return;
    var t = String(text || '');
    if (!t) { n.hidden = true; n.textContent = ''; n.removeAttribute('title'); return; }
    n.textContent = t;
    n.title = t;
    n.hidden = false;
  }

  /* ─────────── 空状态 ─────────── */
  function emptyHtml(iconBody, text) {
    return '<div class="empty"><span class="empty__ic">' +
      svg(iconBody, 1.6) + '</span><span class="empty__t">' + esc(text) + '</span></div>';
  }

  /* ─────────── 主按钮就绪校验（三页同源逻辑） ───────────
     bindReady(btnId, fieldIds, extra) 返回校验函数，运行前再确认一次 */
  function isFilled(id) {
    var n = el(id);
    return !!n && String(n.value || '').trim() !== '';
  }
  function bindReady(btnId, fieldIds, extra) {
    var btn = el(btnId);
    function sync() {
      var okd = true;
      for (var i = 0; i < fieldIds.length; i++) {
        if (!isFilled(fieldIds[i])) { okd = false; break; }
      }
      if (okd && typeof extra === 'function') okd = !!extra();
      btn.disabled = !okd;
      return okd;
    }
    fieldIds.forEach(function (id) {
      var n = el(id);
      if (!n) return;
      n.addEventListener('input', sync);
      n.addEventListener('change', sync);
    });
    sync();
    return sync;
  }

  /* ─────────── 路径行 ─────────── */
  function pathlineHtml(icon, path) {
    return '<div class="pathline"><span class="pathline__ic">' + icon +
      '</span><span class="pathline__p" title="' + esc(path) + '">' + esc(path) + '</span></div>';
  }

  AVT.ui = {
    el: el, esc: esc, toast: toast, hideToast: hideToast,
    fmtElapsed: fmtElapsed, pad2: pad2,
    ICON: ICON, svg: svg,
    logLineClass: logLineClass, LogPanel: LogPanel, copyText: copyText,
    setChip: setChip, setSteps: setSteps, setHint: setHint,
    isFilled: isFilled, bindReady: bindReady,
    showErrBox: showErrBox, hideErrBox: hideErrBox,
    localImg: localImg, previewData: previewData,
    emptyHtml: emptyHtml, pathlineHtml: pathlineHtml, setPath: setPath
  };
})(window.AVT = window.AVT || {});
