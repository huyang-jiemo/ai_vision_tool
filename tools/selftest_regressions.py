"""回归自检：变化检测 / 波段适配 / 分块重叠 / 比对窗口裁剪 / 跨 CRS 比对。

依赖：numpy、rasterio、opencv-python-headless、shapely、pyproj、geopandas
（与运行环境一致）。不依赖真实数据，全部使用合成影像/点要素。

用法：python tools/selftest_regressions.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def _utf8_stdout() -> None:
    """Windows 控制台默认可能是 cp1252/cp936，打印中文会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}".rstrip())


# ---------------------------------------------------------------- 变化检测

def test_change_detector() -> None:
    from pipeline.change_detector import detect_changes

    def pt(x, y, name=""):
        return {"x": x, "y": y, "class_id": 2, "class_cn": "小汽车", "name": name}

    sessions = {
        "20260101": [pt(0, 0, "P1"), pt(100, 0, "P2"), pt(200, 0, "P3")],
        "20260201": [pt(0, 0, "P1"), pt(102, 0, "P2"), pt(300, 0, "P4")],
        "20260301": [pt(0, 0, "P1"), pt(300, 0, "P4"), pt(400, 0, "P5")],
    }
    out = detect_changes(sessions, threshold_m=15)

    def count(a, b, kind):
        return sum(1 for c in out if c["session_a"] == a and c["session_b"] == b
                   and c["change_type"] == kind)

    check("变化检测覆盖第三期", count("20260201", "20260301", "stable") == 2)
    check("变化检测三期新增/消失计数",
          count("20260101", "20260201", "disappeared") == 1
          and count("20260101", "20260201", "appeared") == 1)
    stable = [c for c in out if c["session_a"] == "20260101"
              and c["change_type"] == "stable"]
    centers = {(round(c["x"], 3), round(c["y"], 3)) for c in stable}
    check("stable 一对一（无重复目标）", len(stable) == len(centers) == 2)

    far = detect_changes({"a": [pt(0, 0)], "b": [pt(50, 0)]}, threshold_m=15)
    check("超出阈值不匹配",
          {c["change_type"] for c in far} == {"appeared", "disappeared"})


# ---------------------------------------------------------------- 波段适配

def test_read_rgb() -> None:
    from pipeline.imgio import read_rgb

    class DS:
        def __init__(self, count):
            self.count, self.h, self.w = count, 8, 10

        def read(self, bands, window=None, out_shape=None, resampling=None):
            if out_shape is not None:
                assert out_shape[0] == len(bands), (out_shape, len(bands))
                return np.zeros((len(bands), 5, 6), np.uint8)
            return np.arange(len(bands) * self.h * self.w,
                             dtype=np.uint8).reshape(len(bands), self.h, self.w)

    ok = True
    for n in (1, 2, 3, 4):
        ok &= read_rgb(DS(n)).shape == (3, 8, 10)
        ok &= read_rgb(DS(n), out_shape=(3, 5, 6)).shape == (3, 5, 6)
    check("read_rgb 适配 1/2/3/4 波段", ok)
    a2 = read_rgb(DS(2))
    check("双波段按灰度复制而非使用 alpha",
          np.array_equal(a2[0], a2[1]) and np.array_equal(a2[0], a2[2]))


# ---------------------------------------------------------- 分块重叠 / 灰度

def _write_dom(path: str, img: np.ndarray, crs,
               origin: tuple[float, float] = (500000.0, 3387000.0)) -> None:
    import rasterio
    from rasterio.transform import from_origin
    count = 1 if img.ndim == 2 else img.shape[2]
    with rasterio.open(path, "w", driver="GTiff", height=img.shape[0],
                       width=img.shape[1], count=count, dtype="uint8", crs=crs,
                       transform=from_origin(origin[0], origin[1], 0.1, 0.1)) as ds:
        if count == 1:
            ds.write(img, 1)
        else:
            ds.write(img.transpose(2, 0, 1))


