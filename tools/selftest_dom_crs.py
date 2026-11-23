"""正射提取坐标系自检（合成 DOM，不依赖真实数据）。

验证 v0.6.0 起的坐标系契约：
1. 影像缺 CRS（含只剩名称的 Engineering CRS）→ 抛 CRS_REQUIRED，不自动猜测；
2. 用户填 input_crs=EPSG:3857 → 修复并正常提取（现场 ly-dom.tif 场景）；
3. 文件元数据 ESRI PE String 可恢复完整投影；
4. 输出默认跟随影像 CRS（3857/4547/4326），GDB/SHP/.prj 一致；
5. output_crs 可覆盖输出，几何从工作系转换到目标 CRS；
6. 地理坐标系（4326）输入时内部用米制局部 TM，输出仍为 4326；
7. 存量 GDB 与影像 CRS 不同（4547 vs 3857）时逐层转换后裁剪/走廊仍正确；
8. LEN_M 为真实米（与输出 CRS 无关）。

用法：python tools/selftest_dom_crs.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.transform import from_origin

from pipeline import coords
from pipeline.dom_extractor import DomExtractor

SIZE = 600

# ArcGIS 常见的 ESRI Web Mercator WKT（与 EPSG:3857 等价）
ESRI_WEB_MERCATOR_WKT = (
    'PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",'
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Mercator_Auxiliary_Sphere"],'
    'PARAMETER["False_Easting",0.0],PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",0.0],PARAMETER["Standard_Parallel_1",0.0],'
    'PARAMETER["Auxiliary_Sphere_Type",0.0],UNIT["Meter",1.0]]'
)
# 投影参数丢失时 GDAL 退化出的 Engineering CRS（只剩名称）
LOST_WEB_MERCATOR_WKT = (
    'LOCAL_CS["WGS_1984_Web_Mercator_Auxiliary_Sphere",UNIT["Meter",1.0]]'
)
LOCAL_CS_WKT = 'LOCAL_CS["Nonearth",UNIT["Meter",1.0]]'


def _utf8_stdout() -> None:
    """Windows 控制台默认可能是 cp1252/cp936，打印中文会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _make_dom(path: str, crs, origin: tuple[float, float], pixel: float = 0.1,
              esri_wkt: str | None = None) -> None:
    """合成一张带三条白色标线的简易 DOM。"""
    img = np.full((SIZE, SIZE, 3), 90, np.uint8)
    img[100:104, 40:SIZE - 40] = 235     # 水平线：中点约为像素 (300, 101.5)
    img[300:SIZE - 40, 200:205] = 235
    img[300:SIZE - 40, 400:405] = 235
    with rasterio.open(path, "w", driver="GTiff", height=SIZE, width=SIZE, count=3,
                       dtype="uint8", crs=crs,
                       transform=from_origin(origin[0], origin[1], pixel, pixel)) as ds:
        ds.write(img.transpose(2, 0, 1))
        if esri_wkt:
            ds.update_tags(ESRI_PE_STRING=esri_wkt)


def _check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}".rstrip())
    return bool(cond)


def _lane_midpoint(gdb: str):
    """读取 lanes 图层中的水平线，返回中点（输出 CRS，可能是米或度）。

    不能按绝对长度筛选：输出为经纬度时线长是小数。这里按长宽比识别
    水平线（垂直线 dx≈0，转换后的水平线可能有轻微旋转）。
    """
    import geopandas as gpd
    g = gpd.read_file(gdb, layer="lanes")
    best = None
    for ln in g.geometry:
        if ln is None:
            continue
        minx, miny, maxx, maxy = ln.bounds
        dx, dy = maxx - minx, maxy - miny
        if dx > 0 and dx > 10.0 * dy:
            if best is None or dx > (best.bounds[2] - best.bounds[0]):
                best = ln
    if best is None:
        return None
    return best.interpolate(0.5, normalized=True)


def _gdb_epsg(path: str, layer: str = "lanes"):
    import pyogrio
    info = pyogrio.read_info(path, layer=layer)
    raw = info.get("crs")
    return CRS.from_user_input(raw).to_epsg() if raw else None


def _run(tif: str, out: str, **kwargs):
    logs: list[str] = []
    err = None
    report = None
    try:
        report = DomExtractor(min_len=2.0).run(
            tif, out, wlog=logs.append, export_shp=True, **kwargs)
    except Exception as e:      # noqa: BLE001 - 用例需要捕获任意失败
        err = e
    return report, err, logs


