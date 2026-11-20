# -*- mode: python ; coding: utf-8 -*-
"""单文件启动器打包（第一步）：打出一个十几 MB 的 onefile 启动器。

payload.zip 不放进 datas —— 否则 PyInstaller 每次启动都会把约 600 MB 解压到
临时目录。实际流程是：
    pyinstaller build.spec --noconfirm            # onedir 主程序 → dist/AIVisionTool/
    python tools/make_payload.py                  # → build/payload.zip / payload.version / splash.png
    pyinstaller build_launcher.spec --noconfirm --distpath dist_single
    python tools/make_single_exe.py               # 把 payload 追加到启动器末尾 → 最终单文件 exe

PyInstaller 6 的引导程序从文件末尾向前搜索 CArchive cookie，因此追加在 exe
之后的数据不影响启动。
"""
import os
import sys

APP_NAME = "AIVisionTool"
ROOT = SPECPATH          # PyInstaller 执行 spec 时无 __file__，用 SPECPATH
VERSION = os.path.join(ROOT, "build", "payload.version")
WIN_VERSION = os.path.join(ROOT, "build", "win_version.txt")
SPLASH_PNG = os.path.join(ROOT, "build", "splash.png")
ICON = os.path.join(ROOT, "assets", "app.ico")
version_resource = WIN_VERSION if sys.platform == "win32" and os.path.exists(WIN_VERSION) else None

if not os.path.exists(VERSION):
    raise SystemExit(
        f"缺少 {VERSION}\n请先执行 pyinstaller build.spec 与 python tools/make_payload.py")

a = Analysis(
    ["launcher.py"],
    pathex=[ROOT],
    binaries=[],
    # 只带一个纯文本版本号；payload.zip 由 tools/make_single_exe.py 追加
    datas=[(VERSION, ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 启动器只用标准库（splash 需要 tkinter），把主程序的重依赖全部排除，
    # 避免 PyInstaller 依赖分析把它们误收进来。
    excludes=[
        "numpy", "cv2", "onnxruntime", "geopandas", "pandas", "pyogrio",
        "rasterio", "pyproj", "shapely", "webview", "PIL", "ultralytics",
        "torch", "torchvision", "matplotlib", "pytest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# splash 仅 Windows 支持（PyInstaller 在 macOS 会直接报错）
use_splash = sys.platform == "win32" and os.path.exists(SPLASH_PNG)
if use_splash:
    splash = Splash(
        SPLASH_PNG,
        binaries=a.binaries,
        datas=a.datas,
        text_pos=(160, 240),      # 文字左下角，位于分隔线下方
        text_size=12,
        text_color="#6B7280",
        text_default="0%",
        always_on_top=True,
    )
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, splash, splash.binaries, [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
        version=version_resource,
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
        version=version_resource,
    )
