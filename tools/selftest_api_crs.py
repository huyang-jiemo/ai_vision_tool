"""API 层坐标系契约自检：同步预检错误码 / 字段定位 / 后台状态回传。

覆盖：
1. 正射影像缺 CRS → start_dom 同步返回 CRS_REQUIRED + 候选，不启动线程；
2. 填写 input_crs 后重试成功，后台完成后 status 回传 crs_info；
3. 非法 output_crs → CRS_INVALID 且字段为 output_crs；
4. 存量 GDB 路径不存在 → INPUT_NOT_FOUND + 字段 stock_gdb；
5. 比对新成果缺 CRS → CRS_REQUIRED + 字段 new_crs；
6. 比对底图缺 CRS → 不阻断，返回 note，后台继续并跳过预览；
7. 车载非法 pose_crs → CRS_INVALID + 字段 pose_crs；
8. 矢量缺 CRS → 按邻近矢量 + 底图范围自动推断，错误候选被排除；
9. EPSG 文本带 BOM/零宽字符/全角冒号时仍能解析。

用法：python tools/selftest_api_crs.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from api import Api  # noqa: E402


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}".rstrip())
    return bool(cond)


def _make_dom(path: str, crs, origin=(12730000.0, 3577000.0)) -> None:
    img = np.full((3, 300, 300), 90, np.uint8)
    img[:, 100:104, 20:280] = 235
    with rasterio.open(path, "w", driver="GTiff", height=300, width=300, count=3,
                       dtype="uint8", crs=crs,
                       transform=from_origin(origin[0], origin[1], 0.1, 0.1)) as ds:
        ds.write(img)


def _wait(get_status, timeout: float = 90.0) -> dict:
    end = time.time() + timeout
    s = get_status()
    while time.time() < end:
        s = get_status()
        if s.get("state") in ("done", "error"):
            return s
        time.sleep(0.1)
    return s


def test_dom_preflight_and_retry() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="api_crs_dom_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, None)
    out = os.path.join(tmp, "out")

    api = Api()
    r = api.start_dom(tif, out)
    results = [
        _check("缺 CRS → started=false + CRS_REQUIRED",
               r.get("started") is False and r.get("error_code") == "CRS_REQUIRED",
               str(r.get("error_code"))),
        _check("错误字段定位到 input_crs", r.get("crs_field") == "input_crs"),
        _check("返回 EPSG:3857 候选",
               any(c.get("epsg") == 3857 for c in r.get("crs_candidates", []))),
        _check("预检失败不启动线程", api.dom_state.state == "idle",
               api.dom_state.state),
    ]

    r2 = api.start_dom(tif, out, input_crs="EPSG:3857")
    results.append(_check("填写 input_crs 后启动成功", r2.get("started") is True,
                          str(r2)))
    if r2.get("started"):
        s = _wait(api.get_dom_status)
        results += [
            _check("后台提取完成", s.get("state") == "done",
                   s.get("error") or s.get("state")),
            _check("status 回传 crs_info",
                   "EPSG:3857" in (s.get("crs_info") or {}).get("target_crs", ""),
                   str(s.get("crs_info"))),
            _check("status 始终包含结构化错误字段",
                   all(k in s for k in ("error_code", "crs_field", "hint",
                                        "crs_candidates", "crs_info"))),
        ]
    return results


def test_dom_invalid_and_missing_stock() -> list[bool]:
    tmp = tempfile.mkdtemp(prefix="api_crs_dom2_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, "EPSG:3857")
    out = os.path.join(tmp, "out")
    api = Api()
    r = api.start_dom(tif, out, output_crs="not-a-crs")
    results = [
        _check("非法 output_crs → CRS_INVALID",
               r.get("started") is False and r.get("error_code") == "CRS_INVALID",
               str(r.get("error_code"))),
        _check("字段定位到 output_crs", r.get("crs_field") == "output_crs"),
    ]
    missing = os.path.join(tmp, "nope.gdb")
    r2 = api.start_dom(tif, out, stock_gdb=missing, use_stock_clip=True)
    results += [
        _check("存量 GDB 不存在 → INPUT_NOT_FOUND",
               r2.get("started") is False
               and r2.get("error_code") == "INPUT_NOT_FOUND",
               str(r2.get("error_code"))),
        _check("字段定位到 stock_gdb", r2.get("crs_field") == "stock_gdb"),
    ]
    return results


def _write_gdb(path: str, geom, crs, layer: str = "signs") -> None:
    import geopandas as gpd
    gpd.GeoDataFrame({"A": [1]}, geometry=[geom], crs=crs).to_file(
        path, layer=layer, driver="OpenFileGDB")


def test_compare_preflight() -> list[bool]:
    import geopandas as gpd
    from shapely.geometry import LineString

    tmp = tempfile.mkdtemp(prefix="api_crs_cmp_")
    new_nocrs = os.path.join(tmp, "new_nocrs.gdb")
    gpd.GeoDataFrame({"A": [1]}, geometry=[LineString([(0, 0), (10, 0)])]).to_file(
        new_nocrs, layer="lanes", driver="OpenFileGDB")
    stock = os.path.join(tmp, "stock.gdb")
    _write_gdb(stock, LineString([(500000, 3387000), (500050, 3387000)]),
               "EPSG:4547", layer="LTSL")
    dom = os.path.join(tmp, "dom.tif")
    _make_dom(dom, None)

    api = Api()
    r = api.start_compare(new_nocrs, stock, dom)
    results = [
        _check("新成果缺 CRS → CRS_REQUIRED",
               r.get("started") is False and r.get("error_code") == "CRS_REQUIRED",
               str(r.get("error_code"))),
        _check("字段定位到 new_crs", r.get("crs_field") == "new_crs"),
    ]

    # 底图缺 CRS：不阻断比对，仅提示预览跳过
    new_ok = os.path.join(tmp, "new_ok.gdb")
    _write_gdb(new_ok, LineString([(500000, 3387000), (500050, 3387000)]),
               "EPSG:4547", layer="lanes")
    r2 = api.start_compare(new_ok, stock, dom)
    results.append(_check("底图缺 CRS → 仍启动比对并返回提示",
                          r2.get("started") is True
                          and "预览将跳过" in (r2.get("note") or ""),
                          str(r2)))
    if r2.get("started"):
        s = _wait(api.get_cmp_status)
        results += [
            _check("后台比对完成", s.get("state") == "done",
                   s.get("error") or s.get("state")),
            _check("底图缺 CRS 时预览为空但结果正常",
                   (s.get("result") or {}).get("preview") == ""
                   and (s.get("result") or {}).get("totals", {}).get("matched") == 1,
                   str((s.get("result") or {}).get("totals"))),
        ]
    return results


def test_pipeline_invalid_pose_crs() -> list[bool]:
    api = Api()
    r = api.start_pipeline("/nonexistent", "/tmp/out", pose_crs="bad-crs")
    return [
        _check("车载非法 pose_crs → CRS_INVALID",
               r.get("started") is False and r.get("error_code") == "CRS_INVALID",
               str(r.get("error_code"))),
        _check("字段定位到 pose_crs", r.get("crs_field") == "pose_crs"),
        _check("非法参数不启动线程", api.state.state == "idle", api.state.state),
    ]


def test_pipeline_invalid_params() -> list[bool]:
    """投影位姿坐标系与非法置信度必须在同步预检阶段拒绝。"""
    tmp = tempfile.mkdtemp(prefix="api_crs_params_")
    out = os.path.join(tmp, "out")
    api = Api()
    r = api.start_pipeline("/nonexistent", out, pose_crs="EPSG:4547")
    results = [
        _check("投影 pose_crs → CRS_INVALID",
               r.get("started") is False and r.get("error_code") == "CRS_INVALID",
               str(r)),
        _check("投影 pose_crs 字段定位", r.get("crs_field") == "pose_crs"),
    ]
    r2 = api.start_pipeline("/nonexistent", out, conf=float("nan"))
    results += [
        _check("NaN 置信度 → PARAM_INVALID",
               r2.get("started") is False
               and r2.get("error_code") == "PARAM_INVALID",
               str(r2)),
        _check("置信度错误字段定位到 conf", r2.get("crs_field") == "conf"),
        _check("参数错误不启动线程", api.state.state == "idle", api.state.state),
    ]
    # 清空输入框 → pywebview 传 null：按默认阈值启动，后台必须以终态结束
    r3 = api.start_pipeline("/nonexistent", out, conf=None)
    results.append(_check("置信度 null 按默认值启动", r3.get("started") is True,
                          str(r3)))
    if r3.get("started"):
        s = _wait(api.get_status, timeout=30)
        results.append(_check("null 置信度不会卡在 running",
                              s.get("state") in ("done", "error"),
                              f"{s.get('state')} / {s.get('error')}"))
    return results


def test_state_guard() -> list[bool]:
    """后台线程兜底：异常/意外返回都必须落地为 error，不能停在 running。"""
    from api import RunState, _begin_run

    st = RunState()
    ok_first = _begin_run(st)
    ok_second = _begin_run(st)
    api = Api()
    st2 = RunState()
    _begin_run(st2)
    api._guarded(st2, lambda: 1 / 0)
    st3 = RunState()
    _begin_run(st3)
    api._guarded(st3, lambda: None)
    return [
        _check("_begin_run 首次成功、重复调用拒绝",
               ok_first is True and ok_second is False,
               f"{ok_first}/{ok_second}"),
        _check("后台异常落地为 error",
               st2.state == "error" and st2.error_code == "INTERNAL",
               f"{st2.state}/{st2.error_code}"),
        _check("线程意外返回也落地为 error",
               st3.state == "error" and "意外结束" in (st3.error or ""),
               f"{st3.state}/{st3.error}"),
    ]


def test_pipeline_background_pose_error() -> list[bool]:
    """后台 HDI 坐标非法时，get_status 要回传 POSE_COORDINATES_INVALID。"""
    from PIL import Image
    tmp = tempfile.mkdtemp(prefix="api_crs_pose_")
    session = "20260101_1-2"
    cam = os.path.join(tmp, "照片", session, "CAM_1")
    os.makedirs(cam, exist_ok=True)
    Image.new("RGB", (64, 48), (90, 90, 90)).save(
        os.path.join(cam, "frame_0001.jpg"))
    hdi_dir = os.path.join(tmp, "轨迹", session)
    os.makedirs(hdi_dir, exist_ok=True)
    cols = ["ts", "x", "2026", "01", "01", "10", "00", "00", "000",
            "0", "0", "10.0", "200.0", "30.6", "0", "0", "0",
            "0", "0", "0", "0"]
    with open(os.path.join(hdi_dir, "iScan-Image-1.hdi"), "w",
              encoding="utf-8") as f:
        f.write("\t".join(cols) + "\n")

    api = Api()
    r = api.start_pipeline(tmp, os.path.join(tmp, "out"))
    results = [_check("车载任务启动成功", r.get("started") is True, str(r))]
    if r.get("started"):
        s = _wait(api.get_status, timeout=60)
        results += [
            _check("后台位姿错误状态回传",
                   s.get("state") == "error"
                   and s.get("error_code") == "POSE_COORDINATES_INVALID",
                   f"{s.get('state')} / {s.get('error_code')}"),
            _check("错误信息包含越界坐标",
                   "经纬度越界" in (s.get("error") or ""),
                   s.get("error") or ""),
        ]
    return results


def test_crs_auto_inference() -> list[bool]:
    """矢量缺 CRS 时按邻近数据自动判断；错误候选应被底图锚点排除。

    模拟现场：存量 GDB 无 CRS、同项目轨迹 SHP 带武汉 CGCS2000 3° 带 CRS，
    另有一个原始坐标完全相同的错误候选（相邻 3° 带）。只有能与正射影像
    范围重合的候选才应被采用；没有锚点且证据冲突时必须拒绝猜测。
    """
    import geopandas as gpd
    from pyproj import CRS, Transformer
    from rasterio.transform import from_bounds as rbounds
    from shapely.geometry import LineString

    from pipeline import coords

    results = []
    for text in ("\ufeffEPSG:4547", "EPSG:\u200b4547", "EPSG\uff1a4547"):
        try:
            epsg = coords.parse_crs(text).to_epsg()
        except Exception:
            epsg = None
        results.append(_check(f"CRS 文本容错：{text!r}", epsg == 4547, str(epsg)))

    tmp = tempfile.mkdtemp(prefix="api_crs_infer_")
    stock = os.path.join(tmp, "工作", "stock.gdb")
    os.makedirs(os.path.dirname(stock), exist_ok=True)
    tr = Transformer.from_crs(4326, 4547, always_xy=True)
    x, y = tr.transform(114.36, 30.55)
    geom = LineString([(x, y), (x + 80.0, y + 40.0)])

    gpd.GeoDataFrame({"A": [1]}, geometry=[geom]).to_file(
        stock, layer="LTSL", driver="OpenFileGDB")
    track_dir = os.path.join(tmp, "轨迹")
    decoy_dir = os.path.join(tmp, "旧成果")
    os.makedirs(track_dir, exist_ok=True)
    os.makedirs(decoy_dir, exist_ok=True)
    track = os.path.join(track_dir, "track.shp")
    decoy = os.path.join(decoy_dir, "track.shp")
    gpd.GeoDataFrame({"A": [1]}, geometry=[geom], crs=CRS.from_epsg(4547)).to_file(
        track, driver="ESRI Shapefile")
    # 原始数值完全相同、但标成相邻 3° 带（CM 117E）的错误候选
    gpd.GeoDataFrame({"A": [1]}, geometry=[geom], crs=CRS.from_epsg(4548)).to_file(
        decoy, driver="ESRI Shapefile")

    dom = os.path.join(tmp, "影像", "dom.tif")
    os.makedirs(os.path.dirname(dom), exist_ok=True)
    b4547 = (x - 60.0, y - 60.0, x + 160.0, y + 160.0)
    b3857 = coords.transform_bounds(b4547, CRS.from_epsg(4547), CRS.from_epsg(3857))
    with rasterio.open(dom, "w", driver="GTiff", height=120, width=120, count=1,
                       dtype="uint8", crs=CRS.from_epsg(3857),
                       transform=rbounds(*b3857, 120, 120)) as ds:
        ds.write(np.full((1, 120, 120), 90, np.uint8))
    dom_crs = CRS.from_epsg(3857)
    dom_bounds = b3857

    results.append(_check(
        "GDB 路径的搜索根是其父目录（不是 .gdb 内部）",
        coords.inference_roots(stock)[:1] == [os.path.dirname(stock)],
        str(coords.inference_roots(stock))))
    roots = coords.inference_roots(dom, stock)
    inf = coords.infer_vector_crs(
        stock, roots=roots, anchor_crs=dom_crs, anchor_bounds=dom_bounds)
    results += [
        _check("有底图锚点 → 自动推断成功", inf is not None,
               "None" if inf is None else coords.describe(inf.crs)),
        _check("推断为正确 CRS（EPSG:4547）",
               inf is not None and inf.crs.to_epsg() == 4547,
               str(inf.crs.to_epsg()) if inf else ""),
        _check("推断说明包含依据", inf is not None and "邻近矢量" in inf.reason,
               inf.reason if inf else ""),
        _check("错误候选被锚点排除",
               coords.infer_vector_crs(
                   stock, roots=[decoy_dir], anchor_crs=dom_crs,
                   anchor_bounds=dom_bounds) is None),
        _check("无锚点且候选冲突 → 不猜测",
               coords.infer_vector_crs(stock, roots=roots) is None),
    ]

    info = coords.inspect_gdb_crs(
        stock, infer_roots=roots, anchor_crs=dom_crs, anchor_bounds=dom_bounds)
    results += [
        _check("inspect_gdb_crs 使用推断结果",
               info.origin == "inferred" and info.primary.to_epsg() == 4547,
               f"{info.origin}/{info.primary.to_epsg()}"),
        _check("推断结果写入报告 note", bool(info.report().get("note")),
               info.report().get("note", "")),
    ]
    user = coords.inspect_gdb_crs(
        stock, "EPSG:4549", infer_roots=roots, anchor_crs=dom_crs,
        anchor_bounds=dom_bounds)
    results.append(_check(
        "用户填写优先于自动推断",
        user.origin == "user" and user.primary.to_epsg() == 4549,
        f"{user.origin}/{user.primary.to_epsg()}"))
    return results


def main() -> int:
    _utf8_stdout()
    results: list[bool] = []
    for fn in (test_dom_preflight_and_retry, test_dom_invalid_and_missing_stock,
               test_compare_preflight, test_pipeline_invalid_pose_crs,
               test_pipeline_invalid_params, test_state_guard,
               test_pipeline_background_pose_error, test_crs_auto_inference):
        try:
            results += fn()
        except Exception as e:   # noqa: BLE001
            import traceback
            print(f"[FAIL] {fn.__name__} 异常：{type(e).__name__}: {e}")
            traceback.print_exc()
            results.append(False)
    print(f"API_CRS_SELFTEST: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
