# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

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
    hiddenimports=collect_submodules("webview"),
    excludes=[
        "torch", "torchvision", "ultralytics", "matplotlib",
        "PyQt5", "PySide6", "notebook", "IPython",
    ],
)

for pkg in ["webview", "pyogrio", "onnxruntime", "cv2", "numpy", "shapely", "geopandas"]:
    try:
        datas, binaries, hiddenimports = collect_all(pkg)
        for d in datas:                       # datas: (dest, src)
            a.datas.append((d[0], d[1]))
        for b in binaries:                    # 规整为 3 元组 (dest, src, typecode)
            if len(b) == 2:
                b = (b[0], b[1], "B")
            a.binaries.append((b[0], b[1], b[2]))
        a.hiddenimports += hiddenimports
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
