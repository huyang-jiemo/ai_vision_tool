#!/usr/bin/env python3
"""build_preview.py — 由 ui-redesign/ 源文件生成单文件预览副本 preview.html
用法：python3 build_preview.py
CSS 顺序：tokens → base → layout → components → pages
JS  顺序：mock → api → ui → pages/car → pages/dom → pages/cmp → app
逐字内联，不改逻辑；各段带路径分隔注释。
"""
import io, os, re, pathlib

ROOT = pathlib.Path(__file__).resolve().parent
CSS = ["css/tokens.css", "css/base.css", "css/layout.css", "css/components.css", "css/pages.css"]
JS = ["js/mock.js", "js/api.js", "js/ui.js", "js/pages/car.js", "js/pages/dom.js", "js/pages/cmp.js", "js/app.js"]

HEADER = """<!DOCTYPE html>
<!--
  preview.html — 由 ui-redesign/ 自动生成的【单文件预览副本】
  已内联：5 个 CSS（tokens→base→layout→components→pages）+ 7 个 JS（mock→api→ui→pages/car→pages/dom→pages/cmp→app）
  用途：双击即可在任意浏览器打开查看演示态，无需服务器、无需其他文件。
  ⚠ 禁止直接编辑本文件 —— 源码以 ui-redesign/ 为准，修改请改源文件后重新生成本文件。
  ⚠ 由 build_preview.py 生成，请勿手工改动。
-->
"""

def read(rel):
    return io.open(ROOT / rel, encoding="utf-8").read().rstrip("\n")

def main():
    html = read("index.html")

    # ── 1. CSS：5 个 link → 一个 <style>（逐字内联 + 路径分隔注释）
    css_blocks = "\n\n".join("/* ===== %s ===== */\n%s" % (p, read(p)) for p in CSS)
    style_tag = "<style>\n%s\n</style>" % css_blocks

    # ── 2. JS：7 个 <script src> → 一个 <script>（逐字内联 + 路径分隔注释）
    js_blocks = "\n\n".join("/* ===== %s ===== */\n%s" % (p, read(p)) for p in JS)
    script_tag = "<script>\n%s\n</script>" % js_blocks

    # ── 3. 替换 head 中的 stylesheet link 块（连续若干行）
    link_re = re.compile(r"(?:[ \t]*<link rel=\"stylesheet\" href=\"css/[^\"]+\"[ \t]*/>\n)+")
    # 注意：用 lambda 形式的 repl，避免 re 把替换串里的反斜杠当转义吃掉（CSS/JS 中的 \\ 必须逐字保留）
    html, n_link = link_re.subn(lambda m: style_tag + "\n", html, count=1)
    assert n_link == 1, "未找到 css link 块（n=%d）" % n_link

    # ── 4. 替换 body 末尾的 script src 块（连续若干行）
    s_re = re.compile(r"(?:[ \t]*<script src=\"js/[^\"]+\"></script>\n)+")
    html, n_js = s_re.subn(lambda m: script_tag + "\n", html, count=1)
    assert n_js == 1, "未找到 js script 块（n=%d）" % n_js

    # ── 5. 去掉原文件头，统一套用预览副本头注释
    html = re.sub(r"^<!DOCTYPE html>\s*", "", html, count=1)
    out = HEADER + html
    if not out.endswith("\n"):
        out += "\n"

    (ROOT / "preview.html").write_text(out, encoding="utf-8")
    size = (ROOT / "preview.html").stat().st_size
    print("preview.html 已生成：%d 字节（%.1f KB）" % (size, size / 1024.0))
    print("  内联 CSS %d 个 / JS %d 个" % (len(CSS), len(JS)))
    # 完整性自检：每个源文件的内容都应原样出现在产物中
    for p in CSS + JS:
        src = read(p)
        assert src in out, "内联缺失：%s" % p
    print("  逐字内联自检：全部 %d 个源文件片段均完整存在于产物中 ✓" % (len(CSS) + len(JS)))

if __name__ == "__main__":
    main()
