"""pywebview 桌面入口：双击打开即用，零终端依赖（打包后）。"""
import os
import sys

import webview

from api import Api


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def main():
    api = Api()
    webview.create_window(
        title="AI Vision Tool - 众源感知自动解译工具",
        url=resource_path("web/index.html"),
        js_api=api,
        width=1280,
        height=860,
        min_size=(960, 640),
        background_color="#f7f8fa",
    )
    webview.start(debug=("--debug" in sys.argv),
                  gui="cocoa" if sys.platform == "darwin" else None)


if __name__ == "__main__":
    main()
