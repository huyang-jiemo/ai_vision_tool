#!/usr/bin/env python3
"""build_preview.py — 由 web/ 生成单文件预览副本 web/preview.html

用途：不启动桌面壳、不依赖后端时，直接用浏览器打开 web/preview.html 查看界面演示态
（js/mock.js 提供演示数据）。发布产物与打包均不使用 preview.html。

用法：python3 tools/build_preview.py
CSS 顺序取自 index.html 的 link 声明；JS 顺序为 mock.js + index.html 的 script 声明。
逐字内联，不改逻辑；各段带路径分隔注释。
"""
import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

HEADER = """<!DOCTYPE html>
<!--
  preview.html — 由 web/ 自动生成的【单文件预览副本】
  已内联 index.html 声明的全部 CSS 与 JS，并额外加入 js/mock.js（演示数据）。
  用途：双击即可在任意浏览器打开查看演示态，无需服务器、无需其他文件。
  ⚠ 禁止直接编辑本文件 —— 源码以 web/ 为准，修改后执行 python3 tools/build_preview.py 重新生成。
-->
"""


def read(rel):
    return io.open(WEB / rel, encoding="utf-8").read().rstrip("\n")


def main():
    html = read("index.html")
    css_list = re.findall(r'<link rel="stylesheet" href="(css/[^"]+)"', html)
    js_declared = re.findall(r'<script src="(js/[^"]+)"></script>', html)
    assert css_list, "index.html 中未找到 CSS link"
    assert js_declared, "index.html 中未找到 JS script"
    js_list = ["js/mock.js"] + [p for p in js_declared if p != "js/mock.js"]

    style_tag = "<style>\n%s\n</style>" % "\n\n".join(
        "/* ===== %s ===== */\n%s" % (p, read(p)) for p in css_list)
    script_tag = "<script>\n%s\n</script>" % "\n\n".join(
        "/* ===== %s ===== */\n%s" % (p, read(p)) for p in js_list)

    link_re = re.compile(r"(?:[ \t]*<link rel=\"stylesheet\" href=\"css/[^\"]+\"[ \t]*/>\n)+")
    html, n_link = link_re.subn(lambda m: style_tag + "\n", html, count=1)
    assert n_link == 1, "未找到 CSS link 块"

    s_re = re.compile(r"(?:[ \t]*<script src=\"js/[^\"]+\"></script>\n)+")
    html, n_js = s_re.subn(lambda m: script_tag + "\n", html, count=1)
    assert n_js == 1, "未找到 JS script 块"

    html = re.sub(r"^<!DOCTYPE html>\s*", "", html, count=1)
    out = HEADER + html
    if not out.endswith("\n"):
        out += "\n"
    (WEB / "preview.html").write_text(out, encoding="utf-8")

    size = (WEB / "preview.html").stat().st_size
    print("web/preview.html 已生成：%d 字节（%.1f KB）" % (size, size / 1024.0))
    print("  内联 CSS %d 个 / JS %d 个" % (len(css_list), len(js_list)))
    for p in css_list + js_list:
        assert read(p) in out, "内联不完整：%s" % p
    print("  完整性自检通过")


if __name__ == "__main__":
    main()
