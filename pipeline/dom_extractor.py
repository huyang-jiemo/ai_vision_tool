"""正射影像（DOM）要素提取：车道标线 / 面状标线 → GDB + SHP + 预览图。

流程：分块读取 BigTIFF → 局部对比度分割（白/黄标线掩膜）→ 轮廓分类
（细长=线、块状=面）→ 像素坐标转 working CRS → 共线合并 → 转目标 CRS 落盘。

坐标约定：输入 CRS 从影像自身读取（缺失时由用户指定）；输出默认跟随影像，
可用 output_crs 覆盖；内部计算使用米制 working CRS，阈值/面积不受源单位影响。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import cv2
import numpy as np

from . import coords, crs_plan
from .exporter import clean_vector_outputs, clear_file_dataset
from .imgio import read_rgb

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
            pad_m: float = 60.0, wlog=print, progress_cb=None, export_shp: bool = False,
            use_corridor: bool = True, corridor_half_m: float = 8.0,
            corridor_layers: tuple | None = ("LTSL", "LFCL"),
            input_crs=None, stock_crs=None, output_crs=None,
            use_stock_clip: bool = True):
        import geopandas as gpd
        import rasterio
        import shapely
        from rasterio.windows import Window, from_bounds
        from shapely.geometry import LineString, Polygon, box

        t0 = time.time()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        dom = rasterio.open(tif_path)
        src_crs, src_origin, crs_note = coords.resolve_raster_crs(
            dom, input_crs, field="input_crs")
        target_override = coords.parse_crs(output_crs, field="output_crs")
        plan = crs_plan.plan_for_bounds(
            src_crs, target_override, dom.bounds,
            source_origin=src_origin,
            target_origin="user" if target_override is not None else "default:source")
        target_crs = plan.target_crs
        working_crs = plan.working_crs
        to_working = plan.to_working()
        to_target = plan.to_target()
        wlog(f"影像坐标系：{coords.crs_label(src_crs)}"
             + (f"（{crs_note}）" if crs_note else ""))
        wlog(f"工作坐标系：{coords.crs_label(working_crs)}（{plan.working_reason}）；"
             f"输出坐标系：{coords.crs_label(target_crs)}")
        wlog(f"影像范围：{tuple(round(v, 1) for v in dom.bounds)}")

        # 存量 GDB：逐层读取真实 CRS；缺失层用 stock_crs，仍缺失则按邻近
        # 矢量与影像范围自动推断；证据不足才报错要求用户填写。
        stock_info = None
        if stock_gdb and Path(stock_gdb).exists():
            stock_info = coords.inspect_gdb_crs(
                stock_gdb, stock_crs, field="stock_crs",
                infer_roots=coords.inference_roots(tif_path, stock_gdb),
                anchor_crs=src_crs, anchor_bounds=dom.bounds)
            if stock_info.origin == "mixed":
                extra = "（混合 CRS，逐层转换）"
            elif stock_info.origin == "inferred":
                extra = f"（自动判断：{stock_info.note}）"
            else:
                extra = ""
            wlog(f"存量坐标系：{coords.crs_label(stock_info.primary)}{extra}")

        # 处理范围：默认全图；启用存量裁剪时按其范围 + pad 裁剪（图幅更新场景）。
        # 裁剪与走廊是两个独立开关：不裁剪时仍可用存量构造道路走廊。
        if stock_info is not None and use_stock_clip:
            try:
                b = self._stock_bounds(stock_gdb, stock_info, working_crs)
                b_src = coords.transform_bounds_with(
                    (b[0] - pad_m, b[1] - pad_m, b[2] + pad_m, b[3] + pad_m),
                    plan.to_source())
                win = from_bounds(*b_src, dom.transform)
                win = win.intersection(rasterio.windows.Window(0, 0, dom.width, dom.height))
                win = win.round_offsets().round_lengths()
                wlog(f"处理范围：按存量图幅裁剪（{win.width:.0f}×{win.height:.0f}px）")
            except coords.CRSError:
                raise
            except Exception as e:
                wlog(f"存量范围裁剪失败，改为全图处理：{e}")
                win = rasterio.windows.Window(0, 0, dom.width, dom.height)
        else:
            win = rasterio.windows.Window(0, 0, dom.width, dom.height)
            why = "未启用存量裁剪" if stock_info is not None else "全图"
            wlog(f"处理范围：{why}（{win.width:.0f}×{win.height:.0f}px）")

        # 存量走廊约束：仅保留存量「线状道路要素」缓冲范围内的提取结果
        # （对标基准 GDB 的交通标线口径：道路以外不提取，建筑/屋顶/停车场一律剔除）
        corridor = None
        if stock_info is not None and use_corridor:
            try:
                import pyogrio as _og
                all_layers = [n for n, gt in _og.list_layers(stock_gdb)
                              if gt and not n.startswith("~")
                              and not n.endswith(".xlsx")]
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
                    if g.crs is None:
                        g = g.set_crs(stock_info.per_layer.get(name),
                                      allow_override=True)
                    g = g.to_crs(working_crs)
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
            except coords.CRSError:
                raise
            except Exception as e:
                corridor = None
                wlog(f"走廊构建失败，不启用走廊约束：{e}")
        elif stock_info is not None and not use_corridor:
            wlog("存量走廊约束：关闭（提取全范围，建筑等可能进入成果）")

        res_m = self._center_res_m(dom, to_working, win)
        nx = int(np.ceil(win.width / self.tile))
        ny = int(np.ceil(win.height / self.tile))
        total = nx * ny
        wlog(f"分块处理：{nx}×{ny}={total} 块（{self.tile}px/块，重叠 {self.overlap}px）")

        lines, polys = [], []
        done = 0
        for iy in range(ny):
            for ix in range(nx):
                c0, r0 = ix * self.tile, iy * self.tile
                # 读取窗口必须真正带 overlap：否则块边界处的目标会被切断，
                # 而下方「重叠带去重」会直接丢弃这些位置的真实要素。
                c1 = min(c0 + self.tile + self.overlap, int(win.width))
                r1 = min(r0 + self.tile + self.overlap, int(win.height))
                if c1 - c0 < 64 or r1 - r0 < 64:
                    done += 1
                    continue
                sub = Window(int(win.col_off) + c0, int(win.row_off) + r0,
                             c1 - c0, r1 - r0)
                img = read_rgb(dom, window=sub).transpose(1, 2, 0)
                done += 1
                if img.size == 0 or img.max() == 0:
                    continue
                sub_t = dom.window_transform(sub)
                tile_res_m = self._tile_res_m(dom, sub_t, sub, to_working)
                wm, ym, road = self.masks(img)
                # 每块只保留自己的 tile×tile 区域。读入的 overlap 像素仅用于
                # 让跨块目标在块内完整成形；若把 overlap 段也写出，长线会在
                # 相邻块各输出一次（端点不重合，merge_collinear 也合不了）。
                own = box(0, 0, min(self.tile, c1 - c0),
                          min(self.tile, r1 - r0))
                for m in (wm, ym):
                    cnts, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
                    for cnt in cnts:
                        r = self.classify(cnt, tile_res_m)
                        if r is None:
                            continue
                        kind, pts = r
                        geom = (LineString(pts) if kind == "line"
                                else Polygon(pts))
                        if geom.is_empty:
                            continue
                        try:
                            clipped = geom.intersection(own)
                        except Exception:
                            continue
                        for part in shapely.get_parts([clipped]):
                            if part.is_empty:
                                continue
                            if kind == "line":
                                if part.geom_type not in ("LineString", "LinearRing"):
                                    continue
                                pp = list(part.coords)
                                if len(pp) < 2:
                                    continue
                                # 路面约束：首尾与中点多数落在路面上才保留
                                ss = [pp[0], pp[-1],
                                      ((pp[0][0] + pp[-1][0]) / 2,
                                       (pp[0][1] + pp[-1][1]) / 2)]
                                on_road = sum(
                                    1 for p in ss
                                    if 0 <= p[0] < road.shape[1]
                                    and 0 <= p[1] < road.shape[0]
                                    and road[int(p[1]), int(p[0])] > 0)
                                if on_road < 2:
                                    continue
                                world = [sub_t * (p[0], p[1]) for p in pp]
                            else:
                                if part.geom_type != "Polygon":
                                    continue
                                cxx, cyy = part.representative_point().x, part.representative_point().y
                                if not (0 <= cxx < road.shape[1]
                                        and 0 <= cyy < road.shape[0]
                                        and road[int(cyy), int(cxx)] > 0):
                                    continue
                                world = [sub_t * (p[0], p[1])
                                         for p in part.exterior.coords]
                            if to_working is not None:
                                xs = [p[0] for p in world]
                                ys = [p[1] for p in world]
                                tx, ty = to_working.transform(xs, ys)
                                world = list(zip(tx, ty))
                            # 存量走廊过滤（世界坐标为 working 米制）
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
        # 收尾三个阶段纳入进度刻度，避免最后 3% 长时间无反馈
        N_TAIL = 3
        wlog("正在合并共线要素…")
        if progress_cb:
            progress_cb(total, total + N_TAIL, "正在合并共线要素…")
        lines = self.merge_collinear(lines)
        wlog(f"共线合并后：线 {len(lines)} 条，面 {len(polys)} 个")
        wlog("正在写出 GDB…")
        if progress_cb:
            progress_cb(total + 1, total + N_TAIL, "正在写出 GDB…")

        # 落盘：属性在 working 米制下计算，几何转目标 CRS 后写出。
        lines_out = self._transform_geoms(lines, to_target)
        polys_out = self._transform_geoms(polys, to_target)
        gdb_path = out / "正射提取.gdb"
        layers_written = []
        # 复用输出目录时必须先删除旧 GDB，否则上次运行的图层会残留，
        # 无要素时还会把旧成果当成本次结果报告。
        gdb_error = clear_file_dataset(gdb_path)
        if gdb_error:
            wlog(f"GDB 无法重建：{gdb_error}")
        else:
            try:
                if lines_out:
                    gdf = gpd.GeoDataFrame(
                        {"SID": [i + 1 for i in range(len(lines_out))],
                         "LEN_M": [round(g.length, 2) for g in lines],
                         "SRC": ["DOM"] * len(lines_out)},
                        geometry=lines_out, crs=target_crs)
                    gdf.to_file(str(gdb_path), layer="lanes", driver="OpenFileGDB")
                    layers_written.append("lanes")
                if polys_out:
                    gdf = gpd.GeoDataFrame(
                        {"SID": [i + 1 for i in range(len(polys_out))],
                         "AREA_M2": [round(g.area, 2) for g in polys],
                         "SRC": ["DOM"] * len(polys_out)},
                        geometry=polys_out, crs=target_crs)
                    gdf.to_file(str(gdb_path), layer="lane_areas", driver="OpenFileGDB")
                    layers_written.append("lane_areas")
            except Exception as e:
                wlog(f"GDB 写出失败：{e}")
        gdb_written = bool(layers_written) and gdb_path.exists()
        if progress_cb:
            progress_cb(total + 2, total + N_TAIL, "正在生成预览图…")

        shp_dir = out / "shp"
        shp_files = []
        if shp_dir.is_dir():
            for prefix in ("lanes.", "lane_areas."):
                for msg in clean_vector_outputs(shp_dir, prefix=prefix):
                    wlog(msg)
        if export_shp:
            try:
                shp_dir.mkdir(parents=True, exist_ok=True)
                if lines_out:
                    gpd.GeoDataFrame(
                        {"SID": range(1, len(lines_out) + 1),
                         "LEN_M": [round(g.length, 2) for g in lines],
                         "SRC": ["DOM"] * len(lines_out)},
                        geometry=lines_out, crs=target_crs
                    ).to_file(str(shp_dir / "lanes.shp"), encoding="utf-8")
                    shp_files.append(str(shp_dir / "lanes.shp"))
                if polys_out:
                    gpd.GeoDataFrame(
                        {"SID": range(1, len(polys_out) + 1),
                         "AREA_M2": [round(g.area, 2) for g in polys],
                         "SRC": ["DOM"] * len(polys_out)},
                        geometry=polys_out, crs=target_crs
                    ).to_file(str(shp_dir / "lane_areas.shp"), encoding="utf-8")
                    shp_files.append(str(shp_dir / "lane_areas.shp"))
            except Exception as e:
                wlog(f"SHP 写出失败：{e}")
        shp_files = [p for p in shp_files if Path(p).is_file()]

        # 预览图：工作系几何需反变换回 DOM 源 CRS 才能叠加到影像上。
        preview = str(out / "提取预览.png")
        try:
            self.render_preview(dom, win, lines, polys, preview,
                                work_to_src=plan.to_source())
            wlog("预览图已生成：提取预览.png")
        except Exception as e:
            wlog(f"预览图生成失败：{e}")
            preview = ""

        report = {
            "tif": str(tif_path),
            "crs": coords.crs_label(src_crs),          # 兼容旧字段，等于源 CRS
            **plan.report(),
            "stock_crs": (coords.crs_label(stock_info.primary)
                          if stock_info is not None else ""),
            "window_px": [int(win.width), int(win.height)],
            "res_m": round(res_m, 4),
            "lines": len(lines),
            "areas": len(polys),
            "gdb": str(gdb_path) if gdb_written else "",
            "layers": layers_written,
            "shp": shp_files,
            "preview": preview,
            "seconds": round(time.time() - t0, 1),
        }
        (out / "正射提取报告.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        wlog(f"完成：线 {len(lines)} / 面 {len(polys)}，耗时 {report['seconds']}s")
        if progress_cb:
            progress_cb(total + N_TAIL, total + N_TAIL, "完成")
        return report

    # ---------- 工具 ----------
    @staticmethod
    def _transform_geoms(geoms, tf):
        if tf is None or not geoms:
            return geoms
        from shapely.ops import transform as shp_transform
        return [shp_transform(tf.transform, g) for g in geoms]

    @staticmethod
    def _tile_res_m(dom, sub_t, sub, to_working) -> float:
        """tile 中心处 1 像素在 working CRS 下的米数（x/y 平均）。"""
        cx = sub.width / 2.0
        cy = sub.height / 2.0
        x0, y0 = sub_t * (cx, cy)
        x1, y1 = sub_t * (cx + 1.0, cy)
        x2, y2 = sub_t * (cx, cy + 1.0)
        if to_working is not None:
            x0, y0 = to_working.transform(x0, y0)
            x1, y1 = to_working.transform(x1, y1)
            x2, y2 = to_working.transform(x2, y2)
        return (math.hypot(x1 - x0, y1 - y0)
                + math.hypot(x2 - x0, y2 - y0)) / 2.0

    @classmethod
    def _center_res_m(cls, dom, to_working, win) -> float:
        cx = win.col_off + win.width / 2.0
        cy = win.row_off + win.height / 2.0
        x0, y0 = dom.transform * (cx, cy)
        x1, y1 = dom.transform * (cx + 1.0, cy)
        x2, y2 = dom.transform * (cx, cy + 1.0)
        if to_working is not None:
            x0, y0 = to_working.transform(x0, y0)
            x1, y1 = to_working.transform(x1, y1)
            x2, y2 = to_working.transform(x2, y2)
        return (math.hypot(x1 - x0, y1 - y0)
                + math.hypot(x2 - x0, y2 - y0)) / 2.0

    @staticmethod
    def _stock_bounds(stock_gdb, stock_info, working_crs):
        """各层按真实 CRS 转 working 后的合并范围。"""
        import geopandas as gpd
        bs = []
        for name in stock_info.per_layer:
            try:
                g = gpd.read_file(stock_gdb, layer=name)
            except Exception:
                continue
            if g.empty:
                continue
            if g.crs is None:
                g = g.set_crs(stock_info.per_layer[name], allow_override=True)
            g = g.to_crs(working_crs)
            bs.append(g.total_bounds)
        if not bs:
            raise RuntimeError("存量 GDB 无有效图层")
        arr = np.array(bs)
        return (float(arr[:, 0].min()), float(arr[:, 1].min()),
                float(arr[:, 2].max()), float(arr[:, 3].max()))

    def merge_collinear(self, lines):
        """合并端点相邻的共线线段。
        原实现 5 轮 × O(n²) 全量扫描，线数上万时会卡住数分钟；
        改为端点网格索引：每次只查当前端点附近一格内的候选，复杂度近似 O(n)。"""
        from shapely.geometry import LineString
        lines = [g for g in lines if g.length >= self.min_len]
        snap = self.merge_snap
        if not lines or snap <= 0:
            return lines
        cell = snap
        for _ in range(5):
            merged_any = False
            used = [False] * len(lines)
            out = []
            # 端点网格索引：格键 → 线号
            grid = {}
            for j, h in enumerate(lines):
                for pt in (h.coords[0], h.coords[-1]):
                    grid.setdefault((int(pt[0] // cell), int(pt[1] // cell)), []).append(j)

            def near(pt):
                kx, ky = int(pt[0] // cell), int(pt[1] // cell)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for j in grid.get((kx + dx, ky + dy), ()):
                            yield j

            for i in range(len(lines)):
                if used[i]:
                    continue
                g = lines[i]
                used[i] = True
                changed = True
                while changed:
                    changed = False
                    ge = (g.coords[0], g.coords[-1])
                    seen = set()
                    stop = False
                    for pt in ge:
                        for j in near(pt):
                            if j == i or used[j] or j in seen:
                                continue
                            seen.add(j)
                            h = lines[j]
                            he = (h.coords[0], h.coords[-1])
                            loc = None
                            for a, pe in ((0, ge[0]), (1, ge[1])):
                                for b, qe in ((0, he[0]), (1, he[1])):
                                    dist = ((pe[0] - qe[0]) ** 2 + (pe[1] - qe[1]) ** 2) ** 0.5
                                    if dist < snap and (loc is None or dist < loc[0]):
                                        loc = (dist, a, b)
                            if loc is None:
                                continue
                            gpts, hpts = list(g.coords), list(h.coords)
                            a, b = loc[1], loc[2]
                            if a == 1 and b == 0:
                                pts = gpts + hpts
                            elif a == 0 and b == 1:
                                pts = hpts + gpts
                            elif a == 1 and b == 1:
                                pts = gpts + hpts[::-1]
                            else:   # a == 0 and b == 0：两条线同向，h 需反向
                                pts = hpts[::-1] + gpts
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
                            stop = True   # g 已变化，重新按新端点收集候选
                            break
                        if stop:
                            break
                out.append(g)
            lines = out
            if not merged_any:
                break
        return lines

    def render_preview(self, dom, win, lines, polys, out_png, work_to_src=None):
        """工作系几何 → DOM 源 CRS → 像素，叠加到底图上。"""
        from rasterio.enums import Resampling
        OW = 1600
        scale = win.width / OW
        OH = max(1, int(win.height / scale))
        wt = dom.window_transform(win)
        img = read_rgb(dom, window=win, out_shape=(3, OH, OW),
                       resampling=Resampling.bilinear).transpose(1, 2, 0)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        inv = ~wt

        def w2p(x, y):
            if work_to_src is not None:
                x, y = work_to_src.transform(x, y)
            c, r = inv * (x, y)
            return c / scale, r / scale

        for p in polys:
            ext = np.array([w2p(*c) for c in p.exterior.coords], np.int32)
            cv2.polylines(img, [ext], True, (0, 220, 255), 2, cv2.LINE_AA)
        for g in lines:
            pts = np.array([w2p(*c) for c in g.coords], np.int32)
            cv2.polylines(img, [pts], False, (0, 255, 0), 1, cv2.LINE_AA)
        from .imgio import imwrite_unicode
        if not imwrite_unicode(out_png, img):
            raise RuntimeError("PNG 编码失败")
        return out_png
