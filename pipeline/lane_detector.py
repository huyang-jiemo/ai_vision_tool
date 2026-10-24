"""车道线检测：HSV 白/黄掩膜 + Canny 门控 + Hough 直线段 + 分侧 RANSAC 拟合。

针对城市道路场景的三重约束抑制误检：
1. 严格颜色掩膜沿线段采样评分（≥55%），排除护栏/车顶架等灰亮结构；
2. 排除本车平台遮挡区（底部中央引擎盖/车顶/支架）；
3. 收敛方向约束：左线上端向中心收（x_top > x_bot），右线相反，且上端
   延伸需落在消失点邻域内。
每侧最多输出 2 条线（本车道边界 + 相邻车道线），采样点仅取观测证据区间，
不向遮挡区外推。
"""
from __future__ import annotations

import numpy as np


class LaneLine:
    __slots__ = ("points_px", "poly", "fit_residual", "side")

    def __init__(self, points_px, poly, fit_residual, side):
        self.points_px = points_px      # [(x,y),...] 像素
        self.poly = poly                # np.poly1d（x = a*y+b）
        self.fit_residual = fit_residual
        self.side = side                # 'left' / 'right'


class LaneDetector:
    def __init__(self, roi_ratio=0.50, min_support=120, max_lines_per_side=2):
        self.roi_ratio = roi_ratio
        self.min_support = min_support
        self.max_lines_per_side = max_lines_per_side

    def detect(self, img: np.ndarray):
        import cv2
        rng = np.random.default_rng(42)
        h, w = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        white = cv2.inRange(hsv, np.array([0, 0, 200]), np.array([180, 50, 255]))
        yellow = cv2.inRange(hsv, np.array([15, 80, 110]), np.array([40, 255, 255]))
        color_mask = cv2.bitwise_or(white, yellow)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edges = cv2.dilate(edges, np.ones((5, 5), np.uint8))
        mask = cv2.bitwise_and(color_mask, edges)

        roi_top = int(h * self.roi_ratio)
        quad = np.array([[int(w * 0.05), h], [int(w * 0.38), roi_top],
                         [int(w * 0.62), roi_top], [int(w * 0.95), h]], np.int32)
        roi = np.zeros_like(mask)
        cv2.fillPoly(roi, [quad], 255)
        mask = cv2.bitwise_and(mask, roi)
        # 排除本车平台遮挡区
        ego = np.zeros_like(mask)
        cv2.rectangle(ego, (int(w * 0.28), int(h * 0.70)), (int(w * 0.74), h), 255, -1)
        mask = cv2.bitwise_and(mask, cv2.bitwise_not(ego))

        raw = cv2.HoughLinesP(mask, 1, np.pi / 180, threshold=35,
                              minLineLength=40, maxLineGap=30)
        if raw is None:
            return []

        sides = {"left": [], "right": []}
        for x1, y1, x2, y2 in raw.reshape(-1, 4):
            n = 20
            xs = np.linspace(x1, x2, n).astype(int)
            ys = np.linspace(y1, y2, n).astype(int)
            ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
            if ok.sum() < n * 0.8:
                continue
            if color_mask[ys[ok], xs[ok]].mean() / 255.0 < 0.55:
                continue
            length = float(np.hypot(x2 - x1, y2 - y1))
            ang = np.degrees(np.arctan2(abs(x2 - x1), abs(y2 - y1)))
            if not (10 <= ang <= 75):
                continue
            sides["left" if (x1 + x2) / 2 < w / 2 else "right"].append(
                (x1, y1, x2, y2, length))

        lanes = []
        for side, segs in sides.items():
            pts = []
            for x1, y1, x2, y2, length in segs:
                pts += [(y1, x1, length), (y2, x2, length)]
            if len(pts) < 6:
                continue
            P = np.array(pts, np.float64)
            remaining = P.copy()
            for _ in range(self.max_lines_per_side):
                if len(remaining) < 6:
                    break
                best_inl, best_sup = None, 0
                for _ in range(300):
                    i, j = rng.integers(0, len(remaining), 2)
                    if i == j:
                        continue
                    y1, x1, _ = remaining[i]
                    y2, x2, _ = remaining[j]
                    if abs(y2 - y1) < 20:
                        continue
                    a = (x2 - x1) / (y2 - y1)
                    b = x1 - a * y1
                    inl = np.abs(a * remaining[:, 0] + b - remaining[:, 1]) < 6
                    sup = remaining[inl, 2].sum()
                    if sup > best_sup:
                        best_sup, best_inl = sup, inl
                if best_inl is None or best_sup < self.min_support:
                    break
                Q = remaining[best_inl]
                a, b = np.polyfit(Q[:, 0], Q[:, 1], 1, w=Q[:, 2])
                x_top, x_bot = a * roi_top + b, a * (h - 1) + b
                if not (0.25 * w <= x_top <= 0.75 * w):
                    continue
                if not (0.02 * w <= x_bot <= 0.98 * w):
                    continue
                if side == "left" and x_top <= x_bot:
                    continue
                if side == "right" and x_top >= x_bot:
                    continue
                resid = float(np.median(np.abs(a * Q[:, 0] + b - Q[:, 1])))
                # 采样点仅取观测证据区间（不外推到遮挡区）
                y_lo = max(roi_top, float(Q[:, 0].min()) - 0.02 * h)
                y_hi = min(h - 1.0, float(Q[:, 0].max()) + 0.04 * h)
                ys = np.linspace(y_lo, y_hi, 12)
                sample = [(float(a * y + b), float(y)) for y in ys
                          if 0 <= a * y + b < w]
                if len(sample) >= 6:
                    lanes.append(LaneLine(sample, np.poly1d([a, b]), resid, side))
                remaining = remaining[~best_inl]
        return lanes