def test_crs_resolution() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="dom_crs_resolve_")
    results = []

    # 1) 无 CRS、范围像 Web Mercator → 只报错并给出候选，不自动采用
    no_crs = os.path.join(tmp, "no_crs.tif")
    _make_dom(no_crs, None, (12730000, 3577000))
    with rasterio.open(no_crs) as ds:
        err = None
        try:
            coords.resolve_raster_crs(ds, None)
        except coords.CRSMissingError as e:
            err = e
        results.append(_check(
            "无 CRS → CRS_REQUIRED 且带字段名",
            err is not None and err.code == "CRS_REQUIRED" and err.field == "input_crs",
            str(err) if err else "未抛错"))
        cands = coords.suggest_crs_candidates(ds.bounds)
        results.append(_check(
            "错误提示给出 EPSG:3857 候选（仅提示，不自动应用）",
            any(c["epsg"] == 3857 for c in cands), str(cands)))
        got, origin, _ = coords.resolve_raster_crs(ds, "EPSG:3857")
        results.append(_check("用户填 EPSG:3857 → 解析成功且来源为 user",
                              got.to_epsg() == 3857 and origin == "user"))

    # 2) Engineering CRS + ESRI PE String → 按元数据恢复
    lost = os.path.join(tmp, "lost.tif")
    _make_dom(lost, LOST_WEB_MERCATOR_WKT, (12730000, 3577000),
              esri_wkt=ESRI_WEB_MERCATOR_WKT)
    with rasterio.open(lost) as ds:
        got, origin, note = coords.resolve_raster_crs(ds, None)
        results.append(_check(
            "Engineering CRS 由 ESRI PE String 恢复为 EPSG:3857",
            got.to_epsg() == 3857 and origin == "esri_pe_string" and bool(note), note))

    # 3) Engineering CRS 且范围像武汉本地 TM → 不再自动降级
    local = os.path.join(tmp, "local.tif")
    _make_dom(local, LOCAL_CS_WKT, (780000, 400000))
    with rasterio.open(local) as ds:
        err = None
        try:
            coords.resolve_raster_crs(ds, None)
        except coords.CRSMissingError as e:
            err = e
        results.append(_check(
            "Engineering CRS（本地范围）→ 仍要求用户指定，不猜测",
            err is not None and err.code == "CRS_REQUIRED",
            str(err) if err else "未抛错"))
    return results


def test_output_follows_3857() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="dom_crs_3857_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:3857", (12730000, 3577000))
    out = os.path.join(tmp, "out")
    report, err, _ = _run(tif, out)
    results = [_check("3857 DOM 提取成功", err is None, str(err) if err else "")]
    if err is not None:
        return results
    results += [
        _check("输出跟随影像 = EPSG:3857",
               "EPSG:3857" in report.get("target_crs", ""),
               report.get("target_crs", "")),
        _check("输出 GDB CRS = EPSG:3857",
               _gdb_epsg(os.path.join(out, "正射提取.gdb")) == 3857),
        _check("工作坐标系为局部 TM",
               report.get("working_reason") == "local_tm"),
    ]
    mid = _lane_midpoint(os.path.join(out, "正射提取.gdb"))
    results.append(_check(
        "已知像素点坐标正确（3857，误差 <0.5m）",
        mid is not None and abs(mid.x - 12730030.0) < 0.5
        and abs(mid.y - 3576989.85) < 0.5,
        f"mid=({mid.x:.2f},{mid.y:.2f})" if mid else "无水平线"))
    prj = os.path.join(out, "shp", "lanes.prj")
    cpg = os.path.join(out, "shp", "lanes.cpg")
    results += [
        _check("SHP .prj 写出 ESRI WKT",
               os.path.isfile(prj)
               and "PROJCS" in open(prj, encoding="utf-8").read()),
        _check("SHP .cpg 声明 UTF-8",
               os.path.isfile(cpg)
               and open(cpg, encoding="ascii").read().strip() == "UTF-8"),
    ]
    return results


def test_output_follows_4547() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="dom_crs_4547_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:4547", (500000, 3387000))
    out = os.path.join(tmp, "out")
    report, err, _ = _run(tif, out)
    results = [_check("4547 DOM 提取成功", err is None, str(err) if err else "")]
    if err is not None:
        return results
    results += [
        _check("输出跟随影像 = EPSG:4547",
               "EPSG:4547" in report.get("target_crs", ""),
               report.get("target_crs", "")),
        _check("输出 GDB CRS = EPSG:4547",
               _gdb_epsg(os.path.join(out, "正射提取.gdb")) == 4547),
        _check("4547 尺度/收敛角合格，可直接作为工作系",
               report.get("working_reason") == "target"),
    ]
    mid = _lane_midpoint(os.path.join(out, "正射提取.gdb"))
    results.append(_check(
        "已知像素点坐标正确（4547，误差 <0.5m）",
        mid is not None and abs(mid.x - 500030.0) < 0.5
        and abs(mid.y - 3386989.85) < 0.5,
        f"mid=({mid.x:.2f},{mid.y:.2f})" if mid else "无水平线"))
    return results


