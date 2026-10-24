"""车道线检测：传统 CV（HSV 白/黄掩膜 + 梯形 ROI + 轮廓 + 二次多项式拟合）。

在行车视角、鱼眼矫正后的影像下稳定，且完全可控，符合"演示级/零训练"。
"""
from __future__ import annotations

import numpy as np


class LaneLine:
    __slots__ = ("points_px", "poly", "fit_residual", "side")

    def __init__(self, points_px, poly, fit_residual, side):
        self.points_px = points_px      # [(x,y),...] 像素
        self.poly = poly                # np.poly1d
        self.fit_residual = fit_residual
        self.side = side                # 'left' / 'right'


class LaneDetector:
    def __init__(self, roi_ratio=0.45, min_residual=5.0, max_residual=8.0):
        self.roi_ratio = roi_ratio
        self.min_residual = min_residual
        self.max_residual = max_residual

    def detect(self, img: np.ndarray):
        import cv2
        h, w = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 白色车道线
        white = cv2.inRange(hsv, np.array([0, 0, 200]), np.array([180, 40, 255]))
        # 黄色车道线
        yellow = cv2.inRange(hsv, np.array([15, 80, 120]), np.array([40, 255, 255]))
        mask = cv2.bitwise_or(white, yellow)

        # 梯形 ROI：保留下半屏
        roi_top = int(h * self.roi_ratio)
        pts = np.array([[int(w * 0.1), h], [int(w * 0.4), roi_top],
                        [int(w * 0.6), roi_top], [int(w * 0.9), h]], np.int32)
        roi_mask = np.zeros_like(mask)
        cv2.fillPoly(roi_mask, [pts], 255)
        mask = cv2.bitwise_and(mask, roi_mask)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        lanes = []
        # 按 x 中值分左右
        left_pts, right_pts = [], []
        for c in contours:
            if cv2.contourArea(c) < 80:
                continue
            xy = c.reshape(-1, 2)
            cx = xy[:, 0].mean()
            if cx < w / 2:
                left_pts.extend(xy.tolist())
            else:
                right_pts.extend(xy.tolist())
        for side, pts in (("left", left_pts), ("right", right_pts)):
            if len(pts) < 10:
                continue
            pts = np.array(pts, dtype=np.float64)
            if np.std(pts[:, 1]) < 5:
                continue
            poly = np.poly1d(np.polyfit(pts[:, 1], pts[:, 0], 2))
            resid = float(np.mean(np.abs(poly(pts[:, 1]) - pts[:, 0])))
            if resid > self.max_residual:
                continue
            # 沿 y 采样用于地理参考
            ys = np.linspace(roi_top, h - 1, 10)
            sample = [(float(poly(y)), float(y)) for y in ys]
            lanes.append(LaneLine(sample, poly, resid, side))
        return lanes
