"""变化比对：自动提取成果 vs 存量地图 GDB → 新增 / 缺失 / 改动 三分类变化图层。

匹配规则（按几何类型）：
- 线（标线）：缓冲区重叠（1.5m）+ 方向夹角 <25°；匹配后中点偏移 >2m 或长度比
  超出 [0.5, 2.0] 判为"改动"
- 面（面状标线）：IoU ≥ 0.5 匹配；相交但 IoU < 0.5 判为"改动"
- 点（标志/设施）：3m 内最近邻一对一贪心匹配

坐标系：新成果与存量逐层读取真实 CRS，统一转到米制工作坐标系后匹配；
输出默认跟随新成果（要素最多的图层），可用 output_crs 覆盖。DOM 仅作预览底图，
其 CRS 缺失且未指定 dom_crs 时跳过预览，不影响比对结果。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from . import coords
from .crs_plan import make_plan
from .exporter import clean_vector_outputs, clear_file_dataset

GEOM_KIND = {"LineString": "lines", "MultiLineString": "lines",
             "Polygon": "areas", "MultiPolygon": "areas",
             "Point": "points", "MultiPoint": "points"}

CN = {"new": "新增", "missing": "缺失", "modified": "改动"}


class Comparator:
    def __init__(self, line_buf=1.5, point_dist=3.0, iou_match=0.5):
        self.line_buf = line_buf
        self.point_dist = point_dist
        self.iou_match = iou_match

    # ---------- 读取 ----------
    def _load_layers(self, path, override=None, *, field="input_crs",
                     infer_roots=(), anchor_crs=None, anchor_bounds=None):
        """返回 ({kind: [(layer, GeoSeries)]}, GdbCRS)。

        图层 CRS 逐层从文件读取；缺失层用 override 修复，仍缺失则按邻近
        矢量与底图范围自动推断，证据不足才抛错。
        GeoSeries 保留真实 CRS（尚未转工作系）。
        """
        import geopandas as gpd
        import pyogrio
        import shapely
        info = coords.inspect_gdb_crs(
            path, override, field=field, infer_roots=infer_roots,
            anchor_crs=anchor_crs, anchor_bounds=anchor_bounds)
        out = {"lines": [], "areas": [], "points": []}  # [(layer_name, geoseries)]
        for name, _geom_type in pyogrio.list_layers(path):
            if name.startswith("~") or name.endswith(".xlsx"):
                continue
            layer_crs = info.per_layer.get(name)
            if layer_crs is None:
                continue
            try:
                g = gpd.read_file(path, layer=name)
            except Exception:
                continue
            if g.empty:
                continue
            # 炸开多部件，只保留基础几何类型
            parts = [p for gg in g.geometry for p in shapely.get_parts([gg])
                     if p.geom_type in ("LineString", "Polygon", "Point")]
            if not parts:
                continue
            kind = GEOM_KIND.get(parts[0].geom_type)
            if kind:
                out[kind].append((name, gpd.GeoSeries(parts, crs=layer_crs)))
        return out, info

    @staticmethod
    def _layers_to_working(layers, working_crs):
        """逐层转到工作 CRS；失败时给出图层名与原因。"""
        conv = {"lines": [], "areas": [], "points": []}
        for kind, items in layers.items():
            for name, gs in items:
                try:
                    conv[kind].append((name, gs.to_crs(working_crs)))
                except Exception as e:
                    raise coords.CRSTransformError(
                        f"图层「{name}」无法转换到工作坐标系"
                        f"（{coords.describe(gs.crs)} → {coords.describe(working_crs)}）：{e}"
                    ) from e
        return conv

    # ---------- 匹配 ----------
    def _match_lines(self, new_g, ref_g):
        """以存量为中心的覆盖式匹配：
        - 存量线被新线（可多段）覆盖 ≥50% → 匹配
        - 覆盖不足但附近 3m 内有新线 → 改动
        - 其余 → 缺失；未被任何存量使用的新线 → 新增

        覆盖长度必须用「线 ∩ 缓冲面」的长度，不能对缓冲面取 .length
        （多边形的 .length 是周长，会把 40m 新线对 100m 存量线算成 82%）。
        """
        from shapely.strtree import STRtree
        from shapely.ops import unary_union
        tree = STRtree(list(ref_g.geometry))
        ref_hits = [[] for _ in range(len(ref_g))]   # j -> [(score, i)]
        for i, g in enumerate(new_g.geometry):
            for cj in tree.query(g.buffer(self.line_buf)):
                j = int(cj)
                h = ref_g.geometry.iloc[j]
                # h 被 g 的缓冲带覆盖的长度（不是缓冲面周长）
                inter = h.intersection(g.buffer(self.line_buf / 2)).length
                denom = min(g.length, h.length)
                if denom <= 0:
                    continue
                score = inter / denom
                if score < 0.3:
                    continue
                # 局部方向必须大致平行，排除垂直交叉的道路
                mid = g.interpolate(0.5, normalized=True)
                d1 = h.project(mid)
                pa = h.interpolate(max(0.0, d1 - 2.0))
                pb = h.interpolate(min(h.length, d1 + 2.0))
                v1 = (np.array(mid.coords[0][:2])
                      - np.array(g.interpolate(0.2, normalized=True).coords[0][:2]))
                v2 = (np.array(pb.coords[0][:2]) - np.array(pa.coords[0][:2]))
                n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if n1 > 1e-6 and n2 > 1e-6 and abs(np.dot(v1, v2)) / (n1 * n2) < 0.5:
                    continue
                ref_hits[j].append((score, i))

        pairs, modified, missing = [], [], []
        used_new: set[int] = set()
        for j, hits in enumerate(ref_hits):
            h = ref_g.geometry.iloc[j]
            if not hits:
                missing.append(j)
                continue
            union_g = unary_union([new_g.geometry.iloc[i] for _, i in hits])
            covered = (h.intersection(union_g.buffer(0.3)).length
                       / max(h.length, 1e-6))
            if covered >= 0.5:
                best = max(hits)
                pairs.append((best[1], j, round(covered, 2)))
                used_new.update(i for _, i in hits)
            else:
                # 低覆盖：找附近未被使用的新线 → 改动
                near = None
                for i, g in enumerate(new_g.geometry):
                    if i in used_new:
                        continue
                    d = g.interpolate(0.5, normalized=True).distance(h)
                    if d < 3.0 and (near is None or d < near[0]):
                        near = (d, i)
                if near is not None:
                    used_new.add(near[1])
                    why = (f"偏移{near[0]:.1f}m" if near[0] >= 0.5
                           else "覆盖不足")
                    modified.append((near[1], j, why))
                else:
                    missing.append(j)
        new_only = [i for i in range(len(new_g)) if i not in used_new]
        return pairs, new_only, missing, modified

    def _match_areas(self, new_g, ref_g):
        """面匹配：以新面被存量面覆盖的比例为主（斑马线整体 vs 单条条纹）。"""
        from shapely.strtree import STRtree
        tree = STRtree(list(ref_g.geometry))
        pairs, new_only, modified = [], [], []
        used_ref = set()
        for i, g in enumerate(new_g.geometry):
            cand = tree.query(g)
            best = None
            for cj in cand:
                j = int(cj)
                if j in used_ref:
                    continue
                h = ref_g.geometry.iloc[j]
                cover = g.intersection(h).area / max(g.area, 1e-6)
                if cover < 0.3:
                    continue
                iou = g.intersection(h).area / max(g.union(h).area, 1e-6)
                score = max(cover, iou)
                if best is None or score > best[0]:
                    best = (score, j, iou)
            if best is None:
                new_only.append(i)
                continue
            score, j, iou = best
            used_ref.add(j)
            if iou >= self.iou_match or score >= 0.8:
                pairs.append((i, j, round(score, 3)))
            else:
                modified.append((i, j, f"IoU={iou:.2f}"))
        ref_only = [j for j in range(len(ref_g)) if j not in used_ref]
        return pairs, new_only, ref_only, modified

    def _match_points(self, new_g, ref_g):
        pairs, new_only = [], []
        used_ref = set()
        ref_pts = np.array([(p.x, p.y) for p in ref_g.geometry])
        for i, g in enumerate(new_g.geometry):
            d = np.hypot(ref_pts[:, 0] - g.x, ref_pts[:, 1] - g.y)
            order = np.argsort(d)
            chosen = None
            for j in order:
                if int(j) in used_ref:
                    continue
                if d[j] > self.point_dist:
                    break
                chosen = int(j)
                break
            if chosen is None:
                new_only.append(i)
            else:
                used_ref.add(chosen)
                pairs.append((i, chosen, round(float(d[chosen]), 2)))
        ref_only = [j for j in range(len(ref_g)) if j not in used_ref]
        return pairs, new_only, ref_only, []

    MATCHERS = {"lines": _match_lines, "areas": _match_areas, "points": _match_points}

    # ---------- 主流程 ----------
    def run(self, new_gdb: str, stock_gdb: str, out_dir: str,
            dom_tif: str | None = None, wlog=print, progress_cb=None,
            new_crs=None, stock_crs=None, dom_crs=None, output_crs=None):
        import geopandas as gpd
        import pandas as pd

        t0 = time.time()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # 自动推断缺失 CRS 的依据：同项目目录下的邻近矢量，以及比对底图。
        # 底图缺 CRS 只影响预览/推断，不阻断比对。
        infer_roots = coords.inference_roots(new_gdb, stock_gdb, dom_tif)
        anchor_crs = anchor_bounds = None
        if dom_tif and Path(dom_tif).exists():
            try:
                import rasterio
                with rasterio.open(dom_tif) as ds:
                    anchor_crs, _origin, _note = coords.resolve_raster_crs(
                        ds, dom_crs, field="dom_crs")
                    anchor_bounds = ds.bounds
            except coords.CRSMissingError as e:
                wlog(f"警告：比对底图缺少坐标系，跳过底图与自动推断：{e}")

        wlog(f"读取新成果：{new_gdb}")
        new_layers, new_info = self._load_layers(
            new_gdb, new_crs, field="new_crs", infer_roots=infer_roots,
            anchor_crs=anchor_crs, anchor_bounds=anchor_bounds)
        wlog(f"读取存量：{stock_gdb}")
        stock_layers, stock_info = self._load_layers(
            stock_gdb, stock_crs, field="stock_crs", infer_roots=infer_roots,
            anchor_crs=anchor_crs, anchor_bounds=anchor_bounds)

        source_crs = new_info.primary
        target_override = coords.parse_crs(output_crs, field="output_crs")
        target_crs = target_override if target_override is not None else source_crs

        # 匹配范围中心（在目标 CRS 下取所有图层范围的并集，再转经纬度）
        bounds_list = []
        for layers in (new_layers, stock_layers):
            for kind in ("lines", "areas", "points"):
                for _name, gs in layers[kind]:
                    if len(gs) == 0:
                        continue
                    bounds_list.append(coords.transform_bounds(
                        gs.total_bounds, gs.crs, target_crs))
        if not bounds_list:
            raise RuntimeError("新成果与存量均无有效要素，无法比对")
        ub = (min(b[0] for b in bounds_list), min(b[1] for b in bounds_list),
              max(b[2] for b in bounds_list), max(b[3] for b in bounds_list))
        lon_c, lat_c = coords.geographic_center(target_crs, ub)

        plan = make_plan(
            source_crs, target_override,
            center_lon=lon_c, center_lat=lat_c,
            source_origin=new_info.origin,
            target_origin="user" if target_override is not None else "default:new")

        def _origin_note(info):
            if info.origin == "mixed":
                return "（混合 CRS，逐层转换）"
            if info.origin == "inferred":
                return f"（自动判断：{info.note}）"
            return ""

        wlog(f"新成果坐标系：{coords.crs_label(source_crs)}{_origin_note(new_info)}；"
             f"存量坐标系：{coords.crs_label(stock_info.primary)}"
             + _origin_note(stock_info))
        wlog(f"工作坐标系：{coords.crs_label(plan.working_crs)}（{plan.working_reason}）；"
             f"输出坐标系：{coords.crs_label(plan.target_crs)}")
        if new_info.origin == "mixed":
            wlog("警告：新成果图层存在多种 CRS，已逐层转换；输出默认跟随要素最多的图层"
                 f"（{coords.crs_label(source_crs)}），可用输出坐标系覆盖。")

        new_layers = self._layers_to_working(new_layers, plan.working_crs)
        stock_layers = self._layers_to_working(stock_layers, plan.working_crs)

        results = {}
        warnings: list[str] = []
        changes = {"new": [], "missing": [], "modified": []}  # (kind, layer, geom, note)
        n_kind = len([k for k in ("lines", "areas", "points")
                      if stock_layers[k] or new_layers[k]])
        done_kind = 0

        for kind in ("lines", "areas", "points"):
            new_all, ref_all = [], []
            new_src, ref_src = [], []
            for name, gs in new_layers[kind]:
                new_all += list(gs.geometry)
                new_src += [name] * len(gs)
            for name, gs in stock_layers[kind]:
                ref_all += list(gs.geometry)
                ref_src += [name] * len(gs)
            if not new_all and not ref_all:
                continue
            done_kind += 1
            new_g = gpd.GeoSeries(new_all, crs=plan.working_crs) if new_all else None
            ref_g = gpd.GeoSeries(ref_all, crs=plan.working_crs) if ref_all else None
            if progress_cb:
                progress_cb(done_kind, n_kind, f"比对{kind}：新{len(new_all)} 存量{len(ref_all)}")
            wlog(f"{kind}：新成果 {len(new_all)}，存量 {len(ref_all)}")

            if new_g is None:
                # 新成果完全没有该类要素 → 无法判定，跳过（不算缺失）。
                # 必须在日志/报告中明确说明，否则会被误读为“没有缺失”。
                msg = (f"新成果未包含{kind}要素：存量 {len(ref_all)} 个"
                       f"要素未计入缺失")
                wlog("  注意：" + msg)
                warnings.append(msg)
                results[kind] = {"new": 0, "missing": 0, "modified": 0,
                                 "matched": 0, "skipped": True,
                                 "stock_count": len(ref_all)}
                continue
            if ref_g is None:
                for i, g in enumerate(new_all):
                    changes["new"].append((kind, new_src[i], g, "新成果独有"))
                results[kind] = {"new": len(new_all), "missing": 0, "modified": 0, "matched": 0}
                continue

            matcher = self.MATCHERS[kind].__get__(self)
            pairs, new_only, ref_only, modified = matcher(new_g, ref_g)
            for i in new_only:
                changes["new"].append((kind, new_src[i], new_g.iloc[i], "新成果独有"))
            for j in ref_only:
                changes["missing"].append((kind, ref_src[j], ref_g.iloc[j], "存量独有"))
            for i, j, why in modified:
                note = f"{CN['modified']}（{why}）｜存量:{ref_src[j]} 新:{new_src[i]}"
                changes["modified"].append((kind, new_src[i], new_g.iloc[i], note))
                changes["modified"].append((kind, ref_src[j], ref_g.iloc[j], note))
            results[kind] = {"new": len(new_only), "missing": len(ref_only),
                             "modified": len(modified), "matched": len(pairs)}
            wlog(f"  匹配 {len(pairs)}，新增 {len(new_only)}，缺失 {len(ref_only)}，改动 {len(modified)}")

        # ---------- 写变化图层（工作系 → 目标 CRS） ----------
        gdb_path = out / "变化比对.gdb"
        layer_rows = {}
        for ctype in ("new", "missing", "modified"):
            for kind, src, geom, note in changes[ctype]:
                key = f"chg_{ctype}_{kind}"
                layer_rows.setdefault(key, {"geometry": [], "SRC": [], "NOTE": []})
                layer_rows[key]["geometry"].append(geom)
                layer_rows[key]["SRC"].append(src)
                layer_rows[key]["NOTE"].append(note)
        written = []

        def _out_gdf(rows):
            return gpd.GeoDataFrame(pd.DataFrame(rows), geometry="geometry",
                                    crs=plan.working_crs).to_crs(plan.target_crs)

        # 复用输出目录时先删除旧 GDB，避免上次的变化图层残留；
        # 本次没有变化时也要删除，不能把旧成果当成本次结果。
        gdb_error = clear_file_dataset(gdb_path)
        if gdb_error:
            warnings.append(gdb_error)
            wlog(gdb_error)
        elif layer_rows:
            try:
                for key, rows in layer_rows.items():
                    _out_gdf(rows).to_file(str(gdb_path), layer=key,
                                           driver="OpenFileGDB")
                    written.append(key)
                wlog(f"变化图层已写入：{gdb_path.name}（{len(written)} 层）")
            except Exception as e:
                msg = f"变化 GDB 写出失败：{e}"
                warnings.append(msg)
                wlog(msg)
                # 不保留只有部分图层的成果，避免误读为完整结果
                if gdb_path.exists():
                    clear_file_dataset(gdb_path)
                written.clear()
        change_gdb = str(gdb_path) if written and gdb_path.exists() else ""

        # SHP 同步（与 GDB 使用同一目标 CRS）
        shp_dir = out / "shp"
        shp_files = []
        if shp_dir.is_dir():
            for msg in clean_vector_outputs(shp_dir, prefix="chg_"):
                warnings.append(msg)
                wlog(msg)
        try:
            shp_dir.mkdir(parents=True, exist_ok=True)
            for key, rows in layer_rows.items():
                p = str(shp_dir / f"{key}.shp")
                _out_gdf(rows).to_file(p, encoding="utf-8")
                shp_files.append(p)
        except Exception as e:
            msg = f"变化 SHP 写出失败：{e}"
            warnings.append(msg)
            wlog(msg)
        shp_files = [p for p in shp_files if Path(p).is_file()]

        # 预览渲染（工作系几何；DOM 缺 CRS 时跳过，不阻断比对）
        preview = str(out / "比对预览.png")
        try:
            self.render(new_layers, stock_layers, changes,
                        dom_tif if dom_tif and Path(dom_tif).exists() else None,
                        preview, plan=plan, dom_crs=dom_crs, wlog=wlog)
            wlog("比对预览图已生成：比对预览.png")
        except coords.CRSMissingError as e:
            wlog(f"比对预览已跳过：{e}")
            preview = ""
        except Exception as e:
            wlog(f"预览图生成失败：{e}")
            preview = ""

        stats = {
            "new_gdb": str(new_gdb), "stock_gdb": str(stock_gdb),
            "crs": coords.crs_label(plan.source_crs),      # 兼容别名：主输入 CRS
            **plan.report(),
            "new_origin": new_info.origin,
            "stock_origin": stock_info.origin,
            "stock_crs_note": stock_info.note,
            "new_layers_crs": {k: coords.crs_label(v)
                               for k, v in new_info.per_layer.items()},
            "stock_layers_crs": {k: coords.crs_label(v)
                                 for k, v in stock_info.per_layer.items()},
            "by_kind": results,
            "warnings": warnings,
            "totals": {k: sum(v[k] for v in results.values()) for k in ("new", "missing", "modified", "matched")},
            "change_gdb": change_gdb, "change_layers": written,
            "shp": shp_files, "preview": preview,
            "seconds": round(time.time() - t0, 1),
        }
        (out / "比对报告.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        wlog(f"比对完成：新增 {stats['totals']['new']}，缺失 {stats['totals']['missing']}，"
             f"改动 {stats['totals']['modified']}，匹配 {stats['totals']['matched']}，"
             f"耗时 {stats['seconds']}s")
        return stats

    # ---------- 渲染 ----------
    def render(self, new_layers, stock_layers, changes, dom_tif, out_png, *,
               plan, dom_crs=None, wlog=print):
        """在 working 米制下计算范围；有 DOM 时反变换到影像 CRS 叠加。"""
        import cv2
        import geopandas as gpd
        import rasterio
        import shapely
        from rasterio.enums import Resampling
        from rasterio.windows import Window, from_bounds
        from .imgio import read_rgb

        working_crs = plan.working_crs
        geoms_all = []
        for ctype in ("new", "missing", "modified"):
            for kind, src, geom, note in changes[ctype]:
                geoms_all.append(geom)
        if not geoms_all:
            raise RuntimeError("无变化要素可渲染")
        gs = gpd.GeoSeries(geoms_all, crs=working_crs)
        b = gs.total_bounds
        pad = 40.0
        minx, miny, maxx, maxy = b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad

        to_src = None
        if dom_tif:
            dom = rasterio.open(dom_tif)
            src_crs, src_origin, src_note = coords.resolve_raster_crs(
                dom, dom_crs, field="dom_crs")
            if src_origin != "file" or src_note:
                wlog(f"比对底图坐标系：{coords.crs_label(src_crs)}"
                     + (f"（{src_note}）" if src_note else ""))
            to_src = plan.to_via_target(src_crs)
            sb = coords.transform_bounds_with((minx, miny, maxx, maxy), to_src)
            win = from_bounds(*sb, dom.transform).round_offsets().round_lengths()
            # 变化点靠近影像边缘时，外扩后的窗口会越界；先裁剪到影像范围
            col0 = max(0, int(round(win.col_off)))
            row0 = max(0, int(round(win.row_off)))
            col1 = min(int(dom.width), int(round(win.col_off + win.width)))
            row1 = min(int(dom.height), int(round(win.row_off + win.height)))
            if col1 - col0 < 1 or row1 - row0 < 1:
                raise RuntimeError(
                    "变化要素不在正射影像覆盖范围内，无法生成比对预览")
            win = Window(col0, row0, col1 - col0, row1 - row0)
            OW = 1800
            scale = win.width / OW
            OH = max(1, int(win.height / scale))
            wt = dom.window_transform(win)
            img = read_rgb(dom, window=win, out_shape=(3, OH, OW),
                           resampling=Resampling.bilinear).transpose(1, 2, 0)
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            inv = ~wt

            def w2p(x, y):
                if to_src is not None:
                    x, y = to_src.transform(x, y)
                c, r = inv * (x, y)
                return c / scale, r / scale
        else:
            W, H = 1600, max(1, int(1600 * (maxy - miny) / max(maxx - minx, 1e-6)))
            img = np.full((H, W, 3), 245, np.uint8)

            def w2p(x, y):
                return (x - minx) / (maxx - minx) * W, (maxy - y) / (maxy - miny) * H

        def draw(geom, color, thk):
            for part in shapely.get_parts([geom]):
                if part.geom_type in ("LineString", "LinearRing"):
                    pts = np.array([w2p(*c[:2]) for c in part.coords], np.int32)
                    cv2.polylines(img, [pts], False, color, thk, cv2.LINE_AA)
                elif part.geom_type == "Polygon":
                    ext = np.array([w2p(*c[:2]) for c in part.exterior.coords], np.int32)
                    cv2.polylines(img, [ext], True, color, thk, cv2.LINE_AA)
                elif part.geom_type == "Point":
                    px, py = w2p(part.x, part.y)
                    cv2.circle(img, (int(px), int(py)), 5, color, -1, cv2.LINE_AA)

        for kind in ("lines", "areas", "points"):
            for name, gs2 in stock_layers[kind]:
                for g in gs2.geometry:
                    draw(g, (160, 160, 160), 1)   # 存量 灰
        for kind, src, geom, note in changes["new"]:
            draw(geom, (0, 200, 0), 2)            # 新增 绿
        for kind, src, geom, note in changes["missing"]:
            draw(geom, (0, 0, 255), 2)            # 缺失 红
        for kind, src, geom, note in changes["modified"]:
            draw(geom, (0, 140, 255), 2)          # 改动 橙
        from .imgio import imwrite_unicode
        if not imwrite_unicode(out_png, img):
            raise RuntimeError("PNG 编码失败")
        return out_png