def test_output_follows_4326() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="dom_crs_4326_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:4326", (114.30, 30.60), pixel=1e-6)
    out = os.path.join(tmp, "out")
    report, err, _ = _run(tif, out)
    results = [_check("4326 DOM（度单位）提取成功", err is None, str(err) if err else "")]
    if err is not None:
        return results
    results += [
        _check("输出跟随影像 = EPSG:4326",
               "EPSG:4326" in report.get("target_crs", ""),
               report.get("target_crs", "")),
        _check("输出 GDB CRS = EPSG:4326",
               _gdb_epsg(os.path.join(out, "正射提取.gdb")) == 4326),
        _check("度单位输入内部改用米制局部 TM",
               report.get("working_reason") == "local_tm"),
    ]
    gdb = os.path.join(out, "正射提取.gdb")
    mid = _lane_midpoint(gdb)
    results.append(_check(
        "已知像素点坐标正确（4326，误差 <1e-5°）",
        mid is not None and abs(mid.x - 114.300300) < 1e-5
        and abs(mid.y - 30.5998985) < 1e-5,
        f"mid=({mid.x:.8f},{mid.y:.8f})" if mid else "无水平线"))
    import geopandas as gpd
    lens = gpd.read_file(gdb, layer="lanes")["LEN_M"].tolist()
    results.append(_check(
        "LEN_M 为真实米（输出为经纬度时仍 >40m）",
        bool(lens) and max(lens) > 40, str(sorted(lens)[-3:])))
    return results


def test_output_override() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="dom_crs_override_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:3857", (12730000, 3577000))
    out = os.path.join(tmp, "out")
    report, err, _ = _run(tif, out, output_crs="EPSG:4547")
    results = [_check("output_crs=4547 覆盖成功", err is None, str(err) if err else "")]
    if err is not None:
        return results
    results += [
        _check("报告记录用户覆盖",
               "EPSG:4547" in report.get("target_crs", "")
               and report.get("target_origin") == "user",
               f"{report.get('target_crs')} / {report.get('target_origin')}"),
        _check("覆盖后 GDB CRS = EPSG:4547",
               _gdb_epsg(os.path.join(out, "正射提取.gdb")) == 4547),
    ]
    tf = Transformer.from_crs(3857, 4547, always_xy=True)
    ex, ey = tf.transform(12730030.0, 3576989.85)
    mid = _lane_midpoint(os.path.join(out, "正射提取.gdb"))
    results.append(_check(
        "覆盖后几何正确（误差 <0.5m）",
        mid is not None and abs(mid.x - ex) < 0.5 and abs(mid.y - ey) < 0.5,
        f"mid=({mid.x:.2f},{mid.y:.2f}) 期望=({ex:.2f},{ey:.2f})" if mid else "无水平线"))
    return results


def test_missing_crs_override() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="dom_crs_fix_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, None, (12730000, 3577000))
    out = os.path.join(tmp, "out")
    report, err, _ = _run(tif, out, input_crs="EPSG:3857")
    results = [_check("无 CRS + 用户指定 EPSG:3857 → 提取成功",
                      err is None, str(err) if err else "")]
    if err is not None:
        return results
    results += [
        _check("报告来源为 user",
               report.get("source_origin") == "user",
               report.get("source_origin", "")),
        _check("无 CRS 修复后输出仍跟随输入 = EPSG:3857",
               _gdb_epsg(os.path.join(out, "正射提取.gdb")) == 3857),
    ]
    return results


