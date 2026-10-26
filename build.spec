# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "AI-Vision-Tool"
ROOT = SPECPATH  # PyInstaller 执行 spec 时不提供 __file__，用 SPECPATH（spec 所在目录）
MODELS = os.path.join(ROOT, "models")
ICON_DIR = os.path.join(ROOT, "assets")
ICON_ICO = os.path.join(ICON_DIR, "app.ico")
ICON_ICNS = os.path.join(ICON_DIR, "app.icns")

# rasterio 子模块大量延迟导入（serde/crs/transform 等），必须在 Analysis 之前
# 就把 hiddenimports 备好——Analysis 之后追加 a.hiddenimports 不会生效
RIO_IMPORTS = [
    "rasterio.serde", "rasterio.crs", "rasterio.transform", "rasterio.windows",
    "rasterio.enums", "rasterio.coords", "rasterio.dtypes", "rasterio.path",
    "rasterio.vrt", "rasterio.mask", "rasterio.merge", "rasterio.sample",
    "rasterio.features", "rasterio.warp", "rasterio.errors", "rasterio.profiles",
    "rasterio.rpc", "rasterio.session", "rasterio.control", "rasterio.meta",
    "rasterio.nodata", "rasterio.rgb", "rasterio.stack", "rasterio.tools",
    "rasterio.plot", "rasterio.drivers", "rasterio.env",
]

a = Analysis(
    ["app.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[
        ("web", "web"),
        (os.path.join(MODELS, "yolo11n.onnx"), "models"),
    ],
    hiddenimports=[
        # pyogrio 的二进制扩展模块：_geometry 等为函数内延迟导入，
        # PyInstaller 静态分析可能遗漏（曾导致打包后 GDB 导出报
        # "No module named 'pyogrio._geometry'"），此处显式声明
        "pyogrio", "pyogrio._geometry", "pyogrio._io", "pyogrio._ogr",
        "pyogrio._err", "pyogrio._vsi",
        # rasterio 二进制扩展（正射影像读取，函数内延迟导入）
        "rasterio", "rasterio._io", "rasterio._features", "rasterio._warp",
        "rasterio._err", "rasterio._crs", "rasterio.controls",
        "rasterio.shutil", "rasterio._version",
    ] + RIO_IMPORTS,
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
# 注：rasterio 子模块已在 Analysis 前通过 RIO_IMPORTS 传入，此处循环仅作
# 其他包的兜底（分析后追加对已完成的 Analysis 无效，保留以兼容原逻辑）
for pkg in ["webview", "shapely", "onnxruntime", "cv2", "numpy",
            "geopandas", "pyogrio", "pandas", "pyproj"]:
    try:
        a.hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# rasterio 自带的 GDAL/PROJ 数据与二进制库（rasterio.libs）：贡献 hook 只收集部分，
# 显式补齐 gdal_data/proj_data；.libs 由 PyInstaller 二进制依赖分析自动带入
try:
    import rasterio as _rio
    _pkgdir = os.path.dirname(_rio.__file__)
    for _sub in ("gdal_data", "proj_data"):
        _root = os.path.join(_pkgdir, _sub)
        if not os.path.isdir(_root):
            continue
        for _dp, _dns, _fns in os.walk(_root):
            for _fn in _fns:
                _src = os.path.join(_dp, _fn)
                _dest = os.path.join("rasterio", os.path.relpath(_src, _pkgdir))
                a.datas.append((_dest, _src, "DATA"))
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

# 关键修复：多个包（rasterio/pyproj/pyogrio）捆绑同名 libproj.25.9.8.1.dylib，
# COLLECT 去重时 pyproj 的副本会覆盖 rasterio 的副本，而 rasterio 的 GDAL 3.12.2
# 依赖其自带 libproj 独有的符号（_internal_geod_init），导致打包后
# "Symbol not found: _internal_geod_init"。此处强制以 rasterio/.dylibs 的副本为准。
try:
    import glob as _glob
    import rasterio as _rio2
    _rio_dylibs = os.path.join(os.path.dirname(_rio2.__file__), ".dylibs")
    _rio_proj = sorted(_glob.glob(os.path.join(_rio_dylibs, "libproj*.dylib")))
    if _rio_proj:
        _fixed = []
        for _dest, _src, _kind in a.binaries:
            if os.path.basename(_dest).startswith("libproj"):
                _src = _rio_proj[0]
            _fixed.append((_dest, _src, _kind))
        a.binaries = _fixed
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