def test_dom_tiles() -> None:
    from pipeline.dom_extractor import DomExtractor
    from shapely.geometry import LineString

    tmp = tempfile.mkdtemp(prefix="dom_tiles_")

    # 3200×600：标线中心分别在第一块内部、第二块左缘 100px（旧逻辑丢弃带）
    img = np.full((600, 3200, 3), 90, np.uint8)
    for cx in (1500, 2148):
        img[296:304, cx - 80:cx + 80] = 235
    tif = os.path.join(tmp, "seam.tif")
    _write_dom(tif, img, "EPSG:4547")
    rep = DomExtractor().run(tif, os.path.join(tmp, "out_seam"), wlog=lambda _s: None)
    check("分块接缝处要素不丢失", rep["lines"] == 2, f"lines={rep['lines']}")

    # 1 波段灰度 DOM
    gray = np.full((400, 900), 90, np.uint8)
    gray[196:204, 300:460] = 235
    gtif = os.path.join(tmp, "gray.tif")
    _write_dom(gtif, gray, "EPSG:4547")
    rep = DomExtractor().run(gtif, os.path.join(tmp, "out_gray"), wlog=lambda _s: None)
    check("1 波段灰度 DOM 可提取", rep["lines"] >= 1, f"lines={rep['lines']}")

    # 跨越两个分块的长直线：overlap 段不得重复输出，合并后应是一条完整线
    long_img = np.full((600, 3000, 3), 90, np.uint8)
    long_img[300:310, 100:2900] = 235
    long_tif = os.path.join(tmp, "long_line.tif")
    _write_dom(long_tif, long_img, "EPSG:4547")
    long_out = os.path.join(tmp, "out_long")
    rep = DomExtractor().run(long_tif, long_out, export_shp=True,
                             wlog=lambda _s: None)
    import geopandas as gpd
    g = gpd.read_file(os.path.join(long_out, "正射提取.gdb"), layer="lanes")
    max_len = float(g["LEN_M"].max()) if len(g) else 0.0
    check("跨分块长线不重复且长度正确",
          rep["lines"] == 1 and 270.0 <= max_len <= 290.0,
          f"lines={rep['lines']} max_len={max_len:.2f}m")
    sg = gpd.read_file(os.path.join(long_out, "shp", "lanes.shp"))
    check("DOM SHP 线层含 LEN_M/SRC 属性",
          {"LEN_M", "SRC"}.issubset(sg.columns) and len(sg) == 1,
          str(list(sg.columns)))

    # 复用输出目录且本次无要素：旧 GDB/SHP 必须清除，报告不能指向旧成果
    empty_out = os.path.join(tmp, "out_empty")
    os.makedirs(empty_out, exist_ok=True)
    stale_gdb = os.path.join(empty_out, "正射提取.gdb")
    stale_geom = (g.geometry.iloc[0] if len(g)
                  else LineString([(0, 0), (1, 1)]))
    gpd.GeoDataFrame(
        {"SID": [1]}, geometry=[stale_geom],
        crs="EPSG:4547").to_file(stale_gdb, layer="lanes", driver="OpenFileGDB")
    shp_dir = os.path.join(empty_out, "shp")
    os.makedirs(shp_dir, exist_ok=True)
    for name in ("lanes.shp", "lanes.dbf", "lanes.qix", "lanes.sbn"):
        with open(os.path.join(shp_dir, name), "wb") as f:
            f.write(b"stale")
    bare = np.full((200, 200, 3), 90, np.uint8)
    bare_tif = os.path.join(tmp, "bare.tif")
    _write_dom(bare_tif, bare, "EPSG:4547")
    brep = DomExtractor().run(bare_tif, empty_out, export_shp=True,
                              wlog=lambda _s: None)
    check("无要素时旧 GDB 已清除且不报告路径",
          brep["lines"] == 0 and not os.path.exists(stale_gdb)
          and brep["gdb"] == "", f"gdb={brep['gdb']!r} lines={brep['lines']}")
    check("无要素时旧 SHP/sidecar 已清除",
          not any(os.scandir(shp_dir)), str(os.listdir(shp_dir)))


# ------------------------------------------------------------ 比对窗口裁剪

