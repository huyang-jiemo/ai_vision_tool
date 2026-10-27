"""pywebview 桌面入口：双击打开即用，零终端依赖（打包后）。"""
import os
import sys

import webview

from api import Api


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def frozen_selftest():
    """冻结环境自检（发布验证用，正常用户不触发）：
    AVT_SELFTEST=gdb 验证 GDB 导出依赖；=rasterio 验证正射影像读取依赖；
    =dom 验证标线提取；=deps 验证 pyproj/geopandas/pyogrio 依赖链。"""
    mode = os.environ.get("AVT_SELFTEST", "")
    if mode == "gdb":
        import tempfile
        from pipeline.exporter import export_points_gdb
        feats = [{"x": 500000.0, "y": 3000000.0, "class_id": "2", "class_cn": "自检",
                  "session": "S", "image": "I", "dist": 0.0, "score": 0.9}]
        p = os.path.join(tempfile.mkdtemp(prefix="avt_selftest_"), "t.gdb")
        ok = export_points_gdb(p, "detections", feats)
        print("GDB_SELFTEST:", "PASS" if ok and os.path.exists(p) else "FAIL")
        return True
    if mode == "rasterio":
        try:
            import numpy as np
            import rasterio
            from rasterio.transform import from_origin
            with rasterio.MemoryFile() as mf:
                with mf.open(driver="GTiff", height=10, width=10, count=1,
                             dtype="uint8", crs="EPSG:3857",
                             transform=from_origin(0, 10, 1, 1)) as ds:
                    ds.write(np.zeros((1, 10, 10), dtype="uint8"))
            print("RASTERIO_SELFTEST: PASS")
        except Exception as e:
            print("RASTERIO_SELFTEST: FAIL", e)
        return True
    if mode == "deps":
        # 验证 GIS 依赖链（pyproj → geopandas/pyogrio）在冻结环境可用：
        # 曾因 libproj 多副本冲突导致 pyproj 加载失败、正射提取直接中断
        try:
            import pyproj
            from pyproj import CRS
            import geopandas
            import pyogrio
            crs = CRS.from_epsg(3857)
            gdf = geopandas.GeoDataFrame({"a": [1]}, geometry=geopandas.points_from_xy([0], [0]),
                                         crs=crs)
            ok = gdf.crs is not None and gdf.crs.to_epsg() == 3857
            print("DEPS_SELFTEST:", "PASS" if ok else "FAIL",
                  f"(pyproj={pyproj.__version__}, gpd={geopandas.__version__}, "
                  f"pyogrio={pyogrio.__version__}, crs={gdf.crs})")
        except Exception as e:
            print("DEPS_SELFTEST: FAIL", type(e).__name__, e)
        return True
    if mode == "dom":
        # 验证正射影像（BigTIFF）读取与标线提取在打包环境下可用
        try:
            import numpy as np
            import rasterio
            from rasterio.windows import Window
            from pipeline.dom_extractor import DomExtractor
            tif = os.environ.get("AVT_SELFTEST_TIF", "")
            if not tif or not os.path.exists(tif):
                print("DOM_SELFTEST: SKIP (no tif)")
                return True
            dom = rasterio.open(tif)
            cx, cy = dom.width // 2, dom.height // 2
            win = Window(max(0, cx - 1024), max(0, cy - 1024), 2048, 2048)
            img = dom.read([1, 2, 3], window=win).transpose(1, 2, 0)
            wm, ym, road = DomExtractor.masks(img)
            n = int(wm.sum() + ym.sum() + road.sum())
            print("DOM_SELFTEST:", "PASS" if n > 0 else "FAIL", f"(mask_px={n}, size={dom.width}x{dom.height})")
        except Exception as e:
            print("DOM_SELFTEST: FAIL", e)
        return True
    return False


def main():
    if frozen_selftest():
        return
    api = Api()
    webview.create_window(
        title="",   # 不显示原生标题文字
        url=resource_path("web/index.html"),
        js_api=api,
        width=1360,
        height=900,
        min_size=(1080, 700),
        background_color="#F2F3F5",
    )
    webview.start(debug=("--debug" in sys.argv),
                  gui="cocoa" if sys.platform == "darwin" else None)


if __name__ == "__main__":
    main()
