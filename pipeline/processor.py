"""Pipeline 编排：加载→(鱼眼)→缩放→检测→车道线→地理参考→预览→导出→时序变化。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import CLASS_CN
from .change_detector import detect_changes
from .data_loader import DataProject
from .exporter import (export_lines_gdb, export_lines_shp, export_points_gdb,
                      export_points_shp)
from .georeference import GeoReferencer
from .lane_detector import LaneDetector
# 注意：必须静态导入。此前用 __import__("pipeline.undistort") 动态导入，
# PyInstaller 静态分析看不到，导致打包后运行报 No module named 'pipeline.undistort'。
from .undistort import FisheyeCorrector


@dataclass
class PipelineResult:
    output_dir: str
    per_session: dict = field(default_factory=dict)   # session -> {det, lane, shp, gdb}
    change_count: int = 0
    change_path: str = ""
    errors: list = field(default_factory=list)


class Pipeline:
    def __init__(self, config, detector=None):
        self.cfg = config
        self.detector = detector
        self.lane = LaneDetector()
        self.geo = GeoReferencer(config)
        self._corrector = None
        self._corr_dims = None
        self.errors = []

    def _get_corrector(self, w, h, scale):
        import cv2
        if self._corrector and self._corr_dims == (w, h, scale):
            return self._corrector
        f = self.cfg.fisheye
        corr = FisheyeCorrector(
            f["fx"] * scale, f["fy"] * scale, f["cx"] * scale, f["cy"] * scale,
            f["k1"], f["k2"], int(w * scale), int(h * scale))
        self._corrector = corr
        self._corr_dims = (w, h, scale)
        return corr

    def _load_image(self, path: Path, scale: float, use_fisheye: bool):
        import cv2
        from PIL import Image, ImageOps
        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        arr = np.array(im.convert("RGB"))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        work = cv2.resize(bgr, (int(w * scale), int(h * scale)))
        if use_fisheye:
            work = self._get_corrector(w, h, scale).correct(work)
        return work, scale

    def _draw(self, img, dets, lanes):
        import cv2  # 延迟导入
        for d in dets:
            x1, y1, x2, y2 = map(int, d.bbox)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 255), 2)
            cv2.putText(img, f"{d.class_name}:{d.score:.2f}", (x1, max(0, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 200, 255), 1)
        for ln in lanes:
            for x, y in ln.points_px:
                cv2.circle(img, (int(x), int(y)), 2, (0, 255, 0), -1)

    def process_project(self, data_dir, output_dir, max_images=None, progress_cb=None,
                        export_gdb=True, use_fisheye=None, enable_change=None):
        import cv2  # 延迟导入（processor 始终在含 cv2 环境运行）
        cfg = self.cfg
        use_fisheye = cfg.use_fisheye if use_fisheye is None else use_fisheye
        enable_change = cfg.enable_change_detection if enable_change is None else enable_change
        out = Path(output_dir)
        (out / "preview").mkdir(parents=True, exist_ok=True)
        proj = DataProject.load(data_dir)

        matched = proj.matched
        if max_images:
            matched = matched[:max_images]
        total = len(matched)

        # 每期次采集
        session_points: dict[str, list[dict]] = {}
        session_lanes: dict[str, list[dict]] = {}
        per_session = {}

        for i, (im_rec, pose) in enumerate(matched):
            try:
                og_w, og_h = 7360, 4912
                scale = cfg.resize_max_dim / max(og_w, og_h)
                work, _ = self._load_image(im_rec.path, scale, use_fisheye)
                dets = self.detector.detect(work) if self.detector else []
                lanes = self.lane.detect(work)

                pts_feat = []
                for d in dets:
                    x1, y1, x2, y2 = d.bbox
                    # 还原到原始分辨率坐标再地理参考
                    gp = self.geo.pixel_to_ground(x1 / scale, y2 / scale, pose)
                    if gp.valid:
                        pts_feat.append({
                            "class_id": d.class_id, "class_cn": CLASS_CN.get(d.class_id, d.class_name),
                            "session": im_rec.session, "image": im_rec.filename,
                            "x": gp.x, "y": gp.y, "dist": gp.dist, "score": d.score,
                        })
                lane_feat = []
                for ln in lanes:
                    coords = [(px / scale, py / scale) for px, py in ln.points_px]
                    gcoords = []
                    ok = True
                    for px, py in coords:
                        g = self.geo.pixel_to_ground(px, py, pose)
                        if not g.valid:
                            ok = False
                            break
                        gcoords.append((g.x, g.y))
                    if ok and gcoords:
                        dists = [self.geo.pixel_to_ground(px, py, pose).dist for px, py in coords]
                        lane_feat.append({
                            "session": im_rec.session, "image": im_rec.filename, "side": ln.side,
                            "points": gcoords, "avg_dist": float(np.mean(dists)),
                        })

                session_points.setdefault(im_rec.session, []).extend(pts_feat)
                session_lanes.setdefault(im_rec.session, []).extend(lane_feat)

                # 预览
                prev = work.copy()
                self._draw(prev, dets, lanes)
                cv2.imwrite(str(out / "preview" / f"{im_rec.session}__{im_rec.filename}"), prev)

                if progress_cb:
                    progress_cb(i + 1, total, im_rec.filename, len(pts_feat), len(lane_feat), True)
            except Exception as e:
                self.errors.append(f"{im_rec.filename}: {e}")
                if progress_cb:
                    progress_cb(i + 1, total, im_rec.filename, 0, 0, False)

        # 导出每期次
        for sname, pts in session_points.items():
            lanes = session_lanes.get(sname, [])
            sp = out / f"result_{sname}.shp"
            sl = out / f"result_lanes_{sname}.shp"
            export_points_shp(sp, pts)
            export_lines_shp(sl, lanes)
            gdb_ok = False
            if export_gdb:
                gdb = out / f"result_{sname}.gdb"
                gdb_ok = (export_points_gdb(gdb, "detections", pts) and
                          export_lines_gdb(gdb, "lanes", lanes))
                if not gdb_ok:
                    from .exporter import WARNINGS as _gdb_warn
                    while _gdb_warn:
                        self.errors.append(_gdb_warn.pop(0))
            per_session[sname] = {"det": len(pts), "lane": len(lanes),
                                   "shp": str(sp), "gdb": str(gdb) if (export_gdb and gdb_ok) else ""}

        # 时序变化（v0.1 必做）
        change_count = 0
        change_path = ""
        if enable_change and len(session_points) >= 2:
            changes = detect_changes(session_points)
            change_count = len(changes)
            if changes:
                cp = out / "result_change.shp"
                export_points_shp(cp, [dict(c, class_id=c.get("class_id", -1),
                                            class_cn=c.get("class_cn", ""),
                                            session=f"{c['session_a']}->{c['session_b']}",
                                            image=c.get("change_type", ""),
                                            dist=0.0, score=0.0) for c in changes])
                if export_gdb:
                    if not export_points_gdb(out / "result_change.gdb", "changes", [
                        dict(c, class_id=c.get("class_id", -1), class_cn=c.get("class_cn", ""),
                             session=f"{c['session_a']}->{c['session_b']}", image=c.get("change_type", ""),
                             dist=0.0, score=0.0) for c in changes]):
                        from .exporter import WARNINGS as _gdb_warn
                        while _gdb_warn:
                            self.errors.append(_gdb_warn.pop(0))
                change_path = str(cp)

        return PipelineResult(str(out), per_session, change_count, change_path, self.errors)
