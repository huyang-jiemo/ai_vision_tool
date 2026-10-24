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

# 把第三方包的二进制与数据文件打进包内。
# collect_all 返回：
#   datas    -> 2 元组 (source, dest)
#   binaries -> 2 元组 (source, dest_dir)
# 必须规整成 PyInstaller TOC 要求的 3 元组 (dest, source, typecode)，
# 否则 EXE 阶段的 normalize_toc 会因 “not enough values to unpack (expected 3, got 2)” 报错。
# cv2 / numpy 有官方内置 hook 负责各自的 .dylibs/.so，故不在此手动 collect（避免其 datas 目录冲突）。
for pkg in ["webview", "pyogrio", "onnxruntime", "shapely", "geopandas"]:
    try:
        datas, binaries, hiddenimports = collect_all(pkg)
        for d in datas:                       # d = (source, dest)
            if os.path.isdir(d[0]):           # 跳过 .dist-info 等目录，避免 IsADirectoryError
                continue
            a.datas.append((d[1], d[0], "DATA"))
        for b in binaries:                    # b = (source, dest_dir)
            a.binaries.append((b[1], b[0], "BINARY"))
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