def test_stock_different_crs() -> list[bool]:
    import geopandas as gpd
    from shapely.geometry import LineString

    tmp = tempfile.mkdtemp(prefix="dom_crs_stock_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:3857", (12730000, 3577000))

    tf = Transformer.from_crs(3857, 4547, always_xy=True)
    x0, y0 = tf.transform(12730000.0, 3576989.0)
    x1, y1 = tf.transform(12730060.0, 3576989.0)
    stock = os.path.join(tmp, "stock.gdb")
    gpd.GeoDataFrame(
        {"ID": [1]}, geometry=[LineString([(x0, y0), (x1, y1)])],
        crs="EPSG:4547").to_file(stock, layer="LTSL", driver="OpenFileGDB")

    out = os.path.join(tmp, "out")
    report, err, logs = _run(tif, out, stock_gdb=stock)
    results = [_check("存量 4547 + DOM 3857 提取成功",
                      err is None, str(err) if err else "")]
    if err is not None:
        return results
    results += [
        _check("报告记录存量 CRS 为 EPSG:4547",
               "EPSG:4547" in report.get("stock_crs", ""),
               report.get("stock_crs", "")),
        _check("输出仍跟随 DOM = EPSG:3857",
               _gdb_epsg(os.path.join(out, "正射提取.gdb")) == 3857),
        _check("存量裁剪/走廊日志出现",
               any("按存量图幅裁剪" in line for line in logs)),
        _check("走廊约束下仍提取到水平标线",
               _lane_midpoint(os.path.join(out, "正射提取.gdb")) is not None),
    ]
    return results


def test_projected_to_geographic_output() -> list[bool]:
    """投影 DOM 输出经纬度：成果 CRS 与已知点偏差都要正确。"""
    import math

    tmp = tempfile.mkdtemp(prefix="dom_crs_geo_out_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:3857", (12730000, 3577000))
    out = os.path.join(tmp, "out")
    report, err, _ = _run(tif, out, output_crs="EPSG:4326")
    results = [_check("3857 DOM → output_crs=4326 提取成功",
                      err is None, str(err) if err else "")]
    if err is not None:
        return results
    mid = _lane_midpoint(os.path.join(out, "正射提取.gdb"))
    exp = Transformer.from_crs(3857, 4326, always_xy=True).transform(
        12730030.0, 3576989.85)
    dist = None
    if mid is not None:
        dist = math.hypot((mid.x - exp[0]) * 111320
                          * math.cos(math.radians(exp[1])),
                          (mid.y - exp[1]) * 110540)
    results += [
        _check("输出跟随覆盖值 = EPSG:4326",
               "EPSG:4326" in report.get("target_crs", ""),
               report.get("target_crs", "")),
        _check("地理输出已知点误差 <0.5m",
               dist is not None and dist < 0.5,
               f"mid=({mid.x:.8f},{mid.y:.8f}) 期望=({exp[0]:.8f},{exp[1]:.8f}) "
               f"距离={dist:.3f}m" if mid is not None else "无标线"),
    ]
    return results


def test_stock_corridor_without_clip() -> list[bool]:
    """关闭“按存量范围裁剪”时，走廊约束仍应使用存量 GDB。"""
    import geopandas as gpd
    from shapely.geometry import LineString

    tmp = tempfile.mkdtemp(prefix="dom_crs_corridor_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:3857", (12730000, 3577000))
    tf = Transformer.from_crs(3857, 4547, always_xy=True)
    x0, y0 = tf.transform(12730000.0, 3576989.0)
    x1, y1 = tf.transform(12730060.0, 3576989.0)
    stock = os.path.join(tmp, "stock.gdb")
    gpd.GeoDataFrame(
        {"ID": [1]}, geometry=[LineString([(x0, y0), (x1, y1)])],
        crs="EPSG:4547").to_file(stock, layer="LTSL", driver="OpenFileGDB")

    out = os.path.join(tmp, "out")
    report, err, logs = _run(tif, out, stock_gdb=stock,
                             use_stock_clip=False, use_corridor=True)
    results = [_check("关闭存量裁剪 + 开启走廊 → 提取成功",
                      err is None, str(err) if err else "")]
    if err is not None:
        return results
    results += [
        _check("处理范围改为全图而非静默裁剪",
               any("未启用存量裁剪" in line for line in logs),
               " / ".join(line for line in logs if "处理范围" in line)),
        _check("走廊约束仍然生效",
               any("存量走廊约束：开启" in line for line in logs),
               " / ".join(line for line in logs if "走廊" in line)),
        _check("走廊内水平标线仍被提取",
               _lane_midpoint(os.path.join(out, "正射提取.gdb")) is not None),
    ]
    return results


def main() -> int:
    _utf8_stdout()
    results = []
    for fn in (test_crs_resolution, test_output_follows_3857,
               test_output_follows_4547, test_output_follows_4326,
               test_output_override, test_missing_crs_override,
               test_stock_different_crs, test_stock_corridor_without_clip,
               test_projected_to_geographic_output):
        try:
            results += fn()
        except Exception as e:   # noqa: BLE001 - 单个用例失败不应中断整体自检
            import traceback
            print(f"[FAIL] {fn.__name__} 异常：{type(e).__name__}: {e}")
            traceback.print_exc()
            results.append(False)
    print(f"DOM_CRS_SELFTEST: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
