"""车载提取坐标系自检（合成影像/HDI，不需要模型或真实数据）。

验证 v0.6.0 起的车载坐标契约：
1. 位姿只使用 HDI col12/13 经纬度；col9/10 即使填垃圾值也不影响结果；
2. 默认输出 EPSG:4326（跟随位姿），GDB/SHP/变化图层 CRS 一致；
3. output_crs=EPSG:4547 时几何与 CRS 正确；
4. 地理坐标内部用米制局部 TM，已知像素点落点与几何计算一致；
5. 非法经纬度 fail-fast（POSE_COORDINATES_INVALID），不进入逐影像循环。

用法：python tools/selftest_vehicle_crs.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import coords  # noqa: E402
from pipeline.config import PipelineConfig  # noqa: E402
from pipeline.data_loader import PoseDataError  # noqa: E402
from pipeline.processor import Pipeline  # noqa: E402

SESSIONS = ("20260101_1-2", "20260201_1-2")
IMG_W, IMG_H = 640, 480


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}".rstrip())
    return bool(cond)


def _hdi_line(fi: int, lon: float, lat: float, alt: float = 10.0) -> str:
    """21 列 HDI 行；col9/10 故意填 999999，验证新流程不再使用。"""
    cols = [
        f"00000000-01-2026010110{fi:02d}00000", "x",
        "2026", "01", "01", "10", f"{fi:02d}", "00", "000",
        "999999.0", "999999.0", f"{alt:.3f}",
        f"{lon:.8f}", f"{lat:.8f}", "0.0", "0.0", "0.0",
        "0", "0", "0", "0",
    ]
    assert len(cols) == 21, len(cols)
    return "\t".join(cols)


def _make_project(root: str, *, bad_lon: float | None = None) -> None:
    """两期次 × 2 张影像 + 对应 HDI。"""
    from PIL import Image
    for si, session in enumerate(SESSIONS):
        cam = os.path.join(root, "照片", session, "CAM_1")
        os.makedirs(cam, exist_ok=True)
        for fi in range(2):
            Image.new("RGB", (IMG_W, IMG_H), (90, 90, 90)).save(
                os.path.join(cam, f"frame_{fi + 1:04d}.jpg"))
        hdi_dir = os.path.join(root, "轨迹", session)
        os.makedirs(hdi_dir, exist_ok=True)
        lines = ["iScan 位姿导出（表头行，列数不足 21，应被跳过）"]
        for fi in range(2):
            lon = 114.30 + fi * 0.0001
            lat = 30.60 + fi * 0.0001
            if bad_lon is not None and si == 0 and fi == 0:
                lon = bad_lon
            lines.append(_hdi_line(fi, lon, lat))
        with open(os.path.join(hdi_dir, "iScan-Image-1.hdi"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


class FakeDetector:
    """固定返回画面中下方一辆车（class_id=2，参与时序变化）。"""

    def detect(self, img):
        return [SimpleNamespace(bbox=(300.0, 250.0, 340.0, 280.0),
                                class_id=2, class_name="car", score=0.9)]


def _config() -> PipelineConfig:
    cfg = PipelineConfig()
    # 7360×4912 的真实内参不适用于 640×480 合成图；替换为同尺度针孔参数
    cfg.fisheye = dict(k1=0.0, k2=0.0, fx=500.0, fy=500.0,
                       cx=IMG_W / 2.0, cy=IMG_H / 2.0)
    cfg.use_fisheye = False
    cfg.resize_max_dim = IMG_W
    cfg.enable_change_detection = True
    return cfg


def _expected_wgs84() -> tuple[float, float]:
    """按 georeference 的像素射线计算落点。

    process_project 使用 bbox 左下角 (u=300, v=280)：cx=320、cy=240、f=500、
    alt=10m → 水平偏移 (-5m 东, +125m 北)。用大地线正算得到经纬度。
    """
    import math
    from pyproj import Geod
    east, north = -5.0, 125.0
    dist = math.hypot(east, north)
    az = math.degrees(math.atan2(east, north))
    return Geod(ellps="WGS84").fwd(114.30, 30.60, az, dist)[:2]


def _point_by_image(shp_or_gdb: str, session: str, image: str):
    import geopandas as gpd
    g = gpd.read_file(shp_or_gdb, layer="detections") if shp_or_gdb.endswith(".gdb") \
        else gpd.read_file(shp_or_gdb)
    row = g[(g["SESSION"] == session) & (g["IMAGE"] == image)]
    if row.empty:
        return None
    return row.geometry.iloc[0]


def _epsg(path: str, layer: str = "detections"):
    import pyogrio
    from pyproj import CRS
    info = (pyogrio.read_info(path, layer=layer) if str(path).lower().endswith(".gdb")
            else pyogrio.read_info(path))
    raw = info.get("crs")
    return CRS.from_user_input(raw).to_epsg() if raw else None


def test_default_4326() -> list[bool]:
    from pyproj import Geod
    tmp = tempfile.mkdtemp(prefix="veh_crs_4326_")
    data = os.path.join(tmp, "data")
    _make_project(data)
    out = os.path.join(tmp, "out")
    logs: list[str] = []
    res = Pipeline(_config(), FakeDetector()).process_project(
        data, out, export_shp=True, export_gdb=True, wlog=logs.append)
    results = [
        _check("车载提取完成（位姿仅用经纬度）", res.errors == [], str(res.errors)),
        _check("默认输出跟随位姿 = EPSG:4326",
               "EPSG:4326" in res.crs.get("target_crs", ""),
               res.crs.get("target_crs", "")),
        _check("报告记录来源 lonlat", res.crs.get("source_origin") == "lonlat"),
        _check("位姿坐标系日志输出",
               any("位姿坐标系" in line for line in logs)),
    ]
    sess = SESSIONS[0]
    gdb = os.path.join(out, "gdb", f"result_{sess}.gdb")
    shp = os.path.join(out, "shp", f"result_{sess}.shp")
    results += [
        _check("GDB 点层 CRS = EPSG:4326", _epsg(gdb) == 4326),
        _check("SHP .prj 声明地理坐标系",
               os.path.isfile(shp[:-4] + ".prj")
               and "GEOGCS" in open(shp[:-4] + ".prj", encoding="utf-8").read()),
    ]
    pt = _point_by_image(gdb, sess, "frame_0001.jpg")
    exp_lon, exp_lat = _expected_wgs84()
    dist = None
    if pt is not None:
        dist = Geod(ellps="WGS84").inv(pt.x, pt.y, exp_lon, exp_lat)[2]
    results.append(_check(
        "已知像素落点正确（误差 <0.5m）",
        pt is not None and dist is not None and dist < 0.5,
        f"pt=({pt.x:.7f},{pt.y:.7f}) 期望=({exp_lon:.7f},{exp_lat:.7f}) "
        f"距离={dist:.3f}m" if pt is not None else "无点要素"))
    results.append(_check("时序变化图层生成且 CRS = 4326",
                          bool(res.change_path)
                          and _epsg(res.change_path) == 4326,
                          res.change_path))
    results.append(_check("提取报告 JSON 含三层 CRS",
                          os.path.isfile(res.report_path)
                          and all(k in json.load(open(res.report_path, encoding="utf-8"))
                                  for k in ("source_crs", "target_crs", "working_crs"))))
    return results


def test_output_4547() -> list[bool]:
    import math
    from pyproj import Transformer
    tmp = tempfile.mkdtemp(prefix="veh_crs_4547_")
    data = os.path.join(tmp, "data")
    _make_project(data)
    out = os.path.join(tmp, "out")
    res = Pipeline(_config(), FakeDetector()).process_project(
        data, out, export_shp=False, export_gdb=True,
        output_crs="EPSG:4547", wlog=lambda _s: None)
    results = [
        _check("output_crs=4547 提取完成", res.errors == [], str(res.errors)),
        _check("输出坐标系 = EPSG:4547",
               "EPSG:4547" in res.crs.get("target_crs", ""),
               res.crs.get("target_crs", "")),
    ]
    sess = SESSIONS[0]
    gdb = os.path.join(out, "gdb", f"result_{sess}.gdb")
    results.append(_check("GDB 点层 CRS = EPSG:4547", _epsg(gdb) == 4547))
    lon, lat = _expected_wgs84()
    ex, ey = Transformer.from_crs(4326, 4547, always_xy=True).transform(lon, lat)
    pt = _point_by_image(gdb, sess, "frame_0001.jpg")
    dist = math.hypot(pt.x - ex, pt.y - ey) if pt is not None else None
    results.append(_check(
        "覆盖后几何正确（误差 <0.5m）",
        pt is not None and dist is not None and dist < 0.5,
        f"pt=({pt.x:.3f},{pt.y:.3f}) 期望=({ex:.3f},{ey:.3f})" if pt else "无点要素"))
    return results


def test_invalid_pose() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="veh_crs_bad_")
    data = os.path.join(tmp, "data")
    _make_project(data, bad_lon=200.0)
    err = None
    try:
        Pipeline(_config(), FakeDetector()).process_project(
            data, os.path.join(tmp, "out"), export_shp=False, export_gdb=False,
            wlog=lambda _s: None)
    except PoseDataError as e:
        err = e
    return [
        _check("非法经纬度 fail-fast（POSE_COORDINATES_INVALID）",
               err is not None and err.code == "POSE_COORDINATES_INVALID",
               str(err) if err else "未抛错"),
        _check("非法行号可定位", err is not None and err.line is not None,
               f"line={getattr(err, 'line', None)}"),
    ]


def test_truncated_hdi_row_rejected() -> list[bool]:
    """数据行少于 21 列必须报错，不能静默跳过造成位姿/影像错位。"""
    from pipeline.data_loader import DataProject

    tmp = tempfile.mkdtemp(prefix="veh_crs_trunc_")
    data = os.path.join(tmp, "data")
    _make_project(data)
    hdi = os.path.join(data, "轨迹", SESSIONS[0], "iScan-Image-1.hdi")
    cols = _hdi_line(2, 114.3002, 30.6002).split("\t")
    with open(hdi, "a", encoding="utf-8") as f:
        f.write("\t".join(cols[:20]) + "\n")   # 截断掉最后一列
    err = None
    try:
        DataProject.load(data)
    except PoseDataError as e:
        err = e
    return [
        _check("截断的 HDI 数据行 → POSE_COORDINATES_INVALID",
               err is not None and err.code == "POSE_COORDINATES_INVALID",
               str(err) if err else "未抛错"),
        _check("截断行号可定位", err is not None and err.line == 4,
               f"line={getattr(err, 'line', None)}"),
    ]


class EmptyDetector:
    """不返回任何目标；平灰影像也不会产生车道线。"""

    def detect(self, img):
        return []


def test_empty_outputs_removed() -> list[bool]:
    """复用输出目录且无要素时，旧 GDB 不得留存或被当作本次成果。"""
    import geopandas as gpd
    import shapefile
    from shapely.geometry import Point

    tmp = tempfile.mkdtemp(prefix="veh_crs_empty_")
    data = os.path.join(tmp, "data")
    _make_project(data)
    out = os.path.join(tmp, "out")
    sess = SESSIONS[0]
    stale_gdb = os.path.join(out, "gdb", f"result_{sess}.gdb")
    os.makedirs(os.path.dirname(stale_gdb), exist_ok=True)
    gpd.GeoDataFrame(
        {"A": [1]}, geometry=[Point(0, 0)],
        crs="EPSG:4326").to_file(stale_gdb, layer="detections",
                                 driver="OpenFileGDB")
    shp_dir = os.path.join(out, "shp")
    os.makedirs(shp_dir, exist_ok=True)
    stale_qix = os.path.join(shp_dir, f"result_lanes_{sess}.qix")
    for name in (f"result_{sess}.shp", f"result_lanes_{sess}.shp"):
        with open(os.path.join(shp_dir, name), "wb") as f:
            f.write(b"stale")
    with open(stale_qix, "wb") as f:
        f.write(b"stale")
    res = Pipeline(_config(), EmptyDetector()).process_project(
        data, out, export_shp=True, export_gdb=True, wlog=lambda _s: None)
    info = res.per_session.get(sess, {})
    shp = info.get("shp", "")
    n = len(shapefile.Reader(shp).records()) if shp and os.path.isfile(shp) else -1
    return [
        _check("无检测时旧 GDB 清除且不报告路径",
               res.errors == [] and info.get("gdb", "") == ""
               and not os.path.exists(stale_gdb),
               f"gdb={info.get('gdb')!r} errors={res.errors}"),
        _check("无检测时 SHP 为本次空图层",
               n == 0, f"records={n} shp={shp!r}"),
        _check("旧 SHP 索引文件已清除",
               not os.path.exists(stale_qix), stale_qix),
        _check("无检测时仍报告 0 个要素",
               info.get("det") == 0 and info.get("lane") == 0, str(info)),
    ]


def test_geo_distance_is_horizontal() -> list[bool]:
    """DIST 必须是地面水平距离，不能是射线参数 t。"""
    import math
    from pipeline.georeference import GeoReferencer

    geo = GeoReferencer(_config())
    pose = SimpleNamespace(x=0.0, y=0.0, alt=1.5, heading=0.0,
                           pitch=-45.0, roll=0.0)
    center = geo.pixel_to_ground(IMG_W / 2.0, IMG_H / 2.0, pose)
    off = geo.pixel_to_ground(IMG_W / 2.0 + 100.0, IMG_H / 2.0, pose)
    center_h = math.hypot(center.x - pose.x, center.y - pose.y)
    off_h = math.hypot(off.x - pose.x, off.y - pose.y)
    return [
        _check("中心像素 DIST=水平距离 1.5m",
               center.valid and abs(center.dist - 1.5) < 1e-6,
               f"dist={center.dist:.6f}"),
        _check("中心像素 DIST 与坐标水平距离一致",
               abs(center.dist - center_h) < 1e-9,
               f"dist={center.dist:.6f} hypot={center_h:.6f}"),
        _check("偏心像素 DIST 与坐标水平距离一致",
               off.valid and abs(off.dist - off_h) < 1e-9,
               f"dist={off.dist:.6f} hypot={off_h:.6f}"),
    ]


def test_projected_pose_crs_rejected() -> list[bool]:
    """HDI 的 col12/13 是经纬度，投影 CRS 不能作为位姿坐标系。"""
    tmp = tempfile.mkdtemp(prefix="veh_crs_proj_")
    data = os.path.join(tmp, "data")
    _make_project(data)
    err = None
    try:
        Pipeline(_config(), FakeDetector()).process_project(
            data, os.path.join(tmp, "out"), export_shp=False, export_gdb=False,
            pose_crs="EPSG:4547", wlog=lambda _s: None)
    except coords.CRSInvalidError as e:
        err = e
    return [
        _check("投影坐标系作位姿坐标系 → CRS_INVALID",
               err is not None and err.code == "CRS_INVALID",
               str(err) if err else "未抛错"),
        _check("错误字段定位到 pose_crs",
               err is not None and err.field == "pose_crs",
               getattr(err, "field", "")),
        _check("提示明确要求地理坐标系",
               err is not None and "地理坐标系" in (err.hint + str(err)),
               getattr(err, "hint", "")),
    ]


def test_parse_crs_inputs() -> list[bool]:
    from pyproj import CRS
    from pipeline import crs_plan
    results = [
        _check("空字符串按未指定处理", coords.parse_crs("") is None),
        _check("EPSG 简写可解析", coords.parse_crs("4547").to_epsg() == 4547),
        _check("WKT 可解析", coords.parse_crs(CRS.from_epsg(3857).to_wkt())
               .to_epsg() == 3857),
    ]
    label = coords.crs_label(crs_plan.local_tm_crs(CRS.from_epsg(4326), 114.3))
    results.append(_check("局部 TM 标签显示中央经线", "中央经线" in label, label))
    try:
        coords.parse_crs("not-a-crs", field="output_crs")
        results.append(_check("非法 CRS 抛 CRS_INVALID", False))
    except coords.CRSInvalidError as e:
        results.append(_check("非法 CRS 抛 CRS_INVALID 且带字段",
                              e.code == "CRS_INVALID" and e.field == "output_crs"))
    return results


def main() -> int:
    _utf8_stdout()
    results: list[bool] = []
    for fn in (test_default_4326, test_output_4547, test_invalid_pose,
               test_truncated_hdi_row_rejected, test_empty_outputs_removed,
               test_geo_distance_is_horizontal,
               test_projected_pose_crs_rejected, test_parse_crs_inputs):
        try:
            results += fn()
        except Exception as e:   # noqa: BLE001 - 单个用例失败不应中断整体自检
            import traceback
            print(f"[FAIL] {fn.__name__} 异常：{type(e).__name__}: {e}")
            traceback.print_exc()
            results.append(False)
    print(f"VEHICLE_CRS_SELFTEST: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
