"""数据源加载：解析 iScan .hdi 位姿 + 影像索引，按期次配对。

关键修正（相对原文档）：
- 真实目录为 照片/<期次>/CAM_1/*.jpg + 轨迹/<期次>/iScan-Image-1.hdi（两期次）
- .hdi 21 列，列定义（0-based）已用真实数据 + 东湖路.shp 反查确认：
    col0  = 设备/序列号-时间戳 (如 00000000-01-20260327105948634)
    col2..8 = 年/月/日/时/分/秒/毫秒
    col9  = 相机 X（采集软件给出的投影东坐标，已含假东；新流程不使用）
    col10 = 相机 Y（采集软件给出的投影北坐标；新流程不使用）
    col11 = 相机 Z（高程，米）
    col12 = 经度（WGS84，度）
    col13 = 纬度（WGS84，度）
    col14 = heading（航向，度）
    col15 = pitch（俯仰，度）
    col16 = roll（横滚，度）
- 位姿 71+78=149 条，与 149 张图按期次 1:1 对应（非文档说的取 min=78）
- 车载链路以 col12/13 经纬度为准；col9/10 保留仅用于排障对比。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_TS_RE = re.compile(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{3})")


@dataclass
class CameraPose:
    session: str
    index: int
    image_filename: str
    timestamp: datetime | None
    lon: float
    lat: float
    alt: float
    heading: float
    pitch: float
    roll: float
    x: float      # 由经纬度转换得到的 working CRS 东坐标（米）
    y: float      # 由经纬度转换得到的 working CRS 北坐标（米）
    z: float      # 高程（米）


@dataclass
class ImageRecord:
    session: str
    index: int
    path: Path
    filename: str


def _warn(message: str) -> None:
    """跨平台告警输出：Windows 控制台可能不支持中文，不能因此中断加载。"""
    import sys
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            sys.stderr.buffer.write((message + "\n").encode("utf-8", "replace"))
            sys.stderr.flush()
        except Exception:
            pass


def _parse_ts(col0: str) -> datetime | None:
    m = _TS_RE.search(col0)
    if not m:
        return None
    y, mo, d, hh, mm, ss, ms = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), int(ms) * 1000)
    except ValueError:
        return None


class PoseDataError(ValueError):
    """HDI 位姿数据非法：坐标/姿态缺失或越界。"""

    code = "POSE_COORDINATES_INVALID"

    def __init__(self, message: str, *, line: int | None = None):
        super().__init__(message)
        self.line = line


def _load_hdi(path: Path):
    """返回按行解析的 CameraPose 列表（与影像按期次内顺序 1:1）。

    只有少于 21 列的行（表头/说明行）会被跳过；21 列数据的经纬度、高程或
    姿态非法时直接报错，避免静默丢行导致影像与位姿错位。
    """
    import math
    poses = []
    started = False
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        c = line.split("\t")
        if len(c) < 21:
            # 表头/说明行（通常无制表符）可以跳过；数据开始后或明显是
            # 制表符分隔的截断行必须报错，否则会静默错位配对。
            if not started and line.count("\t") < 5:
                continue
            raise PoseDataError(
                f"位姿文件「{path.name}」第 {i} 行只有 {len(c)} 列"
                f"（应为 21 列），请检查文件是否被截断。", line=i)
        started = True
        try:
            lon, lat = float(c[12]), float(c[13])
            alt, heading = float(c[11]), float(c[14])
            pitch, roll = float(c[15]), float(c[16])
        except (ValueError, IndexError) as e:
            raise PoseDataError(
                f"位姿文件「{path.name}」第 {i} 行坐标/姿态不是有效数字：{e}",
                line=i) from e
        if not all(math.isfinite(v) for v in (lon, lat, alt, heading, pitch, roll)):
            raise PoseDataError(
                f"位姿文件「{path.name}」第 {i} 行包含非有限值。", line=i)
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise PoseDataError(
                f"位姿文件「{path.name}」第 {i} 行经纬度越界：lon={lon}, lat={lat}。",
                line=i)
        # col9/10 是采集软件给出的投影坐标；新流程不再依赖它，解析失败也不影响。
        try:
            x, y = float(c[9]), float(c[10])
        except (ValueError, IndexError):
            x, y = 0.0, 0.0
        poses.append(CameraPose(
            session="", index=i, image_filename="", timestamp=_parse_ts(c[0]),
            lon=lon, lat=lat, alt=alt, heading=heading, pitch=pitch, roll=roll,
            x=x, y=y, z=alt,
        ))
    if not poses:
        raise PoseDataError(f"位姿文件「{path.name}」没有可用的 21 列数据行。")
    return poses


def validate_pose_coordinates(poses):
    """校验经纬度并返回 (min_lon, min_lat, max_lon, max_lat)。"""
    import math
    if not poses:
        raise PoseDataError("没有可用的位姿数据。")
    lons, lats = [], []
    for p in poses:
        if not math.isfinite(p.lon) or not math.isfinite(p.lat):
            raise PoseDataError(f"位姿 {p.session}/{p.image_filename} 经纬度非有限值。")
        if not (-180.0 <= p.lon <= 180.0 and -90.0 <= p.lat <= 90.0):
            raise PoseDataError(
                f"位姿 {p.session}/{p.image_filename} 经纬度越界："
                f"lon={p.lon}, lat={p.lat}。")
        lons.append(float(p.lon))
        lats.append(float(p.lat))
    return (min(lons), min(lats), max(lons), max(lats))


def prepare_poses(poses, pose_crs, working_crs, transformer=None) -> None:
    """把位姿经纬度批量转换到 working_crs，写入 pose.x/pose.y（米）。

    ``transformer`` 可由 CRSPlan.to_working() 提供（经目标 CRS 中转，保留
    PROJ 默认基准转换）；为空时直接按 pose_crs→working_crs 建立。
    """
    if not poses:
        return
    from . import coords
    lon = [float(p.lon) for p in poses]
    lat = [float(p.lat) for p in poses]
    tf = transformer if transformer is not None else \
        coords.build_transformer(pose_crs, working_crs, field="pose_crs")
    if tf is None:
        # 同一 CRS：按经纬度列即平面坐标处理（车载正常流程不会走到这里，
        # 因为 working 始终是局部 TM）
        for p in poses:
            p.x, p.y = float(p.lon), float(p.lat)
        return
    try:
        xs, ys = tf.transform(lon, lat)
    except Exception as e:
        raise coords.CRSTransformError(
            f"无法把位姿经纬度从「{coords.describe(pose_crs)}」转换到"
            f"工作坐标系「{coords.describe(working_crs)}」：{e}",
            field="pose_crs") from e
    import math
    for p, x, y in zip(poses, xs, ys):
        if not (math.isfinite(x) and math.isfinite(y)):
            raise coords.CRSTransformError(
                f"位姿 {p.session}/{p.image_filename} 转换后坐标非有限值。",
                field="pose_crs")
        p.x, p.y = float(x), float(y)


@dataclass
class DataProject:
    root: Path
    sessions: dict[str, dict] = None  # session -> {images:[ImageRecord], poses:[CameraPose]}

    def __post_init__(self):
        if self.sessions is None:
            self.sessions = {}

    @property
    def images(self):
        return [im for s in self.sessions.values() for im in s["images"]]

    @property
    def poses(self):
        return [p for s in self.sessions.values() for p in s["poses"]]

    @property
    def matched(self):
        """可解译的记录：(ImageRecord, CameraPose) 按期次内索引配对。"""
        out = []
        for s in self.sessions.values():
            for im, p in zip(s["images"], s["poses"]):
                out.append((im, p))
        return out

    @classmethod
    def load(cls, root: Path | str) -> "DataProject":
        root = Path(root)
        proj = cls(root=root)

        # 发现照片期次：任意名为 CAM_1 的目录下的 jpg
        photo_groups: dict[str, list[Path]] = {}
        for cam_dir in root.rglob("CAM_1"):
            if cam_dir.is_dir():
                session = cam_dir.parent.name  # 如 20260327_1-2（东湖路）
                imgs = sorted(cam_dir.glob("*.jpg"))
                if imgs:
                    photo_groups.setdefault(session, []).extend(imgs)

        # 发现位姿：任意 iScan-Image-1.hdi
        pose_groups: dict[str, Path] = {}
        for hdi in root.rglob("iScan-Image-1.hdi"):
            session = hdi.parent.name  # 轨迹 下与照片同名的期次
            pose_groups[session] = hdi

        # 兜底：文档假设的扁平结构 images/ + pose/
        if not photo_groups and (root / "images").is_dir():
            photo_groups["default"] = sorted((root / "images").glob("*.jpg"))
        if not pose_groups and (root / "pose").is_dir():
            for hdi in (root / "pose").glob("*.hdi"):
                pose_groups["default"] = hdi

        for session in sorted(set(photo_groups) | set(pose_groups)):
            imgs = photo_groups.get(session, [])
            hdi = pose_groups.get(session)
            poses = _load_hdi(hdi) if hdi else []
            imgs.sort(key=lambda p: p.name)
            n = min(len(imgs), len(poses))
            if n == 0:
                continue
            if len(imgs) != len(poses):
                # 容错：取交集，避免某期次缺数据导致整体崩溃
                _warn(f"[warn] 期次 {session}: 影像 {len(imgs)} 张 / "
                      f"位姿 {len(poses)} 条，按 min={n} 配对")
            recs = [ImageRecord(session=session, index=i, path=p, filename=p.name)
                    for i, p in enumerate(imgs[:n])]
            for i, p in enumerate(poses[:n]):
                p.session = session
                p.index = i
                p.image_filename = imgs[i].name
            proj.sessions[session] = {"images": recs, "poses": poses[:n]}
        return proj

    def summary(self) -> dict:
        return {
            "root": str(self.root),
            "sessions": {k: len(v["images"]) for k, v in self.sessions.items()},
            "images": len(self.images),
            "poses": len(self.poses),
            "matched": len(self.matched),
        }
