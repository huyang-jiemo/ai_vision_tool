"""数据源加载：解析 iScan .hdi 位姿 + 影像索引，按期次配对。

关键修正（相对原文档）：
- 真实目录为 照片/<期次>/CAM_1/*.jpg + 轨迹/<期次>/iScan-Image-1.hdi（两期次）
- .hdi 21 列，列定义（0-based）已用真实数据 + 东湖路.shp 反查确认：
    col0  = 设备/序列号-时间戳 (如 00000000-01-20260327105948634)
    col2..8 = 年/月/日/时/分/秒/毫秒
    col9  = 相机 X（武汉本地 TM 东坐标，已含 FE 800000）
    col10 = 相机 Y（武汉本地 TM 北坐标）
    col11 = 相机 Z（高程，米）
    col12 = 经度
    col13 = 纬度
    col14 = heading（航向，度）
    col15 = pitch（俯仰，度）
    col16 = roll（横滚，度）
- 位姿 71+78=149 条，与 149 张图按期次 1:1 对应（非文档说的取 min=78）
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
    x: float      # 武汉 TM 东坐标
    y: float      # 武汉 TM 北坐标
    z: float      # 高程


@dataclass
class ImageRecord:
    session: str
    index: int
    path: Path
    filename: str


def _parse_ts(col0: str) -> datetime | None:
    m = _TS_RE.search(col0)
    if not m:
        return None
    y, mo, d, hh, mm, ss, ms = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), int(ms) * 1000)
    except ValueError:
        return None


def _load_hdi(path: Path):
    """返回按行解析的 CameraPose 列表（与影像按期次内顺序 1:1）。"""
    poses = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        c = line.split("\t")
        if len(c) < 21:
            continue
        try:
            pose = CameraPose(
                session="", index=i,
                image_filename="", timestamp=_parse_ts(c[0]),
                lon=float(c[12]), lat=float(c[13]),
                alt=float(c[11]), heading=float(c[14]),
                pitch=float(c[15]), roll=float(c[16]),
                x=float(c[9]), y=float(c[10]), z=float(c[11]),
            )
        except (ValueError, IndexError):
            continue
        poses.append(pose)
    return poses


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
                print(f"[warn] 期次 {session}: 影像 {len(imgs)} 张 / 位姿 {len(poses)} 条，按 min={n} 配对")
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
