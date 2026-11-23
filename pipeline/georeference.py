"""地理参考：像素 → working CRS 平面坐标（米）。

- 相机原点 pose.x/y 由 .hdi 的 col12/13 经纬度在任务开始时转换到 working CRS；
  working 默认是以前方平均经线为中央经线的局部 TM，中心处 grid north≈true north。
- 内参取自 config.fisheye；外参取位姿的 heading/pitch/roll。
- 地面 z=0，相机高程 = pose.alt；射线向下与地面求交得平面坐标。
- 本模块只负责米制平面几何；输入/输出 CRS 的转换在 processor/exporter 完成。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from .config import PipelineConfig

if TYPE_CHECKING:
    from .data_loader import CameraPose


@dataclass
class GeoPoint:
    x: float       # working CRS 东坐标（米）
    y: float       # working CRS 北坐标（米）
    dist: float    # 距相机水平距离（米）
    valid: bool


# 相机→世界（ENU）基变换：相机 +x(右)→East, +z(前)→North, +y(下)→-Up
R_BASE = np.array([[1.0, 0.0, 0.0],
                   [0.0, 0.0, 1.0],
                   [0.0, -1.0, 0.0]])


def _rot_yaw(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])


def _rot_pitch(p):
    c, s = math.cos(p), math.sin(p)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def _rot_roll(r):
    c, s = math.cos(r), math.sin(r)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


class GeoReferencer:
    def __init__(self, config: PipelineConfig):
        self.cfg = config
        f = config.fisheye
        self.fx, self.fy = f["fx"], f["fy"]
        self.cx, self.cy = f["cx"], f["cy"]

    def _rotation(self, heading_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
        a, p, r = map(math.radians, (heading_deg, pitch_deg, roll_deg))
        return _rot_yaw(a) @ R_BASE @ _rot_pitch(p) @ _rot_roll(r)

    def pixel_to_ground(self, u: float, v: float, pose: "CameraPose") -> GeoPoint:
        """像素(u,v) → 地面 working 坐标。无效（射线不朝下/位姿非法）返回 valid=False。"""
        if not (math.isfinite(pose.x) and math.isfinite(pose.y)
                and math.isfinite(pose.alt)):
            return GeoPoint(pose.x, pose.y, 0.0, False)
        x_c = (u - self.cx) / self.fx
        y_c = (v - self.cy) / self.fy
        z_c = 1.0
        R = self._rotation(pose.heading, pose.pitch, pose.roll)
        world = R @ np.array([x_c, y_c, z_c])  # [East, North, Up]
        up = world[2]
        if up >= -1e-6:
            return GeoPoint(pose.x, pose.y, 0.0, False)
        t = -pose.alt / up  # 射线参数，不是距离
        gx = pose.x + t * world[0]
        gy = pose.y + t * world[1]
        # DIST/AVGDIST 是地面水平距离；t 只在相机中心像素才等于水平距离。
        dist = math.hypot(gx - pose.x, gy - pose.y)
        return GeoPoint(gx, gy, dist, True)

    def bbox_bottom_center(self, x1: float, y2: float, pose: "CameraPose") -> GeoPoint:
        """bbox 底边中点 (x1,y2) → 地面坐标（目标落点）。"""
        return self.pixel_to_ground(x1, y2, pose)
