"""正射提取坐标系分支自检（合成 DOM，不依赖真实数据）。

回归背景：v0.5.6 在 Windows 真机上报
`Error creating Transformer from CRS.`（pyproj 对 proj_create_crs_to_crs 返回空
的通用文案）。实测该报错对应影像为本地/未定义坐标系（GeoTIFF 只有 LOCAL_CS，
PROJ 中为 Engineering CRS），PROJ 无法建立到武汉本地 TM 的转换。

本脚本覆盖五种输入并断言行为：
1. 本地坐标系 + 坐标范围即武汉本地 TM → 降级为按源坐标处理，输出坐标不变；
2. 本地坐标系 + 普通局部坐标 → 抛出可执行的中文错误；
3. EPSG:4547（CGCS2000 3 度带 CM 114E）→ 正常坐标转换；
4. 工具自有武汉本地 TM WKT → 识别为同投影，跳过转换；
5. 无坐标系 → 回退 EPSG:3857。

用法：python tools/selftest_dom_crs.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from rasterio.transform import from_origin

from pipeline.config import WUHAN_CGCS2000_GK_WKT
from pipeline.dom_extractor import DomExtractor

SIZE = 600


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


def main() -> int:
    results = [
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
    ]
    print(f"DOM_CRS_SELFTEST: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
