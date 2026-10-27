# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "AIVisionTool"
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

# 关键修复：多个包（rasterio / pyproj）捆绑同名 libproj.25.9.8.1.dylib，但两者是
# 不同构建：rasterio 版导出 GDAL 需要的 _internal_* 符号，pyproj 版导出公共 API
# _proj_context_create。二者不可互换（早前统一替换成 rasterio 版，导致打包后
# 正射提取报 "Symbol not found: _proj_context_create" 直接中断）。
# 处理方式：① 顶层副本用 rasterio 版（rasterio 的 GDAL 依赖 _internal_geod_init）；
# ② pyproj 目录内保留 pyproj 自己的库；③ 把 pyproj 各扩展的 @rpath/libproj 改成
# @loader_path/.dylibs/libproj，使其锁定到 pyproj 目录内的副本。②③ 在 COLLECT 之后
# 直接操作落盘文件（PyInstaller 会按 basename 去重，只能在产物上做）。
try:
    import glob as _glob, shutil as _shutil, subprocess as _sp, sys as _sys


    def _fix_libproj(dist_dir):
        if _sys.platform != "darwin" or not dist_dir or not os.path.isdir(dist_dir):
            return
        import pyproj as _pj, rasterio as _rio3
        pj_libs = sorted(_glob.glob(os.path.join(os.path.dirname(_pj.__file__),
                                                 ".dylibs", "libproj*.dylib")))
        rio_libs = sorted(_glob.glob(os.path.join(os.path.dirname(_rio3.__file__),
                                                  ".dylibs", "libproj*.dylib")))
        if not pj_libs or not rio_libs:
            return
        base = os.path.join(dist_dir, "_internal")
        if not os.path.isdir(base):
            base = dist_dir
        name = os.path.basename(rio_libs[0])
        # ① 顶层：真实文件（rasterio 版），替换掉指向 pyproj 目录的符号链接
        top = os.path.join(base, name)
        try:
            if os.path.islink(top) or os.path.isfile(top):
                os.remove(top)
            _shutil.copy2(rio_libs[0], top)
        except OSError:
            pass
        # ② pyproj 目录内的副本恢复为 pyproj 自己的构建
        for sub in (".dylibs", "__dot__dylibs"):
            d = os.path.join(base, "pyproj", sub)
            if os.path.isdir(d):
                try:
                    _shutil.copy2(pj_libs[0], os.path.join(d, name))
                except OSError:
                    pass
        # ③ pyproj 扩展锁定到自己的副本
        for so in _glob.glob(os.path.join(base, "pyproj", "*.so")):
            _sp.run(["install_name_tool", "-change",
                     "@rpath/" + name, "@loader_path/.dylibs/" + name, so],
                    check=False, capture_output=True)
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

# COLLECT 落盘后先修正一次（onedir），BUNDLE 之后还要对 .app 再修一次，
# 因为 BUNDLE 组包时会重建指向 pyproj 目录的 libproj 符号链接
try:
    _fix_libproj(os.path.join(ROOT, "dist", APP_NAME))
except Exception:
    pass

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

# BUNDLE 之后：对 .app 内的 Frameworks 再做一次 libproj 修正，并重新 ad-hoc 签名
try:
    _app_dir = os.path.join(ROOT, "dist", APP_NAME + ".app")
    _fix_libproj(os.path.join(_app_dir, "Contents", "Frameworks"))
    import subprocess as _sp2
    _sp2.run(["codesign", "--force", "--deep", "--sign", "-", _app_dir],
             check=False, capture_output=True)
except Exception:
    pass

