"""本轮修复的回归自检：变化检测 / 波段适配 / 分块重叠 / 比对窗口裁剪。

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

def _write_dom(path: str, img: np.ndarray, crs) -> None:
    import rasterio
    from rasterio.transform import from_origin
    count = 1 if img.ndim == 2 else img.shape[2]
    with rasterio.open(path, "w", driver="GTiff", height=img.shape[0],
                       width=img.shape[1], count=count, dtype="uint8", crs=crs,
                       transform=from_origin(780000, 400000, 0.1, 0.1)) as ds:
        if count == 1:
            ds.write(img, 1)
        else:
            ds.write(img.transpose(2, 0, 1))


def test_dom_tiles() -> None:
    from pipeline.config import WUHAN_CGCS2000_GK_WKT
    from pipeline.dom_extractor import DomExtractor

    tmp = tempfile.mkdtemp(prefix="dom_tiles_")

    # 3200×600：标线中心分别在第一块内部、第二块左缘 100px（旧逻辑丢弃带）
    img = np.full((600, 3200, 3), 90, np.uint8)
    for cx in (1500, 2148):
        img[296:304, cx - 80:cx + 80] = 235
    tif = os.path.join(tmp, "seam.tif")
    _write_dom(tif, img, WUHAN_CGCS2000_GK_WKT)
    rep = DomExtractor().run(tif, os.path.join(tmp, "out_seam"), wlog=lambda _s: None)
    check("分块接缝处要素不丢失", rep["lines"] == 2, f"lines={rep['lines']}")

    # 1 波段灰度 DOM
    gray = np.full((400, 900), 90, np.uint8)
    gray[196:204, 300:460] = 235
    gtif = os.path.join(tmp, "gray.tif")
    _write_dom(gtif, gray, WUHAN_CGCS2000_GK_WKT)
    rep = DomExtractor().run(gtif, os.path.join(tmp, "out_gray"), wlog=lambda _s: None)
    check("1 波段灰度 DOM 可提取", rep["lines"] >= 1, f"lines={rep['lines']}")


# ------------------------------------------------------------ 比对窗口裁剪

def test_comparator_edge() -> None:
    from shapely.geometry import Point
    from pipeline.comparator import Comparator

    tmp = tempfile.mkdtemp(prefix="cmp_edge_")
    img = np.full((400, 400, 3), 180, np.uint8)
    tif = os.path.join(tmp, "dom.tif")
    from pipeline.config import WUHAN_CGCS2000_GK_WKT
    _write_dom(tif, img, WUHAN_CGCS2000_GK_WKT)
    # 变化点贴近影像右下角：外扩 40m 的窗口必然越界
    changes = {"new": [("point", "x.shp", Point(780039.5, 399960.5), "")],
               "missing": [], "modified": []}
    stock = {"lines": [], "areas": [], "points": []}
    out = os.path.join(tmp, "preview.png")
    Comparator().render(None, stock, changes, tif, out)
    check("比对预览窗口自动裁剪", os.path.isfile(out))


def main() -> int:
    _utf8_stdout()
    test_change_detector()
    test_read_rgb()
    test_dom_tiles()
    test_comparator_edge()
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"REGRESSIONS_SELFTEST: {passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
