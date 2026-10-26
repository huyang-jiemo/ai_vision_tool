"""正射影像（DOM）要素提取：车道标线 / 面状标线 → GDB + SHP + 预览图。

流程：分块读取 BigTIFF → 局部对比度分割（白/黄标线掩膜）→ 轮廓分类
（细长=线、块状=面）→ 像素坐标转武汉本地 TM → 共线合并 → 落盘。

坐标约定：输出与存量 GDB 一致的武汉本地 TM（WUHAN_CGCS2000_GK_WKT），
DOM 若为 EPSG:3857 等其它坐标系，使用 `to_local` 变换链统一。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np

from .config import WUHAN_CGCS2000_GK_WKT

LOCAL_PROJ4 = ("+proj=tmerc +lat_0=0 +lon_0=114.3333333333333 +k=1 +x_0=800000 "
               "+y_0=-3000000 +a=6378137 +rf=298.257222101 +units=m +no_defs")

TILE = 2048
OVERLAP = 256


class DomExtractor:
    """正射影像标线提取器。"""

    def __init__(self, min_len=2.0, max_width=2.5, min_area=3.0, merge_snap=2.0,
                 tile=TILE, overlap=OVERLAP):
        self.min_len = float(min_len)
        self.max_width = float(max_width)
        self.min_area = float(min_area)
        self.merge_snap = float(merge_snap)
        self.tile = int(tile)
        self.overlap = int(overlap)

    # ---------- 掩膜 ----------
    @staticmethod
    def masks(img_rgb: np.ndarray):
        """返回 (white, yellow, road) 掩膜。road 用于剔除树冠/屋顶误检。"""
        gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
        hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
        local = gray.astype(np.int16) - cv2.medianBlur(gray, 31).astype(np.int16)
        white = ((local > 20) & (gray > 130) & (hsv[:, :, 1] < 90)).astype(np.uint8)
        yellow = ((hsv[:, :, 0] >= 18) & (hsv[:, :, 0] <= 35)
                  & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)).astype(np.uint8)
        # 路面：灰调、低饱和、低纹理（屋顶有强结构边界/阴影，路面均匀）
        road = ((gray > 50) & (gray < 185) & (hsv[:, :, 1] < 60)).astype(np.uint8)
        g = gray.astype(np.float32)
        mu = cv2.blur(g, (25, 25))
        mu2 = cv2.blur(g * g, (25, 25))
        std = np.sqrt(np.maximum(mu2 - mu * mu, 0))
        road &= (std < 32).astype(np.uint8)
        road = cv2.morphologyEx(road, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (81, 81)))
        road = cv2.morphologyEx(road, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))
        # 只保留大片连通域（路面/停车场），去掉小块误判
        n, lab, stats, _ = cv2.connectedComponentsWithStats(road, 8)
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < 80000:  # ≈80m²@3.2cm
                road[lab == i] = 0
        k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        white = cv2.morphologyEx(white, cv2.MORPH_OPEN, k3)
        yellow = cv2.morphologyEx(yellow, cv2.MORPH_CLOSE, k5)
        return white, yellow, road

    # ---------- 单轮廓分类 ----------
    def classify(self, cnt, res_m):
        """→ ('line', [(x,y),(x,y)] 像素) | ('poly', [pts] 像素) | None"""
        rect = cv2.minAreaRect(cnt)
        (cx, cy), (w, h), ang = rect
        if w <= 0 or h <= 0:
            return None
        long_s, short_s = max(w, h), min(w, h)
        area_px = cv2.contourArea(cnt)
        if (long_s * res_m >= self.min_len and short_s * res_m <= self.max_width
                and long_s / max(short_s, 1e-6) >= 3.0):
            import math
            if w >= h:
                ux, uy, L = math.cos(math.radians(ang)), math.sin(math.radians(ang)), w
            else:
                ux = math.cos(math.radians(ang + 90.0))
                uy = math.sin(math.radians(ang + 90.0))
                L = h
            p1 = (cx - ux * L / 2.0, cy - uy * L / 2.0)
            p2 = (cx + ux * L / 2.0, cy + uy * L / 2.0)
            return ("line", [p1, p2])
        if (area_px * res_m * res_m >= self.min_area
                and long_s / max(short_s, 1e-6) < 3.0):
            poly = cv2.approxPolyDP(cnt, 2.0, True)
            pts = [tuple(map(float, p[0])) for p in poly]
            if len(pts) >= 3:
                return ("poly", pts)
        return None

    # ---------- 主流程 ----------
    def run(self, tif_path: str, out_dir: str, stock_gdb: str | None = None,
            pad_m: float = 60.0, wlog=print, progress_cb=None, export_shp: bool = True,
            use_corridor: bool = True, corridor_half_m: float = 8.0,
            corridor_layers: tuple | None = ("LTSL", "LFCL")):
        import geopandas as gpd
        import rasterio
        import shapely
        from pyproj import CRS
        from rasterio.enums import Resampling
        from rasterio.windows import Window, from_bounds
        from shapely.geometry import LineString, Polygon

        t0 = time.time()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        dom = rasterio.open(tif_path)
        src_crs = CRS.from_user_input(dom.crs) if dom.crs else CRS.from_epsg(3857)
        local = CRS.from_proj4(LOCAL_PROJ4)
        to_local = None
        if not src_crs.equals(local):
            from pyproj import Transformer
            to_local = Transformer.from_crs(src_crs, local, always_xy=True)

        # 处理范围：默认全图；给定存量 GDB 时按其范围 + pad 裁剪（实用场景：图幅更新）
        if stock_gdb and Path(stock_gdb).exists():
            try:
                b = self._stock_bounds(stock_gdb, src_crs)
                win = from_bounds(b[0] - pad_m, b[1] - pad_m, b[2] + pad_m, b[3] + pad_m,
                                  dom.transform)
                win = win.intersection(rasterio.windows.Window(0, 0, dom.width, dom.height))
                win = win.round_offsets().round_lengths()
                wlog(f"处理范围：按存量图幅裁剪（{win.width:.0f}×{win.height:.0f}px）")
            except Exception as e:
                wlog(f"存量范围裁剪失败，改为全图处理：{e}")
                win = rasterio.windows.Window(0, 0, dom.width, dom.height)
        else:
            win = rasterio.windows.Window(0, 0, dom.width, dom.height)
            wlog(f"处理范围：全图（{win.width:.0f}×{win.height:.0f}px）")

        # 存量走廊约束：仅保留存量「线状道路要素」缓冲范围内的提取结果
        # （对标基准 GDB 的交通标线口径：道路以外不提取，建筑/屋顶/停车场一律剔除）
        corridor = None
        if stock_gdb and Path(stock_gdb).exists() and use_corridor:
            try:
                import geopandas as gpd
                import shapely
                from pyproj import CRS
                local_crs = CRS.from_proj4(LOCAL_PROJ4)
                import pyogrio as _og
                all_layers = [n for n, _gt in _og.list_layers(stock_gdb)
                              if not n.startswith("~") and not n.endswith(".xlsx")]
                want = [n for n in (corridor_layers or ()) if n in all_layers]
                names = want or all_layers
                kind = "线状道路要素" if want else "全部要素（未找到线状图层）"
                geoms = []
                for name in names:
                    try:
                        g = gpd.read_file(stock_gdb, layer=name)
                    except Exception:
                        continue
                    if g.empty:
                        continue
                    geoms += [p for gg in g.geometry for p in shapely.get_parts([gg])]
                if geoms:
                    corridor = shapely.union_all(
                        [g.buffer(corridor_half_m) for g in geoms])
                    corridor = shapely.prepared.prep(corridor)
                    wlog(f"存量走廊约束：开启（{kind} ±{corridor_half_m}m，"
                         f"{len(names)} 个图层 / {len(geoms)} 个要素，"
                         f"道路以外区域不提取）")
                else:
                    wlog("存量走廊约束：无可用要素，按路面掩膜提取")
            except Exception as e:
                corridor = None
                wlog(f"走廊构建失败，不启用走廊约束：{e}")
        elif stock_gdb and not use_corridor:
            wlog("存量走廊约束：关闭（提取全范围，建筑等可能进入成果）")

        res_m = abs(dom.transform.a)  # 源坐标系单位/像素
        nx = int(np.ceil(win.width / self.tile))
        ny = int(np.ceil(win.height / self.tile))
        total = nx * ny
        wlog(f"分块处理：{nx}×{ny}={total} 块（{self.tile}px/块，重叠 {self.overlap}px）")

        lines, polys = [], []
        done = 0
        for iy in range(ny):
            for ix in range(nx):
                c0, r0 = ix * self.tile, iy * self.tile
                c1 = min(c0 + self.tile, int(win.width))
                r1 = min(r0 + self.tile, int(win.height))
                if c1 - c0 < 64 or r1 - r0 < 64:
                    done += 1
                    continue
                sub = Window(int(win.col_off) + c0, int(win.row_off) + r0,
                             c1 - c0, r1 - r0)
                img = dom.read([1, 2, 3], window=sub).transpose(1, 2, 0)
                done += 1
                if img.size == 0 or img.max() == 0:
                    continue
                sub_t = dom.window_transform(sub)
                wm, ym, road = self.masks(img)
                for m in (wm, ym):
                    cnts, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
                    for cnt in cnts:
                        r = self.classify(cnt, res_m)
                        if r is None:
                            continue
                        kind, pts = r
                        M = cv2.moments(cnt)
                        if M["m00"] == 0:
                            continue
                        pcx = M["m10"] / M["m00"] + c0
                        pcy = M["m01"] / M["m00"] + r0
                        if (pcx < self.overlap and ix > 0) or (pcy < self.overlap and iy > 0):
                            continue  # 重叠带去重
                        # 路面约束：采样点多数落在路面上才保留
                        if kind == "line":
                            ss = [pts[0], pts[1],
                                  ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)]
                            on_road = sum(1 for p in ss
                                          if 0 <= p[0] < road.shape[1] and 0 <= p[1] < road.shape[0]
                                          and road[int(p[1]), int(p[0])] > 0)
                            if on_road < 2:
                                continue
                        else:
                            poly_a = np.array(pts, np.float64)
                            cxx, cyy = poly_a[:, 0].mean(), poly_a[:, 1].mean()
                            if not (0 <= cxx < road.shape[1] and 0 <= cyy < road.shape[0]
                                    and road[int(cyy), int(cxx)] > 0):
                                continue
                        world = [sub_t * (p[0], p[1]) for p in pts]
                        if to_local:
                            world = [to_local.transform(x, y) for x, y in world]
                        # 存量走廊过滤（世界坐标为本地系）
                        if corridor is not None:
                            test = (LineString(world) if kind == "line"
                                    else Polygon(world).representative_point())
                            ok = (corridor.intersects(test) if kind == "line"
                                  else corridor.contains(test))
                            if not ok:
                                continue
                        if kind == "line":
                            lines.append(LineString(world))
                        else:
                            p = Polygon(world)
                            if p.is_valid and p.area >= self.min_area:
                                polys.append(p)
                if progress_cb:
                    progress_cb(done, total, f"已处理 {done}/{total} 块")

        wlog(f"原始提取：线 {len(lines)} 条，面 {len(polys)} 个")
        lines = self.merge_collinear(lines)
        wlog(f"共线合并后：线 {len(lines)} 条，面 {len(polys)} 个")

        # 落盘
        gdb_path = out / "正射提取.gdb"
        layers_written = []
        try:
            if lines:
                gdf = gpd.GeoDataFrame(
                    {"SID": [i + 1 for i in range(len(lines))],
                     "LEN_M": [round(g.length, 2) for g in lines],
                     "SRC": ["DOM"] * len(lines)},
                    geometry=lines, crs=WUHAN_CGCS2000_GK_WKT)
                gdf.to_file(str(gdb_path), layer="lanes", driver="OpenFileGDB")
                layers_written.append("lanes")
            if polys:
                gdf = gpd.GeoDataFrame(
                    {"SID": [i + 1 for i in range(len(polys))],
                     "AREA_M2": [round(g.area, 2) for g in polys],
                     "SRC": ["DOM"] * len(polys)},
                    geometry=polys, crs=WUHAN_CGCS2000_GK_WKT)
                gdf.to_file(str(gdb_path), layer="lane_areas", driver="OpenFileGDB")
                layers_written.append("lane_areas")
        except Exception as e:
            wlog(f"GDB 写出失败：{e}")

        shp_dir = out / "shp"
        shp_files = []
        if export_shp:
            try:
                if shp_dir.exists():
                    for old in shp_dir.glob("*"):
                        if old.suffix.lower() in (".shp", ".dbf", ".shx", ".prj", ".cpg"):
                            old.unlink()
                else:
                    shp_dir.mkdir(parents=True, exist_ok=True)
                if lines:
                    gpd.GeoDataFrame({"SID": range(1, len(lines) + 1)},
                                     geometry=lines, crs=WUHAN_CGCS2000_GK_WKT
                                     ).to_file(str(shp_dir / "lanes.shp"), encoding="utf-8")
                    shp_files.append(str(shp_dir / "lanes.shp"))
                if polys:
                    gpd.GeoDataFrame({"SID": range(1, len(polys) + 1)},
                                     geometry=polys, crs=WUHAN_CGCS2000_GK_WKT
                                     ).to_file(str(shp_dir / "lane_areas.shp"), encoding="utf-8")
                    shp_files.append(str(shp_dir / "lane_areas.shp"))
            except Exception as e:
                wlog(f"SHP 写出失败：{e}")

        # 预览图
        preview = str(out / "提取预览.png")
        try:
            self.render_preview(dom, win, lines, polys, preview,
                                to_local is not None, local, src_crs)
            wlog("预览图已生成：提取预览.png")
        except Exception as e:
            wlog(f"预览图生成失败：{e}")
            preview = ""

        report = {
            "tif": str(tif_path),
            "crs": str(dom.crs),
            "window_px": [int(win.width), int(win.height)],
            "res_m": round(res_m, 4),
            "lines": len(lines),
            "areas": len(polys),
            "gdb": str(gdb_path),
            "layers": layers_written,
            "shp": shp_files,
            "preview": preview,
            "seconds": round(time.time() - t0, 1),
        }
        (out / "正射提取报告.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        wlog(f"完成：线 {len(lines)} / 面 {len(polys)}，耗时 {report['seconds']}s")
        return report

    # ---------- 工具 ----------
    def _stock_bounds(self, stock_gdb, src_crs):
        import geopandas as gpd
        import pyogrio
        from pyproj import CRS
        local = CRS.from_proj4(LOCAL_PROJ4)
        bs = []
        for name, _geom in pyogrio.list_layers(stock_gdb):
            if name.startswith("~") or name.endswith(".xlsx"):
                continue
            try:
                g = gpd.read_file(stock_gdb, layer=name)
            except Exception:
                continue
            if g.empty:
                continue
            g = g.set_crs(local, allow_override=True).to_crs(src_crs)
            bs.append(g.total_bounds)
        if not bs:
            raise RuntimeError("存量 GDB 无有效图层")
        arr = np.array(bs)
        return (float(arr[:, 0].min()), float(arr[:, 1].min()),
                float(arr[:, 2].max()), float(arr[:, 3].max()))

    def merge_collinear(self, lines):
        from shapely.geometry import LineString
        lines = [g for g in lines if g.length >= self.min_len]
        snap = self.merge_snap
        for _ in range(5):
            merged_any = False
            used = [False] * len(lines)
            out = []
            for i in range(len(lines)):
                if used[i]:
                    continue
                g = lines[i]
                used[i] = True
                changed = True
                while changed:
                    changed = False
                    for j in range(len(lines)):
                        if used[j]:
                            continue
                        h = lines[j]
                        ge, he = (g.coords[0], g.coords[-1]), (h.coords[0], h.coords[-1])
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
                        # 方向一致性（避免把拐角连成一条）
                        v1 = np.array(pts[1]) - np.array(pts[0])
                        v2 = np.array(pts[-1]) - np.array(pts[-2])
                        if np.linalg.norm(v1) > 0 and np.linalg.norm(v2) > 0:
                            cosang = abs(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))
                            if cosang < 0.95:
                                continue
                        g = LineString(pts)
                        used[j] = True
                        changed = merged_any = True
                out.append(g)
            lines = out
            if not merged_any:
                break
        return lines

    def render_preview(self, dom, win, lines, polys, out_png,
                       need_back: bool = False, local=None, src_crs=None):
        import rasterio
        from rasterio.enums import Resampling
        OW = 1600
        scale = win.width / OW
        OH = max(1, int(win.height / scale))
        wt = dom.window_transform(win)
        img = dom.read([1, 2, 3], window=win, out_shape=(3, OH, OW),
                       resampling=Resampling.bilinear).transpose(1, 2, 0)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        inv = ~wt

        back = None
        if need_back and local is not None and src_crs is not None:
            from pyproj import Transformer
            back = Transformer.from_crs(local, src_crs, always_xy=True)

        def w2p(x, y):
            if back is not None:
                x, y = back.transform(x, y)
            c, r = inv * (x, y)
            return c / scale, r / scale

        for p in polys:
            ext = np.array([w2p(*c) for c in p.exterior.coords], np.int32)
            cv2.polylines(img, [ext], True, (0, 220, 255), 2, cv2.LINE_AA)
        for g in lines:
            pts = np.array([w2p(*c) for c in g.coords], np.int32)
            cv2.polylines(img, [pts], False, (0, 255, 0), 1, cv2.LINE_AA)
        cv2.imwrite(out_png, img)
        return out_png
