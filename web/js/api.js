/* ============================================================
   api.js — 后端桥接（pywebview js api）
   契约固定 14 个方法，本文件只做调用封装，不改签名：
   ping / model_status / pick_folder / pick_file / open_folder /
   scan_directory / start_pipeline / get_status / start_dom /
   get_dom_status / start_compare / get_cmp_status /
   preview_image / read_text_file
   ============================================================ */
(function (AVT) {
  'use strict';

  function ready() {
    return !!(window.pywebview && window.pywebview.api);
  }

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    if (!ready()) return Promise.reject(new Error('backend not ready'));
    var fn = window.pywebview.api[name];
    if (typeof fn !== 'function') return Promise.reject(new Error('unknown api: ' + name));
    return Promise.resolve(fn.apply(window.pywebview.api, args));
  }

  /* 选择器：结果直接写入目标输入框 */
  function pickInto(inputId, kind, title, exts) {
    var p = kind === 'file'
      ? call('pick_file', title, exts || [])
      : call('pick_folder', title);
    return p.then(function (r) {
      if (r && r.ok && r.path) {
        AVT.ui.el(inputId).value = r.path;
        /* 程序化赋值不会触发 input 事件 —— 必须手动派发，
           否则「就绪态」校验不刷新：选完目录后主按钮仍是灰的 */
        try {
          AVT.ui.el(inputId).dispatchEvent(new Event('input', { bubbles: true }));
        } catch (e) { /* noop */ }
        return r.path;
      }
      if (r && r.error) {
        AVT.ui.toast((kind === 'file' ? '无法打开文件选择器：' : '无法打开目录选择器：') + r.error, 'err');
      }
      return null;
    }).catch(function (e) {
      AVT.ui.toast((kind === 'file' ? '无法打开文件选择器：' : '无法打开目录选择器：') + e, 'err');
      return null;
    });
  }

  /* 打开目录 */
  function openDir(p) {
    if (!p) { AVT.ui.toast('目录不存在，请先运行任务', 'err'); return Promise.resolve(false); }
    return call('open_folder', p).then(function (r) {
      if (r && !r.ok) AVT.ui.toast(r.error || '打开失败', 'err');
      return true;
    }).catch(function (e) {
      AVT.ui.toast('打开失败：' + e, 'err');
      return false;
    });
  }

  AVT.api = {
    ready: ready,
    call: call,
    pickInto: pickInto,
    openDir: openDir,
    pickDir: function (id, title) { return pickInto(id, 'dir', title); },
    pickFile: function (id, title, exts) { return pickInto(id, 'file', title, exts); }
  };
})(window.AVT = window.AVT || {});
