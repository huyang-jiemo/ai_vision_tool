# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

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
        # FileGDB 导出依赖 GDAL/pyogrio/geopandas，PyInstaller 打包跨平台极不稳定，
        # 且 exporter.py 中 GDB 导出已用 try/except 包裹、失败可降级（SHP 仍可用），
        # 因此从冻结包中剔除，保证安装包可稳定构建与运行；FileGDB 作为已知限制后续再补。
        "geopandas", "pyogrio",
        # pandas/lxml/openpyxl 仅被 GDB 导出路径的 _gdf() 懒加载使用，随 geopandas 一起剔除，
        # 可减小安装包约 60-80MB 且不影响 SHP 导出与主流程。
        "pandas",
    ],
)

# cv2 / numpy / onnxruntime / webview / shapely 均有官方/贡献内置 hook，
# 会在 Analysis 阶段自动收集其二进制（.dylib/.so/.dll）与子模块，无需手动 collect_all。
# 手动 collect_all 会把 webview/lib/runtimes（目录）等误判为二进制导致报错，
# 因此这里仅补充 hiddenimports 以覆盖动态后端/子模块，二进制交给内置 hook。
for pkg in ["webview", "shapely", "onnxruntime", "cv2", "numpy"]:
    try:
        a.hiddenimports += collect_submodules(pkg)
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