def test_comparator_edge() -> None:
    from pyproj import CRS
    from shapely.geometry import Point
    from pipeline.comparator import Comparator
    from pipeline.crs_plan import plan_for_bounds

    tmp = tempfile.mkdtemp(prefix="cmp_edge_")
    img = np.full((400, 400, 3), 180, np.uint8)
    tif = os.path.join(tmp, "dom.tif")
    _write_dom(tif, img, "EPSG:4547")
    # 变化点贴近影像右下角：外扩 40m 的窗口必然越界
    changes = {"new": [("point", "x.shp", Point(500039.5, 3386960.5), "")],
               "missing": [], "modified": []}
    stock = {"lines": [], "areas": [], "points": []}
    plan = plan_for_bounds(CRS.from_epsg(4547), None,
                           (500000, 3386960, 500040, 3387000))
    out = os.path.join(tmp, "preview.png")
    Comparator().render(None, stock, changes, tif, out, plan=plan)
    check("比对预览窗口自动裁剪", os.path.isfile(out))


# ------------------------------------------------------ 跨 CRS 比对 / 缺失 CRS

def test_comparator_cross_crs() -> None:
    import geopandas as gpd
    from pyproj import Transformer
    from shapely.geometry import LineString, Point
    from pipeline.comparator import Comparator

    tmp = tempfile.mkdtemp(prefix="cmp_crs_")
    tf = Transformer.from_crs(4547, 3857, always_xy=True)

    def p3857(x, y):
        return tf.transform(x, y)

    new = os.path.join(tmp, "new.gdb")
    stock = os.path.join(tmp, "stock.gdb")
    # 新成果（4547）：两条线 + 一个点
    gpd.GeoDataFrame(
        {"A": [1, 2]},
        geometry=[LineString([(500000, 3387000), (500050, 3387000)]),
                  LineString([(500000, 3386950), (500050, 3386950)])],
        crs="EPSG:4547").to_file(new, layer="lanes", driver="OpenFileGDB")
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(500010, 3387005)],
        crs="EPSG:4547").to_file(new, layer="signs", driver="OpenFileGDB")
    # 存量（3857）：与新成果第 1 条线/点重合，另有第 3 条线（新成果缺失）
    a = p3857(500000, 3387000) + p3857(500050, 3387000)
    b = p3857(500000, 3386900) + p3857(500050, 3386900)
    pt = p3857(500010, 3387005)
    gpd.GeoDataFrame(
        {"A": [1, 2]},
        geometry=[LineString([a[:2], a[2:]]), LineString([b[:2], b[2:]])],
        crs="EPSG:3857").to_file(stock, layer="lanes", driver="OpenFileGDB")
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(*pt)],
        crs="EPSG:3857").to_file(stock, layer="signs", driver="OpenFileGDB")

    out = os.path.join(tmp, "out")
    res = Comparator().run(new, stock, out)
    check("跨 CRS 比对：输出跟随新成果 = 4547",
          "EPSG:4547" in res.get("target_crs", ""), res.get("target_crs", ""))
    check("跨 CRS 比对：重合线/点匹配，新增线 1、缺失线 1",
          res["totals"]["matched"] == 2 and res["totals"]["new"] == 1
          and res["totals"]["missing"] == 1, str(res["totals"]))
    import pyogrio
    from pyproj import CRS
    info = pyogrio.read_info(os.path.join(out, "变化比对.gdb"),
                             layer="chg_new_lines")
    got = CRS.from_user_input(info["crs"]).to_epsg() if info.get("crs") else None
    check("变化 GDB 图层 CRS = 4547", got == 4547, str(got))


def test_line_coverage_length() -> None:
    """覆盖长度必须按线长计算；缓冲面周长会把 40% 覆盖误判为 82%。"""
    import geopandas as gpd
    from shapely.geometry import LineString
    from pipeline.comparator import Comparator

    cmp = Comparator()
    ref = gpd.GeoSeries([LineString([(0, 0), (100, 0)])], crs="EPSG:4547")
    partial = gpd.GeoSeries([LineString([(0, 0), (40, 0)])], crs="EPSG:4547")
    pairs, new_only, missing, modified = cmp._match_lines(partial, ref)
    check("40% 覆盖不应判为匹配",
          not pairs and len(modified) == 1 and not missing and not new_only,
          f"pairs={pairs} modified={modified} missing={missing} new={new_only}")
    # 两段新线合计覆盖 60%，应视为匹配（多段覆盖）
    two = gpd.GeoSeries([LineString([(0, 0), (30, 0)]),
                         LineString([(70, 0), (100, 0)])], crs="EPSG:4547")
    pairs, new_only, missing, modified = cmp._match_lines(two, ref)
    check("多段合计覆盖 ≥50% 判为匹配",
          len(pairs) == 1 and not missing and not new_only and not modified,
          f"pairs={pairs} modified={modified} missing={missing} new={new_only}")


