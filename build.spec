# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

APP_NAME = "AI-Vision-Tool"
ROOT = SPECPATH  # PyInstaller 执行 spec 时不提供 __file__，用 SPECPATH（spec 所在目录）
MODELS = os.path.join(ROOT, "models")

a = Analysis(
    ["app.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[
        ("web", "web"),
        (os.path.join(MODELS, "yolo11n.onnx"), "models"),
    ],
    hiddenimports=[],
    excludes=[
        "torch", "torchvision", "ultralytics", "matplotlib",
        "PyQt5", "PySide6", "notebook", "IPython",
    ],
)

# 各第三方包（cv2 / numpy / geopandas / shapely / onnxruntime / webview）均有官方/贡献内置 hook，
# 会在 Analysis 阶段自动收集它们的二进制（.dylib/.so/.dll）与必要子模块，无需手动 collect_all。
# 手动 collect_all 会把 webview/lib/runtimes（目录）等误判为二进制，导致打包阶段报错，
# 因此这里只补充 hiddenimports，避免漏掉动态后端/子模块。
for pkg in ["webview", "pyogrio", "geopandas", "shapely", "onnxruntime", "cv2", "numpy"]:
    try:
        a.hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# pyogrio 依赖 GDAL 数据文件（proj.db / gdal 数据）。这些“数据文件”不会被 import 跟踪自动收集，
# 必须显式收集，否则运行时 geopandas/pyogrio 导出 SHP / FileGDB 会因找不到 GDAL_DATA 而失败。
# 仅收集“数据文件”，跳过目录与共享库（共享库交由 Analysis/内置 hook 以二进制方式收集，避免冲突）。
_LIB_EXT = (".so", ".dylib", ".dll", ".pyd")
try:
    for d in collect_data_files("pyogrio"):
        src = d[0]
        if os.path.isdir(src):
            continue
        if os.path.splitext(src)[1].lower() in _LIB_EXT:
            continue
        a.datas.append((d[1], src, "DATA"))   # d = (source, dest) -> (dest, source, DATA)
except Exception:
    pass

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=APP_NAME, debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)

app = BUNDLE(
    coll, name=APP_NAME + ".app",
    bundle_identifier="com.zhongxiang.aivisiontool",
    info_plist={
        "NSHighResolutionCapable": True,
        "CFBundleName": "AI Vision Tool",
        "CFBundleDisplayName": "AI Vision Tool",
    },
)
