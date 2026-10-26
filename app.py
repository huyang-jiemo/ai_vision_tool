"""pywebview 桌面入口：双击打开即用，零终端依赖（打包后）。"""
import os
import sys

import webview

from api import Api


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def frozen_selftest():
    """冻结环境自检（发布验证用，正常用户不触发）：
    AVT_SELFTEST=gdb 时验证 GDB 导出依赖在打包产物内完整可用。"""
    if os.environ.get("AVT_SELFTEST") != "gdb":
        return False
    import tempfile
    from pipeline.exporter import export_points_gdb
    feats = [{"x": 500000.0, "y": 3000000.0, "class_id": "2", "class_cn": "自检",
              "session": "S", "image": "I", "dist": 0.0, "score": 0.9}]
    p = os.path.join(tempfile.mkdtemp(prefix="avt_selftest_"), "t.gdb")
    ok = export_points_gdb(p, "detections", feats)
    print("GDB_SELFTEST:", "PASS" if ok and os.path.exists(p) else "FAIL")
    return True


def main():
    if frozen_selftest():
        return
    api = Api()
    webview.create_window(
        title="",   # 不显示原生标题文字
        url=resource_path("web/index.html"),
        js_api=api,
        width=1360,
        height=900,
        min_size=(1080, 700),
        background_color="#F2F3F5",
    )
    webview.start(debug=("--debug" in sys.argv),
                  gui="cocoa" if sys.platform == "darwin" else None)


if __name__ == "__main__":
    main()
