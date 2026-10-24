# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "AI-Vision-Tool"
ROOT = SPECPATH  # PyInstaller 执行 spec 时不提供 __file__，用 SPECPATH（spec 所在目录）
MODELS = os.path.join(ROOT, "models")
ICON_DIR = os.path.join(ROOT, "assets")
ICON_ICO = os.path.join(ICON_DIR, "app.ico")
ICON_ICNS = os.path.join(ICON_DIR, "app.icns")

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
        "lxml", "openpyxl",
    ],
)

# cv2 / numpy / onnxruntime / webview / shapely 均有官方/贡献内置 hook，
# 会在 Analysis 阶段自动收集其二进制（.dylib/.so/.dll）与子模块，无需手动 collect_all。
# 手动 collect_all 会把 webview/lib/runtimes（目录）等误判为二进制导致报错，
# 因此这里仅补充 hiddenimports 以覆盖动态后端/子模块，二进制交给内置 hook。
for pkg in ["webview", "shapely", "onnxruntime", "cv2", "numpy",
            "geopandas", "pyogrio", "pandas", "pyproj"]:
    try:
        a.hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# pyogrio 自带 GDAL 数据（gdal_data/proj_data），OpenFileGDB 驱动与坐标系定义依赖它们；
# 官方 hooks-contrib 尚无 hook-pyogrio。注意：本版本 collect_data_files 对 pyogrio 子目录
# 的 dest 计算有缺陷（所有文件挤到同一目标路径），因此手动遍历收集，保留子目录结构。
try:
    import pyogrio as _pyogrio
    _pkgdir = os.path.dirname(_pyogrio.__file__)
    for _sub in ("gdal_data", "proj_data"):
        _root = os.path.join(_pkgdir, _sub)
        for _dp, _dns, _fns in os.walk(_root):
            for _fn in _fns:
                _src = os.path.join(_dp, _fn)
                _dest = os.path.join("pyogrio", os.path.relpath(_src, _pkgdir))
                a.datas.append((_dest, _src, "DATA"))
except Exception:
    pass

# 本项目 pipeline 包全量子模块强制收集：防止个别模块经动态导入
# （__import__/importlib）被 PyInstaller 静态分析漏掉
try:
    a.hiddenimports += collect_submodules("pipeline")
except Exception:
    pass

pyz = PYZ(a.pure)
# 标准 onedir 布局：EXE 不内嵌二进制/数据（exclude_binaries=True），
# 所有二进制与数据统一交给 COLLECT 落到 _internal 目录，避免 EXE/COLLECT 重复嵌入导致
# macOS “Resource not a valid file” 与 Windows “needs to create a directory ... but a file exists”。
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME, debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    icon=ICON_ICO,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)

app = BUNDLE(
    coll, name=APP_NAME + ".app",
    bundle_identifier="com.zhongxiang.aivisiontool",
    icon=ICON_ICNS,
    info_plist={
        "NSHighResolutionCapable": True,
        "CFBundleName": "AI Vision Tool",
        "CFBundleDisplayName": "AI Vision Tool",
    },
)
