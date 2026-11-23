"""Pipeline 编排：加载→(鱼眼)→缩放→检测→车道线→地理参考→预览→导出→时序变化。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import coords
from .config import BUSINESS_CATEGORIES, CATEGORY_CN, CLASS_CN
from .change_detector import detect_changes
from .crs_plan import make_plan
from .data_loader import DataProject, prepare_poses, validate_pose_coordinates
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
    crs: dict = field(default_factory=dict)
    report_path: str = ""


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

    def _load_image(self, path: Path, use_fisheye: bool):
        import cv2
        from PIL import Image, ImageOps
        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        arr = np.array(im.convert("RGB"))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        scale = self.cfg.resize_max_dim / max(w, h)
        work = cv2.resize(bgr, (int(w * scale), int(h * scale)))
        if use_fisheye:
            work = self._get_corrector(w, h, scale).correct(work)
        return work, scale, (w, h)

    def _draw(self, img, dets, lanes):
        import cv2  # 延迟导入
        for d in dets:
            x1, y1, x2, y2 = map(int, d.bbox)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 255), 2)
            cv2.putText(img, f"{d.class_name}:{d.score:.2f}", (x1, max(0, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 200, 255), 1)
        for ln in lanes:
            color = (0, 0, 255) if ln.side == "left" else (0, 255, 0)
            pts = np.array([(int(x), int(y)) for x, y in ln.points_px], np.int32)
            cv2.polylines(img, [pts], False, color, 2)

    @staticmethod
    def _transform_point_features(feats, tf):
        if tf is None:
            return feats
        out = []
        for f in feats:
            g = dict(f)
            g["x"], g["y"] = tf.transform(f["x"], f["y"])
            out.append(g)
        return out

    @staticmethod
    def _transform_line_features(feats, tf):
        if tf is None:
            return feats
        out = []
        for f in feats:
            g = dict(f)
            g["points"] = [tf.transform(x, y) for x, y in f["points"]]
            out.append(g)
        return out

    def process_project(self, data_dir, output_dir, max_images=None, progress_cb=None,
                        export_shp=True, export_gdb=True, use_fisheye=None,
                        enable_change=None, pose_crs=None, output_crs=None, wlog=None):
        from pyproj import CRS
        cfg = self.cfg
        log = wlog or (lambda _m: None)
        use_fisheye = cfg.use_fisheye if use_fisheye is None else use_fisheye
        enable_change = cfg.enable_change_detection if enable_change is None else enable_change
        out = Path(output_dir)
        # 输出分区：preview 标注预览 / shp 矢量（gdb 仅在需要时创建）
        (out / "preview").mkdir(parents=True, exist_ok=True)
        (out / "shp").mkdir(parents=True, exist_ok=True)
        if export_gdb:
            (out / "gdb").mkdir(parents=True, exist_ok=True)

        pose_crs_obj = coords.parse_crs(pose_crs, field="pose_crs") or CRS.from_epsg(4326)
        target_override = coords.parse_crs(output_crs, field="output_crs")
        proj = DataProject.load(data_dir)
        all_poses = proj.poses
        bounds = validate_pose_coordinates(all_poses)
        lon_c = (bounds[0] + bounds[2]) / 2.0
        lat_c = (bounds[1] + bounds[3]) / 2.0
        plan = make_plan(
            pose_crs_obj, target_override,
            center_lon=lon_c, center_lat=lat_c,
            source_origin="lonlat",
            target_origin="user" if target_override is not None else "default:pose",
            force_local_tm=True)
        prepare_poses(all_poses, pose_crs_obj, plan.working_crs)
        log(f"位姿坐标系：{coords.crs_label(pose_crs_obj)}；"
            f"工作坐标系：{coords.crs_label(plan.working_crs)}；"
            f"输出坐标系：{coords.crs_label(plan.target_crs)}")
        if bounds[2] - bounds[0] > 0.5:
            log("警告：位姿经度跨度超过 0.5°，局部 TM 的网格北与真北可能存在偏差；"
                "如成果方向异常，请改用更合适的输出/工作投影并复核。")

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
                work, scale, _ = self._load_image(im_rec.path, use_fisheye)
                dets = self.detector.detect(work) if self.detector else []
                # 本车平台遮挡区抑制：画面底部（车顶/引擎盖/支架）的检出均为
                # 本车结构反光造成的误检，且该区域地物不具备地理参考意义。
                # 仅过滤中央区域，保留画面左右边缘紧邻车道的真实目标
                if dets:
                    H_w = work.shape[0]
                    W_w = work.shape[1]
                    def _not_ego(d):
                        y1 = d.bbox[1]
                        cx = (d.bbox[0] + d.bbox[2]) / 2
                        return not (y1 > 0.64 * H_w and 0.10 * W_w < cx < 0.90 * W_w)
                    dets = [d for d in dets if _not_ego(d)]
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
                    px_pts = [(px / scale, py / scale) for px, py in ln.points_px]
                    gcoords = []
                    dists = []
                    ok = True
                    for px, py in px_pts:
                        g = self.geo.pixel_to_ground(px, py, pose)
                        if not g.valid:
                            ok = False
                            break
                        gcoords.append((g.x, g.y))
                        dists.append(g.dist)
                    if ok and gcoords:
                        lane_feat.append({
                            "session": im_rec.session, "image": im_rec.filename, "side": ln.side,
                            "points": gcoords, "avg_dist": float(np.mean(dists)),
                        })

                session_points.setdefault(im_rec.session, []).extend(pts_feat)
                session_lanes.setdefault(im_rec.session, []).extend(lane_feat)

                # 预览
                prev = work.copy()
                self._draw(prev, dets, lanes)
                from .imgio import imwrite_unicode
                imwrite_unicode(str(out / "preview" / f"{im_rec.session}__{im_rec.filename}"), prev)

                if progress_cb:
                    cats = {}
                    for p in pts_feat:
                        c = CATEGORY_CN.get(p["class_id"])
                        if c:
                            cats[c] = cats.get(c, 0) + 1
                    progress_cb(i + 1, total, im_rec.filename, len(pts_feat),
                                len(lane_feat), True, cats, im_rec.session)
            except Exception as e:
                self.errors.append(f"{im_rec.filename}: {e}")
                if progress_cb:
                    progress_cb(i + 1, total, im_rec.filename, 0, 0, False, {},
                                im_rec.session)

        # 导出每批次（shp/ 与 gdb/ 分区）；属性在 working 米制下计算，
        # 写盘前一次性转到目标 CRS。
        to_target = plan.to_target()
        for sname, pts in session_points.items():
            lanes = session_lanes.get(sname, [])
            pts_out = self._transform_point_features(pts, to_target)
            lanes_out = self._transform_line_features(lanes, to_target)
            sp = sl = ""
            if export_shp:
                sp = out / "shp" / f"result_{sname}.shp"
                sl = out / "shp" / f"result_lanes_{sname}.shp"
                export_points_shp(sp, pts_out, crs=plan.target_crs)
                export_lines_shp(sl, lanes_out, crs=plan.target_crs)
            gdb_ok = False
            if export_gdb:
                gdb = out / "gdb" / f"result_{sname}.gdb"
                gdb_ok = (export_points_gdb(gdb, "detections", pts_out,
                                            crs=plan.target_crs) and
                          export_lines_gdb(gdb, "lanes", lanes_out,
                                           crs=plan.target_crs))
                if not gdb_ok:
                    from .exporter import WARNINGS as _gdb_warn
                    while _gdb_warn:
                        self.errors.append(_gdb_warn.pop(0))
            cats = {}
            for p in pts:
                c = CATEGORY_CN.get(p.get("class_id"))
                if c:
                    cats[c] = cats.get(c, 0) + 1
            per_session[sname] = {"det": len(pts), "lane": len(lanes), "cats": cats,
                                   "shp": str(sp) if sp else "",
                                   "gdb": str(gdb) if (export_gdb and gdb_ok) else ""}

        # 时序变化（v0.1 必做）：在 working 米制下匹配，导出前转目标 CRS
        change_count = 0
        change_path = ""
        if enable_change and len(session_points) >= 2:
            changes = detect_changes(session_points)
            change_count = len(changes)
            if changes:
                change_feats = [dict(c, class_id=c.get("class_id", -1),
                                     class_cn=c.get("class_cn", ""),
                                     session=f"{c['session_a']}->{c['session_b']}",
                                     image=c.get("change_type", ""),
                                     dist=0.0, score=0.0) for c in changes]
                change_feats = self._transform_point_features(change_feats, to_target)
                cp = ""
                if export_shp:
                    cp = out / "shp" / "result_change.shp"
                    export_points_shp(cp, change_feats, crs=plan.target_crs)
                if export_gdb:
                    if not export_points_gdb(out / "gdb" / "result_change.gdb",
                                             "changes", change_feats,
                                             crs=plan.target_crs):
                        from .exporter import WARNINGS as _gdb_warn
                        while _gdb_warn:
                            self.errors.append(_gdb_warn.pop(0))
                change_path = str(cp) if cp else ""

        import json
        report = {
            "data_dir": str(data_dir),
            "output_dir": str(out),
            **plan.report(),
            "per_session": per_session,
            "change_count": change_count,
            "change_path": change_path,
            "errors": self.errors,
        }
        report_path = out / "提取报告.json"
        try:
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.errors.append(f"提取报告写入失败: {e}")
            report_path = None
        return PipelineResult(str(out), per_session, change_count, change_path,
                              self.errors, plan.report(),
                              str(report_path) if report_path else "")