def test_comparator_stale_outputs() -> None:
    """无变化时复用输出目录：旧变化 GDB/SHP 必须清除且报告为空。"""
    import geopandas as gpd
    from shapely.geometry import Point
    from pipeline.comparator import Comparator

    tmp = tempfile.mkdtemp(prefix="cmp_stale_")
    new = os.path.join(tmp, "new.gdb")
    stock = os.path.join(tmp, "stock.gdb")
    for p in (new, stock):
        gpd.GeoDataFrame(
            {"A": [1]}, geometry=[Point(500010, 3387005)],
            crs="EPSG:4547").to_file(p, layer="signs", driver="OpenFileGDB")
    out = os.path.join(tmp, "out")
    os.makedirs(out, exist_ok=True)
    stale_gdb = os.path.join(out, "变化比对.gdb")
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(0, 0)],
        crs="EPSG:4547").to_file(stale_gdb, layer="chg_new_points",
                                 driver="OpenFileGDB")
    shp_dir = os.path.join(out, "shp")
    os.makedirs(shp_dir, exist_ok=True)
    for name in ("chg_new_points.shp", "chg_new_points.dbf", "chg_new_points.qix"):
        with open(os.path.join(shp_dir, name), "wb") as f:
            f.write(b"stale")
    res = Comparator().run(new, stock, out)
    check("无变化时旧变化 GDB 已清除且报告为空",
          res["change_gdb"] == "" and not os.path.exists(stale_gdb),
          f"change_gdb={res['change_gdb']!r}")
    check("无变化时旧变化 SHP/sidecar 已清除",
          not os.listdir(shp_dir), str(os.listdir(shp_dir)))


def test_local_tm_datum_chain() -> None:
    """局部 TM 必须保留目标基准转换；ED50 等默认转换不能退化为 ballpark。"""
    import math
    from types import SimpleNamespace

    from pyproj import CRS, Transformer
    from pipeline.crs_plan import make_plan
    from pipeline.data_loader import prepare_poses

    src = CRS.from_epsg(4326)
    tgt = CRS.from_epsg(23032)   # ED50 / UTM zone 32N
    plan = make_plan(src, tgt, center_lon=9.0, center_lat=52.0,
                     force_local_tm=True)
    direct = Transformer.from_crs(src, plan.working_crs,
                                  always_xy=True).transform(9.0, 52.0)
    via = plan.to_working().transform(9.0, 52.0)
    diff = math.hypot(direct[0] - via[0], direct[1] - via[1])
    check("局部 TM 经目标基准转换（ED50 不退化为 ballpark）",
          10.0 < diff < 500.0, f"直接/中转相差 {diff:.1f}m")

    pose = SimpleNamespace(lon=9.0, lat=52.0, x=0.0, y=0.0,
                           session="s", image_filename="i")
    prepare_poses([pose], src, plan.working_crs,
                  transformer=plan.to_working())
    check("prepare_poses 使用中转转换链",
          math.hypot(pose.x - via[0], pose.y - via[1]) < 1e-6,
          f"({pose.x:.3f},{pose.y:.3f}) vs {via}")

    bound = CRS.from_proj4(
        "+proj=tmerc +lat_0=0 +lon_0=114 +k=1 +x_0=500000 +y_0=0 "
        "+ellps=IAU76 +towgs84=-24.4,112.9,-54.8,0,0,0,0 +units=m +no_defs")
    p2 = make_plan(bound, None, center_lon=114.3, center_lat=30.6,
                   force_local_tm=True)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        proj4 = p2.working_crs.to_proj4()
    check("局部 TM 保留自定义 TOWGS84",
          "towgs84" in proj4.lower(), proj4)


