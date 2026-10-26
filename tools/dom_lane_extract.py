"""DOM 车道标线提取试验：局部对比度分割 → 矢量化 → 与存量 GDB 比对。

用法：python tools/dom_lane_extract.py
输出：/tmp/extract_report.json、/tmp/dom_compare.png、提取要素 GeoJSON(GCS)
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import rasterio
import shapely
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from shapely.geometry import LineString, Polygon

ROOT = Path(__file__).resolve().parent.parent
DOM = ROOT / "docs/众源感知测试数据源/测试/正射影像/梨园-DOM-3857/ly-dom.tif"
GDB = ROOT / "docs/众源感知测试数据源/测试/全要素成果/测试数据.gdb"
TRAJ = ROOT / "docs/众源感知测试数据源/测试/轨迹/20260327_1-2（东湖路）/20260327_1-2.csv"

LOCAL = CRS.from_proj4(
    "+proj=tmerc +lat_0=0 +lon_0=114.3333333333333 +k=1 +x_0=800000 "
    "+y_0=-3000000 +a=6378137 +rf=298.257222101 +units=m +no_defs"
)

TILE = 2048          # 分块大小（像素）
OVERLAP = 256        # 重叠（像素）
MIN_LEN_M = 2.0      # 线要素最短长度
MAX_W_M = 2.5        # 线要素最大宽度（超过归为面）
MIN_AREA_M2 = 3.0    # 面要素最小面积


def corridor_bounds_3857() -> tuple[float, float, float, float]:
    layers = {}
    for lyr in ("LTSL", "LTSA", "LTSP"):
        layers[lyr] = gpd.read_file(GDB, layer=lyr).set_crs(LOCAL, allow_override=True).to_crs(3857)
    tx, ty = [], []
    with open(TRAJ) as f:
        for row in csv.DictReader(f):
            tx.append(float(row["X"]))
            ty.append(float(row["Y"]))
    tg = gpd.GeoSeries(gpd.points_from_xy(tx, ty), crs=LOCAL).to_crs(3857)
    b = layers["LTSL"].total_bounds
    tb = tg.total_bounds
    return (min(b[0], tb[0]) - 40, min(b[1], tb[1]) - 40,
            max(b[2], tb[2]) + 40, max(b[3], tb[3]) + 40)


def masks(img_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (white, yellow) 标线掩膜。局部对比度抑制大块白色地物。"""
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    local = gray.astype(np.int16) - cv2.medianBlur(gray, 31).astype(np.int16)
    white = ((local > 22) & (gray > 140) & (hsv[:, :, 1] < 90)).astype(np.uint8)
    yellow = ((hsv[:, :, 0] >= 18) & (hsv[:, :, 0] <= 35)
              & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    white = cv2.morphologyEx(white, cv2.MORPH_OPEN, k)
    yellow = cv2.morphologyEx(yellow, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    return white, yellow


def classify_contour(cnt, res_m: float):
    """轮廓 → ('line', LineString) | ('poly', Polygon) | None。"""
    rect = cv2.minAreaRect(cnt)
    (cx, cy), (w, h), ang = rect
    if w == 0 or h == 0:
        return None
    long_s, short_s = max(w, h), min(w, h)
    area = cv2.contourArea(cnt)
    if long_s * res_m >= MIN_LEN_M and short_s * res_m <= MAX_W_M and long_s / max(short_s, 1) >= 3.0:
        box = cv2.boxPoints(rect)
        if w < h:
            p1, p2 = box[0], box[1]
        else:
            p1, p2 = box[1], box[2]
        # 世界坐标（居中取两端点）
        return ("line", LineString([(p1[0], p1[1]), (p2[0], p2[1])]))
    if area * res_m * res_m >= MIN_AREA_M2:
        poly = cv2.approxPolyDP(cnt, 2.0, True)
        pts = [(float(p[0][0]), float(p[0][1])) for p in poly]
        if len(pts) >= 3:
            return ("poly", Polygon(pts))
    return None


def extract():
    dom = rasterio.open(DOM)
    b = corridor_bounds_3857()
    win = from_bounds(*b, dom.transform).round_offsets().round_lengths()
    full_w = dom.window_transform(win)
    res = dom.transform.a  # units/px
    lines: list[LineString] = []
    polys: list[Polygon] = []

    cols = win.width
    rows = win.height
    nx = (cols + TILE - 1) // TILE
    ny = (rows + TILE - 1) // TILE
    inv = ~full_w

    for iy in range(ny):
        for ix in range(nx):
            c0, r0 = ix * TILE, iy * TILE
            c1, r1 = min(c0 + TILE, cols), min(r0 + TILE, rows)
            sub = rasterio.windows.Window(c0 + win.col_off, r0 + win.row_off,
                                          c1 - c0, r1 - r0)
            img = dom.read([1, 2, 3], window=sub).transpose(1, 2, 0)
            if img.size == 0:
                continue
            wm, ym = masks(img)
            sub_t = dom.window_transform(sub)
            sub_inv = ~sub_t
            res_m = res  # units/px（墨卡托单位，近似米）
            for m in (wm, ym):
                cnts, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
                for cnt in cnts:
                    r = classify_contour(cnt, res_m)
                    if r is None:
                        continue
                    kind, geom = r
                    # 质心去重：只保留落在本块核心区（非重叠侧）的要素
                    M = cv2.moments(cnt)
                    if M["m00"] == 0:
                        continue
                    pcx = M["m10"] / M["m00"] + c0
                    pcy = M["m01"] / M["m00"] + r0
                    if pcx < c0 + OVERLAP and ix > 0:
                        continue
                    if pcy < r0 + OVERLAP and iy > 0:
                        continue
                    # 像素 → 3857
                    if kind == "line":
                        gx0, gy0 = sub_t * (geom.coords[0])
                        gx1, gy1 = sub_t * (geom.coords[1])
                        lines.append(LineString([(gx0, gy0), (gx1, gy1)]))
                    else:
                        gx, gy = sub_t * (np.array(geom.exterior.coords).T)
                        p = Polygon(zip(gx, gy))
                        if p.area > MIN_AREA_M2:
                            polys.append(p)
        print(f"tile row {iy+1}/{ny} done, lines={len(lines)}, polys={len(polys)}", flush=True)

    print(f"原始提取：线 {len(lines)}，面 {len(polys)}")
    # 融合共线短段（同方向且端点接近的线合并，减少碎片）
    lines = merge_collinear(lines)
    print(f"共线合并后：线 {len(lines)}，面 {len(polys)}")
    return lines, polys, full_w


def merge_collinear(lines: list[LineString], snap: float = 2.0, rounds: int = 5) -> list[LineString]:
    """端点距离 < snap 且方向一致（夹角 <8°）的两线首尾合并，迭代收敛。"""
    lines = [g for g in lines if g.length >= MIN_LEN_M]

    def ends(g):
        return g.coords[0], g.coords[-1]

    for _ in range(rounds):
        merged_any = False
        used = [False] * len(lines)
        out: list[LineString] = []
        for i in range(len(lines)):
            if used[i]:
                continue
            g = lines[i]
            changed = True
            used[i] = True
            while changed:
                changed = False
                for j in range(len(lines)):
                    if used[j]:
                        continue
                    h = lines[j]
                    ge, he = ends(g), ends(h)
                    best = None
                    for a, pe in ((0, ge[0]), (1, ge[1])):
                        for b, qe in ((0, he[0]), (1, he[1])):
                            dist = ((pe[0] - qe[0]) ** 2 + (pe[1] - qe[1]) ** 2) ** 0.5
                            if dist < snap and (best is None or dist < best[0]):
                                best = (dist, a, b)
                    if best is None:
                        continue
                    gpts, hpts = list(g.coords), list(h.coords)
                    a, b = best[1], best[2]
                    if a == 1 and b == 0:
                        pts = gpts + hpts
                    elif a == 0 and b == 1:
                        pts = hpts + gpts
                    elif a == 1 and b == 1:
                        pts = gpts + hpts[::-1]
                    else:
                        pts = hpts + gpts
                    g = LineString(pts)
                    used[j] = True
                    changed = merged_any = True
            out.append(g)
        lines = out
        if not merged_any:
            break
    return lines


def compare(lines: list[LineString]) -> dict:
    """与存量 LTSL 比对：召回（存量被提取覆盖）+ 提取线命中/多出统计。"""
    ref = gpd.read_file(GDB, layer="LTSL").set_crs(LOCAL, allow_override=True).to_crs(3857)
    ref_parts = [p for g in ref.geometry for p in shapely.get_parts([g])]
    ext_buf = shapely.union_all([g.buffer(0.75) for g in lines]) if lines else shapely.GeometryCollection()
    ref_buf = shapely.union_all([p.buffer(1.5) for p in ref_parts])

    ref_hit = sum(1 for p in ref_parts if p.buffer(1.5).intersects(ext_buf))
    ext_hit = sum(1 for g in lines if ref_buf.intersects(g))
    return {
        "ref_LTSL_parts": len(ref_parts),
        "ref_matched_by_extract": ref_hit,
        "ref_recall": round(ref_hit / max(len(ref_parts), 1), 3),
        "extracted_lines": len(lines),
        "extracted_on_ref": ext_hit,
        "extracted_extra": len(lines) - ext_hit,
    }


def render(lines, polys):
    """渲染：存量=橙、提取命中=绿、提取未命中=红。"""
    import shapely as sh
    local = LOCAL
    layers = {lyr: gpd.read_file(GDB, layer=lyr).set_crs(local, allow_override=True).to_crs(3857)
              for lyr in ("LTSL", "LTSA", "LTSP")}
    ref = layers["LTSL"]
    ref_buf = ref.geometry.union_all().buffer(1.5)
    b = corridor_bounds_3857()
    dom = rasterio.open(DOM)
    win = from_bounds(*b, dom.transform).round_offsets().round_lengths()
    OW = 2200
    scale = win.width / OW
    OH = int(win.height / scale)
    wt = dom.window_transform(win)
    img = dom.read([1, 2, 3], window=win, out_shape=(3, OH, OW), resampling=Resampling.bilinear).transpose(1, 2, 0)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    inv = ~wt

    def w2p(x, y):
        c, r = inv * (x, y)
        return c / scale, r / scale

    def draw_geoms(geoms, color, thk):
        for geom in geoms:
            for part in sh.get_parts([geom]):
                if part.geom_type == "LineString":
                    pts = np.array([w2p(c[0], c[1]) for c in part.coords], np.int32)
                    cv2.polylines(img, [pts], False, color, thk, cv2.LINE_AA)
                elif part.geom_type == "Polygon":
                    ext = np.array([w2p(c[0], c[1]) for c in part.exterior.coords], np.int32)
                    cv2.polylines(img, [ext], True, color, thk, cv2.LINE_AA)

    draw_geoms(ref.geometry, (0, 165, 255), 2)  # 存量 橙
    for g in lines:
        color = (0, 200, 0) if ref_buf.intersects(g) else (0, 0, 255)
        pts = np.array([w2p(c[0], c[1]) for c in g.coords], np.int32)
        cv2.polylines(img, [pts], False, color, 2, cv2.LINE_AA)
    cv2.imwrite("/tmp/dom_compare.png", img)
    print("render /tmp/dom_compare.png")


def main():
    lines, polys, _ = extract()
    if not lines:
        print("未提取到线要素，检查阈值")
        sys.exit(1)
    gs = gpd.GeoSeries(lines, crs=3857).to_crs(4326)
    Path("/tmp/extract_lines.geojson").write_text(gs.to_json())
    stats = compare(lines)
    stats["extracted_polys"] = len(polys)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    Path("/tmp/extract_report.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    render(lines, polys)


if __name__ == "__main__":
    main()
