"""鱼眼矫正：cv2.fisheye。cv2 延迟导入，避免污染无 cv2 的导入链。"""
from __future__ import annotations

import numpy as np


class FisheyeCorrector:
    def __init__(self, fx, fy, cx, cy, k1, k2, w, h):
        self.w, self.h = w, h
        import cv2  # 延迟导入
        self.cv2 = cv2
        self.K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)
        self.D = np.array([k1, k2, 0.0, 0.0], dtype=np.float64)
        self.map1, self.map2 = cv2.fisheye.initUndistortRectifyMap(
            self.K, self.D, np.eye(3), self.K, (w, h), cv2.CV_16SC2)

    def correct(self, img: np.ndarray) -> np.ndarray:
        return self.cv2.remap(img, self.map1, self.map2, self.cv2.INTER_LINEAR)
