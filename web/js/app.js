/* ============================================================
   app.js — 启动 / 导航 / 模型状态 / 演示态装配
   Tab 用 hidden 切换：不销毁 DOM、切页保留状态、切页回顶部
   ============================================================ */
(function (AVT) {
  'use strict';
  var U = AVT.ui, A = AVT.api, el = U.el;

  /* ─────────── 导航 ─────────── */
  function bindNav() {
    var items = document.querySelectorAll('.nav__i');
    for (var i = 0; i < items.length; i++) {
      (function (btn) {
        btn.onclick = function () {
          for (var k = 0; k < items.length; k++) items[k].classList.remove('is-on');
          btn.classList.add('is-on');
          var pages = document.querySelectorAll('.page');
          for (var j = 0; j < pages.length; j++) pages[j].hidden = true;
          var target = el('tab-' + btn.getAttribute('data-tab'));
          if (target) target.hidden = false;
          var m = document.querySelector('.wrap main');
          if (m) m.scrollTop = 0;
        };
      })(items[i]);
    }
  }

  /* ─────────── 模型状态 ─────────── */
  function modelStatus() {
    A.call('model_status').then(function (st) {
      if (st && st.available) {
        el('modelDot').className = 'dot is-ok';
        el('modelText').textContent = 'AI检测模型已就绪';
      } else {
        el('modelDot').className = 'dot is-no';
        el('modelText').textContent = '模型缺失，仅提取车道线';
      }
    }).catch(function () {
      el('modelText').textContent = '后端未就绪';
    });
  }

  /* ─────────── 演示态 ─────────── */
  var DEMO_PATHS = {
    dataDir: 'D:\\采集车\\20250412',
    outDir: 'D:\\众源解译\\输出\\车载提取_20250412_0931',
    domTif: 'D:\\DOM\\城南片区_0.05m.tif',
    domOut: 'D:\\众源解译\\输出',
    domStock: 'D:\\存量\\城南存量.gdb',
    cmpNew: 'D:\\众源解译\\输出\\正射提取.gdb',
    cmpStock: 'D:\\存量\\城南存量.gdb',
    cmpTif: 'D:\\DOM\\城南片区_0.05m.tif'
  };

  function demoTick() {
    if (!AVT.pages) return;
    ['car', 'dom', 'cmp'].forEach(function (k) {
      if (AVT.pages[k] && AVT.pages[k].demoApply) AVT.pages[k].demoApply();
    });
  }

  function bindDemo() {
    var bar = el('demoBar');
    if (!bar) return;
    bar.hidden = false;

    Object.keys(DEMO_PATHS).forEach(function (id) {
      var n = el(id);
      if (!n) return;
      n.value = DEMO_PATHS[id];
      n.dispatchEvent(new Event('input'));
    });
    /* 触发一次数据源扫描，让提示行显示配对结果 */
    el('dataDir').dispatchEvent(new Event('change'));

    var btns = bar.querySelectorAll('.demo-bar__b');
    for (var i = 0; i < btns.length; i++) {
      (function (b) {
        b.onclick = function () {
          for (var k = 0; k < btns.length; k++) btns[k].classList.remove('is-on');
          b.classList.add('is-on');
          AVT.demo.set(b.getAttribute('data-demo'));
        };
      })(btns[i]);
    }

    demoTick();
    setInterval(demoTick, 600);
  }

  /* ─────────── 启动 ─────────── */
  function boot() {
    if (!AVT.ui || !AVT.pages) return;

    bindNav();
    if (el('toastX')) el('toastX').onclick = U.hideToast;

    /* 支持 #car / #dom / #cmp 深链直达（也便于评审与截图） */
    var hash = String(location.hash || '').replace('#', '');
    if (/^(car|dom|cmp)$/.test(hash)) {
      var nav = document.querySelector('.nav__i[data-tab="' + hash + '"]');
      if (nav) nav.click();
    }

    AVT.pages.car.init();
    AVT.pages.dom.init();
    AVT.pages.cmp.init();

    if (AVT.isDemo && AVT.demo) {
      bindDemo();
      modelStatus();
    } else {
      window.addEventListener('pywebviewready', modelStatus);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window.AVT = window.AVT || {});
