"""正射提取坐标系分支自检（合成 DOM，不依赖真实数据）。

回归背景：v0.5.6 在 Windows 真机上报
`Error creating Transformer from CRS.`（pyproj 对 proj_create_crs_to_crs 返回空
的通用文案）。实测该报错对应影像为本地/未定义坐标系（GeoTIFF 只有 LOCAL_CS，
PROJ 中为 Engineering CRS），PROJ 无法建立到武汉本地 TM 的转换。

本脚本覆盖以下输入并断言行为：
1. 本地坐标系 + 坐标范围即武汉本地 TM → 降级为按源坐标处理，输出坐标不变；
2. 本地坐标系 + 普通局部坐标 → 抛出可执行的中文错误；
3. EPSG:4547（CGCS2000 3 度带 CM 114E）→ 正常坐标转换；
4. 工具自有武汉本地 TM WKT → 识别为同投影，跳过转换；
5. 无坐标系 → 回退 EPSG:3857；
6. ArcGIS 导出丢失投影参数、仅剩 Web Mercator 名称 → 恢复为 EPSG:3857。

另外直接校验坐标系恢复函数（ESRI PE String 优先、本地 TM 不误判）。

用法：python tools/selftest_dom_crs.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.transform import from_origin

from pipeline import coords
from pipeline.config import WUHAN_CGCS2000_GK_WKT
from pipeline.dom_extractor import DomExtractor, LOCAL_PROJ4

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


def _utf8_stdout() -> None:
    """Windows 控制台默认可能是 cp1252/cp936，打印中文会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _make_dom(path: str, crs, origin: tuple[float, float]) -> None:
    """合成一张带白色标线的简易 DOM（0.1m/px）。"""
    img = np.full((SIZE, SIZE, 3), 90, np.uint8)
    img[100:104, 40:SIZE - 40] = 235
    img[300:SIZE - 40, 200:205] = 235
    img[300:SIZE - 40, 400:405] = 235
    with rasterio.open(path, "w", driver="GTiff", height=SIZE, width=SIZE, count=3,
                       dtype="uint8", crs=crs,
                       transform=from_origin(origin[0], origin[1], 0.1, 0.1)) as ds:
        ds.write(img.transpose(2, 0, 1))


def _run_case(name: str, crs, origin, expect_ok: bool,
              expect_log: str | None = None) -> bool:
    tmp = tempfile.mkdtemp(prefix="dom_crs_selftest_")
    tif = os.path.join(tmp, "dom.tif")
    _make_dom(tif, crs, origin)
    logs: list[str] = []
    err = None
    try:
        DomExtractor().run(tif, os.path.join(tmp, "out"), wlog=logs.append)
    except Exception as e:      # noqa: BLE001 - 用例需要捕获任意失败
        err = e
    ok = err is None
    passed = ok == expect_ok
    if passed and expect_log:
        passed = any(expect_log in line for line in logs)
    print(f"[{'PASS' if passed else 'FAIL'}] {name}"
          + (f" -> {type(err).__name__}: {err}" if err else ""))
    return passed


def _check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}".rstrip())
    return bool(cond)


def test_recovery() -> list[bool]:
    """直接校验坐标系恢复：ESRI PE String 优先，名称+范围兜底。"""
    local_web = CRS.from_wkt(LOST_WEB_MERCATOR_WKT)
    bounds = (12729623.7, 3575205.6, 12731315.9, 3577825.1)

    got, note = coords.resolve_source_crs(local_web, bounds)
    ok_rt = False
    if got.to_epsg() == 3857:
        tf = coords.build_transformer(got, CRS.from_proj4(LOCAL_PROJ4))
        x, y = tf.transform(12730469.8, 3576515.35)
        ok_rt = coords.bounds_look_like_local_tm((x, y, x, y))

    got2, note2 = coords.resolve_source_crs(
        local_web, (780000, 400000, 780600, 400600))
    got3, note3 = coords.resolve_source_crs(
        local_web, bounds, ESRI_WEB_MERCATOR_WKT)
    return [
        _check("仅名称的 Web Mercator 恢复为 EPSG:3857",
               got.to_epsg() == 3857 and bool(note), note),
        _check("恢复后可转换到武汉本地 TM", ok_rt),
        _check("本地 TM 范围不误判为 Web Mercator",
               got2.is_engineering and not note2),
        _check("ESRI PE String 元数据优先恢复",
               got3.to_epsg() == 3857 and "元数据" in note3, note3),
    ]


def main() -> int:
    _utf8_stdout()
    results = test_recovery()
    results += [
        _run_case("本地坐标系（武汉范围）→ 按源坐标处理",
                  'LOCAL_CS["Nonearth",UNIT["Meter",1.0]]',
                  (780000, 400000), True, "按源坐标直接处理"),
        _run_case("本地坐标系（局部模型）→ 明确报错",
                  'LOCAL_CS["Nonearth",UNIT["Meter",1.0]]',
                  (0, 600), False),
        _run_case("EPSG:4547 → 正常转换",
                  "EPSG:4547", (500000, 3387000), True),
        _run_case("武汉本地 TM WKT → 跳过转换",
                  WUHAN_CGCS2000_GK_WKT, (780000, 400000), True,
                  "跳过坐标转换"),
        _run_case("无坐标系 → 回退 EPSG:3857",
                  None, (12700000, 3585000), True),
        _run_case("仅名称的 Web Mercator → 恢复后正常提取",
                  LOST_WEB_MERCATOR_WKT, (12729623, 3575205), True,
                  "EPSG:3857"),
    ]
    print(f"DOM_CRS_SELFTEST: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
