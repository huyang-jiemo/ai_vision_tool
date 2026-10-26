"""YOLO11n ONNX 目标检测（ONNX Runtime CPU，零训练）。

cv2 仅在方法内延迟导入。后处理针对 YOLO11 输出 (1,84,8400)。
NMS 手写，不引入 torch。
"""
from __future__ import annotations

import numpy as np

from .config import COCO_CLASSES


class Detection:
    __slots__ = ("bbox", "class_id", "class_name", "score")

    def __init__(self, bbox, class_id, class_name, score):
        self.bbox = bbox            # (x1, y1, x2, y2) 像素
        self.class_id = class_id
        self.class_name = class_name
        self.score = score


class YoloDetector:
    def __init__(self, onnx_path, imgsz=640, conf=0.35, iou=0.45, target_classes=None):
        import onnxruntime as ort  # 延迟导入，运行时不依赖 torch
        self.imgsz = imgsz
        self.conf = conf
        self.iou = iou
        self.target_classes = set(target_classes) if target_classes else None
        self.session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

    @staticmethod
    def _iou(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        return inter / (area_a + area_b - inter + 1e-9)

    def _nms(self, dets):
        dets = sorted(dets, key=lambda d: d.score, reverse=True)
        keep = []
        while dets:
            best = dets.pop(0)
            keep.append(best)
            dets = [d for d in dets if self._iou(best.bbox, d.bbox) < self.iou]
        return keep

    def _preprocess(self, img):
        import cv2
        h, w = img.shape[:2]
        scale = self.imgsz / max(w, h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(img, (nw, nh))
        canvas = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
        canvas[:nh, :nw] = resized
        blob = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        return np.ascontiguousarray(blob[None, ...]), scale, (nw, nh)

    def detect(self, img):
        blob, scale, (nw, nh) = self._preprocess(img)
        out = self.session.run(None, {self.input_name: blob})[0]  # (1,84,8400)
        pred = out[0].T  # (8400,84)
        boxes, scores, class_ids = [], [], []
        H, W = img.shape[:2]
        for row in pred:
            cls = row[4:]
            cid = int(np.argmax(cls))
            sc = float(cls[cid])
            if sc < self.conf:
                continue
            if self.target_classes is not None and cid not in self.target_classes:
                continue
            x, y, bw, bh = row[:4]
            # 模型坐标为 1280 画布坐标（letterbox 左上对齐），除以缩放系数即回原图
            x1 = max(0.0, (x - bw / 2) / scale)
            y1 = max(0.0, (y - bh / 2) / scale)
            x2 = min(float(W), (x + bw / 2) / scale)
            y2 = min(float(H), (y + bh / 2) / scale)
            boxes.append([x1, y1, x2, y2])
            scores.append(sc)
            class_ids.append(cid)
        dets = [Detection(b, c, COCO_CLASSES[c], s) for b, c, s in zip(boxes, class_ids, scores)]
        return self._nms(dets)
