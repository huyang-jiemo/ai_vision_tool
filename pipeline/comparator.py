"""变化比对：自动提取成果 vs 存量地图 GDB → 新增 / 灭失 / 改动 三分类变化图层。

匹配规则（按几何类型）：
- 线（标线）：缓冲区重叠（1.5m）+ 方向夹角 <25°；匹配后中点偏移 >2m 或长度比
  超出 [0.5, 2.0] 判为"改动"
- 面（面状标线）：IoU ≥ 0.5 匹配；相交但 IoU < 0.5 判为"改动"
- 点（标志/设施）：3m 内最近邻一对一贪心匹配

输出：变化 GDB（chg_new_* / chg_missing_* / chg_modified_*，按几何类型分图层）、
SHP 同步、JSON 报告、叠加渲染预览图。坐标系与存量一致（武汉本地 TM）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .config import WUHAN_CGCS2000_GK_WKT
from .dom_extractor import LOCAL_PROJ4

GEOM_KIND = {"LineString": "lines", "MultiLineString": "lines",
             "Polygon": "areas", "MultiPolygon": "areas",
             "Point": "points", "MultiPoint": "points"}

CN = {"new": "新增", "missing": "灭失", "modified": "改动"}


class Comparator:
    def __init__(self, line_buf=1.5, angle_deg=25.0, point_dist=3.0,
                 iou_match=0.5, mod_mid_off=2.0, mod_len_ratio=(0.5, 2.0)):
        self.line_buf = line_buf
        self.cos_min = float(np.cos(np.radians(angle_deg)))
        self.point_dist = point_dist
        self.iou_match = iou_match
        self.mod_mid_off = mod_mid_off
        self.mod_len_ratio = mod_len_ratio

    # ---------- 读取 ----------
    def _load_layers(self, path):
        import geopandas as gpd
        import pyogrio
        import shapely
        out = {"lines": [], "areas": [], "points": []}  # [(layer_name, geoseries)]
        for name, geom_type in pyogrio.list_layers(path):
            if name.startswith("~") or name.endswith(".xlsx"):
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
                out[kind].append((name, gpd.GeoSeries(parts, crs=g.crs)))
        return out

    # ---------- 匹配 ----------
    def _match_lines(self, new_g, ref_g):
        """以存量为中心的覆盖式匹配：
        - 存量线被新线（可多段）覆盖 ≥50% → 匹配
        - 无覆盖且附近（3m 内）存在平行新线 → 改动
        - 其余 → 灭失；未被任何存量覆盖的新线 → 新增"""
        from shapely.strtree import STRtree
        from shapely.ops import unary_union
        tree = STRtree(list(ref_g.geometry))
        ref_hits = [[] for _ in range(len(ref_g))]   # j -> [(score, i)]
        assigned = [False] * len(new_g)
        for i, g in enumerate(new_g.geometry):
            for cj in tree.query(g.buffer(self.line_buf)):
                j = int(cj)
                h = ref_g.geometry.iloc[j]
                inter = g.buffer(self.line_buf / 2).intersection(h.buffer(self.line_buf / 2)).length
                denom = min(g.length, h.length)
                if denom <= 0:
                    continue
                score = inter / denom
                if score < 0.3:
                    continue
                mid = g.interpolate(0.5, normalized=True)
                d1 = h.project(mid)
                pa = h.interpolate(max(0.0, d1 - 2.0))
                pb = h.interpolate(min(h.length, d1 + 2.0))
                v1 = np.array(mid.coords[0][:2]) - np.array(g.interpolate(0.2, normalized=True).coords[0][:2])
                v2 = np.array(pb.coords[0][:2]) - np.array(pa.coords[0][:2])
                n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if n1 > 1e-6 and n2 > 1e-6 and abs(np.dot(v1, v2)) / (n1 * n2) < 0.5:
                    continue
                ref_hits[j].append((score, i))
                assigned[i] = True

        pairs, modified, missing = [], [], []
        for j, hits in enumerate(ref_hits):
            h = ref_g.geometry.iloc[j]
            if not hits:
                missing.append(j)
                continue
            union_g = unary_union([new_g.geometry.iloc[i] for _, i in hits])
            covered = h.buffer(0.3).intersection(union_g.buffer(0.3)).length / max(h.length, 1e-6)
            if covered >= 0.5:
                best = max(hits)
                pairs.append((best[1], j, round(covered, 2)))
            else:
                # 低覆盖：找附近平行的新线 → 改动
                near = None
                for i, g in enumerate(new_g.geometry):
                    if assigned[i]:
                        continue
                    d = g.interpolate(0.5, normalized=True).distance(h)
                    if d < 3.0 and (near is None or d < near[0]):
                        near = (d, i)
                if near is not None:
                    assigned[near[1]] = True
                    modified.append((near[1], j, f"偏移{near[0]:.1f}m"))
                else:
                    missing.append(j)
        new_only = [i for i, a in enumerate(assigned) if not a]
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
        pairs, new_only, modified = [], [], []
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
            dom_tif: str | None = None, wlog=print, progress_cb=None):
        import geopandas as gpd
        import pandas as pd
        import shapely
        from shapely.geometry import box

        t0 = time.time()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        wlog(f"读取新成果：{new_gdb}")
        new_layers = self._load_layers(new_gdb)
        wlog(f"读取存量：{stock_gdb}")
        stock_layers = self._load_layers(stock_gdb)

        results = {}
        changes = {"new": [], "missing": [], "modified": []}  # (kind, layer, geom, note)
        n_kind = len([k for k in ("lines", "areas", "points") if stock_layers[k] or new_layers[k]])
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
            new_g = gpd.GeoSeries(new_all, crs=WUHAN_CGCS2000_GK_WKT) if new_all else None
            ref_g = gpd.GeoSeries(ref_all, crs=WUHAN_CGCS2000_GK_WKT) if ref_all else None
            if progress_cb:
                progress_cb(done_kind, n_kind, f"比对{kind}：新{len(new_all)} 存量{len(ref_all)}")
            wlog(f"{kind}：新成果 {len(new_all)}，存量 {len(ref_all)}")

            if new_g is None:
                # 新成果完全没有该类要素 → 无法判定，跳过（不算灭失）
                wlog(f"  新成果未包含{kind}要素，跳过该类比对")
                results[kind] = {"new": 0, "missing": 0, "modified": 0, "matched": 0,
                                 "skipped": True}
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
            wlog(f"  匹配 {len(pairs)}，新增 {len(new_only)}，灭失 {len(ref_only)}，改动 {len(modified)}")

        # ---------- 写变化图层 ----------
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
        try:
            for key, rows in layer_rows.items():
                gdf = gpd.GeoDataFrame(pd.DataFrame(rows), geometry="geometry",
                                       crs=WUHAN_CGCS2000_GK_WKT)
                gdf.to_file(str(gdb_path), layer=key, driver="OpenFileGDB")
                written.append(key)
            wlog(f"变化图层已写入：{gdb_path.name}（{len(written)} 层）")
        except Exception as e:
            wlog(f"变化 GDB 写出失败：{e}")

        # SHP 同步
        shp_dir = out / "shp"
        shp_files = []
        try:
            if shp_dir.exists():
                for old in shp_dir.glob("*"):
                    if old.suffix.lower() in (".shp", ".dbf", ".shx", ".prj", ".cpg"):
                        old.unlink()
            else:
                shp_dir.mkdir(parents=True, exist_ok=True)
            for key, rows in layer_rows.items():
                gdf = gpd.GeoDataFrame(pd.DataFrame(rows), geometry="geometry",
                                       crs=WUHAN_CGCS2000_GK_WKT)
                p = str(shp_dir / f"{key}.shp")
                gdf.to_file(p, encoding="utf-8")
                shp_files.append(p)
        except Exception as e:
            wlog(f"变化 SHP 写出失败：{e}")

        # 预览渲染
        preview = str(out / "比对预览.png")
        try:
            self.render(new_layers, stock_layers, changes,
                        dom_tif if dom_tif and Path(dom_tif).exists() else None,
                        preview)
            wlog("比对预览图已生成：比对预览.png")
        except Exception as e:
            wlog(f"预览图生成失败：{e}")
            preview = ""

        stats = {
            "new_gdb": str(new_gdb), "stock_gdb": str(stock_gdb),
            "by_kind": results,
            "totals": {k: sum(v[k] for v in results.values()) for k in ("new", "missing", "modified", "matched")},
            "change_gdb": str(gdb_path), "change_layers": written,
            "shp": shp_files, "preview": preview,
            "seconds": round(time.time() - t0, 1),
        }
        (out / "比对报告.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        wlog(f"比对完成：新增 {stats['totals']['new']}，灭失 {stats['totals']['missing']}，"
             f"改动 {stats['totals']['modified']}，匹配 {stats['totals']['matched']}，"
             f"耗时 {stats['seconds']}s")
        return stats

    # ---------- 渲染 ----------
    def render(self, new_layers, stock_layers, changes, dom_tif, out_png):
        import cv2
        import geopandas as gpd
        import rasterio
        import shapely
        from pyproj import CRS
        from rasterio.enums import Resampling
        from rasterio.windows import from_bounds
        from shapely.geometry import box

        local = CRS.from_proj4(LOCAL_PROJ4)
        geoms_all = []
        for ctype in ("new", "missing", "modified"):
            for kind, src, geom, note in changes[ctype]:
                geoms_all.append(geom)
        if not geoms_all:
            raise RuntimeError("无变化要素可渲染")
        gs = gpd.GeoSeries(geoms_all, crs=WUHAN_CGCS2000_GK_WKT)
        b = gs.total_bounds
        pad = 40
        minx, miny, maxx, maxy = b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad

        if dom_tif:
            dom = rasterio.open(dom_tif)
            src_crs = CRS.from_user_input(dom.crs)
            from pyproj import Transformer
            fwd = Transformer.from_crs(local, src_crs, always_xy=True)
            sx0, sy0 = fwd.transform(minx, miny)
            sx1, sy1 = fwd.transform(maxx, maxy)
            win = from_bounds(min(sx0, sx1), min(sy0, sy1), max(sx0, sx1), max(sy0, sy1),
                              dom.transform).round_offsets().round_lengths()
            OW = 1800
            scale = win.width / OW
            OH = max(1, int(win.height / scale))
            wt = dom.window_transform(win)
            img = dom.read([1, 2, 3], window=win, out_shape=(3, OH, OW),
                           resampling=Resampling.bilinear).transpose(1, 2, 0)
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            inv = ~wt
            back = Transformer.from_crs(local, src_crs, always_xy=True)

            def w2p(x, y):
                x, y = back.transform(x, y)
                c, r = inv * (x, y)
                return c / scale, r / scale
        else:
            W, H = 1600, int(1600 * (maxy - miny) / max(maxx - minx, 1e-6))
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
            draw(geom, (0, 0, 255), 2)            # 灭失 红
        for kind, src, geom, note in changes["modified"]:
            draw(geom, (0, 140, 255), 2)          # 改动 橙
        from .imgio import imwrite_unicode
        if not imwrite_unicode(out_png, img):
            raise RuntimeError("PNG 编码失败")
