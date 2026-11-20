"""把 onedir 构建结果打成单文件启动器所需的 payload.zip（含 splash 图）。

前置：先完成 PyInstaller onedir 构建（build.spec），产物在 dist/AIVisionTool/。
用法：
    python tools/make_payload.py                 # 版本取 web/index.html + git SHA
    python tools/make_payload.py --version v1.2.3
产物：
    build/payload.zip        启动器随包（解压到 %LOCALAPPDATA% 的运行时）
    build/payload.version    版本目录名（版本-git短码-内容摘要，新版本解压到新目录）
    build/splash.png         启动器首启解压时的启动画面（无 CJK 字体时跳过）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "AIVisionTool"
BUILD = ROOT / "build"
PAYLOAD = BUILD / "payload.zip"
VERSION_FILE = BUILD / "payload.version"
SPLASH = BUILD / "splash.png"
WIN_VERSION = BUILD / "win_version.txt"     # PyInstaller EXE 的版本资源（Windows）

SPLASH_SIZE = (560, 300)
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",                                  # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _repo_version() -> str:
    try:
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        m = re.search(r'brand__ver">\s*(v[0-9][0-9.]*)\s*<', html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "dev"


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _digest(path: Path, n: int = 8) -> str:
    """payload 内容摘要：同一版本重打包时也能触发重新解压，避免用到旧文件。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def _find_font():
    """返回 (字体三元组, 是否支持中文)。找不到 CJK 字体时退回默认字体 + 英文文案。"""
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return (ImageFont.truetype(path, 24),
                        ImageFont.truetype(path, 15),
                        ImageFont.truetype(path, 13)), True
            except Exception:
                continue
    try:
        return (ImageFont.load_default(),) * 3, False
    except Exception:
        return None, False


def make_win_version(version: str) -> None:
    """生成 PyInstaller 的 Windows 版本资源，exe 属性页可看到产品名与版本。"""
    BUILD.mkdir(parents=True, exist_ok=True)
    nums = [int(x) for x in re.findall(r"\d+", version)[:4]]
    nums = (nums + [0, 0, 0, 0])[:4]
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple(nums)}, prodvers={tuple(nums)},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('FileDescription', '众源感知提取工具'),
        StringStruct('FileVersion', '{'.'.join(map(str, nums))}'),
        StringStruct('InternalName', 'AIVisionTool'),
        StringStruct('OriginalFilename', 'AIVisionTool.exe'),
        StringStruct('ProductName', '众源感知提取工具'),
        StringStruct('ProductVersion', '{version}')]),
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    WIN_VERSION.write_text(text, encoding="utf-8")
    print(f"[ok] 版本资源: {WIN_VERSION}")


def make_splash(version: str = "") -> bool:
    """生成首启启动画面；缺少 Pillow/字体时返回 False（构建将不带 splash）。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        print("[skip] 未安装 Pillow，跳过 splash")
        return False
    BUILD.mkdir(parents=True, exist_ok=True)
    fonts, has_cjk = _find_font()
    if fonts is None:
        return False
    title_font, sub_font, note_font = fonts

    img = Image.new("RGB", SPLASH_SIZE, (242, 243, 245))
    d = ImageDraw.Draw(img)

    # 应用图标（与界面左上角一致）
    icon_path = ROOT / "assets" / "icon.png"
    try:
        icon = Image.open(icon_path).convert("RGBA").resize((96, 96), Image.LANCZOS)
        img.paste(icon, (40, 72), icon)
    except Exception:
        d.rounded_rectangle([40, 72, 136, 168], radius=22, fill=(62, 99, 216))

    title = "众源感知提取工具" if has_cjk else "AI Vision Tool"
    sub = ("首次启动正在准备运行文件，请稍候…" if has_cjk
           else "Preparing runtime files for first launch...")
    if has_cjk:
        note = (f"版本 {version} · 仅首次需要，之后启动更快" if version
                else "仅首次需要，之后启动更快")
    else:
        note = (f"Version {version} - first launch only" if version
                else "First launch only")
    d.text((160, 74), title, font=title_font, fill=(31, 41, 55))
    d.text((162, 116), sub, font=sub_font, fill=(107, 114, 128))
    d.text((162, 144), note, font=note_font, fill=(156, 163, 175))
    d.line([(40, 200), (520, 200)], fill=(229, 231, 235), width=1)
    img.save(SPLASH)
    print(f"[ok] splash: {SPLASH} ({SPLASH.stat().st_size / 1024:.0f} KB)")
    return True


def make_payload(version: str) -> str:
    exe_names = ["AIVisionTool.exe", "AIVisionTool"]
    if not any((SRC / n).exists() for n in exe_names):
        raise SystemExit(
            f"未找到 onedir 构建产物：{SRC}\n请先执行 pyinstaller build.spec --noconfirm")
    BUILD.mkdir(parents=True, exist_ok=True)
    if PAYLOAD.exists():
        PAYLOAD.unlink()
    files = sorted(p for p in SRC.rglob("*") if p.is_file())
    print(f"[..] 打包 {len(files)} 个文件 → {PAYLOAD}")
    # level=6：DEFLATE 解压速度与压缩级别基本无关，而 payload 要随 exe 一起
    # 拷贝/分发，优先压得更小（首启耗时主要花在写入 1.3 GB 文件上）。
    with zipfile.ZipFile(PAYLOAD, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, p in enumerate(files, 1):
            zf.write(p, p.relative_to(SRC).as_posix())
            if i % 400 == 0 or i == len(files):
                print(f"     {i}/{len(files)}")
    final_version = f"{version}-{_digest(PAYLOAD)}"
    VERSION_FILE.write_text(final_version, encoding="utf-8")
    print(f"[ok] payload: {PAYLOAD.stat().st_size / 1048576:.1f} MB")
    print(f"[ok] version: {final_version}")
    return final_version


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="")
    ap.add_argument("--no-splash", action="store_true")
    args = ap.parse_args()

    base = args.version.strip() or _repo_version()
    version = base
    sha = _git_sha()
    if sha and not version.endswith(sha):
        version = f"{version}-{sha}"

    make_payload(version)
    make_win_version(base)
    if not args.no_splash:
        make_splash(base)
    else:
        SPLASH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