def test_prj_bound_crs() -> None:
    """带 TOWGS84 的自定义 CRS 写 .prj 时不能丢基准转换。"""
    from pyproj import CRS
    from pipeline import coords

    c = CRS.from_proj4(
        "+proj=tmerc +lat_0=0 +lon_0=114 +k=1 +x_0=500000 +y_0=0 "
        "+ellps=IAU76 +towgs84=-24.4,112.9,-54.8,0,0,0,0 +units=m +no_defs")
    wkt = coords.crs_to_esri_wkt(c)
    back = CRS.from_wkt(wkt)
    check("自定义 TOWGS84 的 .prj 保留基准转换",
          "TOWGS84" in wkt and back.is_bound, wkt.splitlines()[0] if wkt else "")
    plain = coords.crs_to_esri_wkt(CRS.from_epsg(4547))
    check("普通投影仍写 ESRI WKT", "PROJCS" in plain and "TOWGS84" not in plain)


def test_comparator_missing_crs() -> None:
    import geopandas as gpd
    from shapely.geometry import Point
    from pipeline import coords
    from pipeline.comparator import Comparator

    tmp = tempfile.mkdtemp(prefix="cmp_nocrs_")
    new = os.path.join(tmp, "new.gdb")
    stock = os.path.join(tmp, "stock.gdb")
    # 新成果缺 CRS（模拟 ArcGIS 导出丢失投影信息）
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(500010, 3387005)]
    ).to_file(new, layer="signs", driver="OpenFileGDB")
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(500010, 3387005)],
        crs="EPSG:4547").to_file(stock, layer="signs", driver="OpenFileGDB")

    err = None
    try:
        Comparator().run(new, stock, os.path.join(tmp, "out1"))
    except coords.CRSMissingError as e:
        err = e
    check("新成果缺 CRS → CRS_REQUIRED 且字段为 new_crs",
          err is not None and err.field == "new_crs", str(err) if err else "未抛错")

    res = Comparator().run(new, stock, os.path.join(tmp, "out2"),
                           new_crs="EPSG:4547")
    check("填写 new_crs 后重试成功并匹配",
          res["totals"]["matched"] == 1, str(res["totals"]))


def test_comparator_nonspatial_table() -> None:
    import geopandas as gpd
    import pandas as pd
    import pyogrio
    from shapely.geometry import Point
    from pipeline.comparator import Comparator

    tmp = tempfile.mkdtemp(prefix="cmp_table_")
    new = os.path.join(tmp, "new.gdb")
    stock = os.path.join(tmp, "stock.gdb")
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(500010, 3387005)],
        crs="EPSG:4547").to_file(new, layer="signs", driver="OpenFileGDB")
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(500010, 3387005)],
        crs="EPSG:4547").to_file(stock, layer="signs", driver="OpenFileGDB")
    # 非空间属性表没有 CRS，不应阻断比对
    pyogrio.write_dataframe(pd.DataFrame({"K": ["v"]}), new,
                            layer="meta", driver="OpenFileGDB")
    res = Comparator().run(new, stock, os.path.join(tmp, "out"))
    check("非空间属性表不阻断比对",
          res["totals"]["matched"] == 1, str(res["totals"]))


def main() -> int:
    _utf8_stdout()
    tests = (test_change_detector, test_read_rgb, test_dom_tiles,
             test_comparator_edge, test_comparator_cross_crs,
             test_line_coverage_length,
             test_comparator_stale_outputs, test_local_tm_datum_chain,
             test_prj_bound_crs,
             test_comparator_missing_crs, test_comparator_nonspatial_table)
    for fn in tests:
        try:
            fn()
        except Exception as e:   # noqa: BLE001 - 单个用例失败不应中断整体自检
            import traceback
            print(f"[FAIL] {fn.__name__} 异常：{type(e).__name__}: {e}")
            traceback.print_exc()
            RESULTS.append((fn.__name__, False))
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"REGRESSIONS_SELFTEST: {passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
