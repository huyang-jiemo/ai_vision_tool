"""构建期脚本：下载 YOLO11n 预训练权重并导出 ONNX（零训练，仅推理用）。

本地开发也可运行一次以生成 models/yolo11n.onnx。
ultralytics 导出落点跨版本不稳定，故用 glob + mtime 兜底取最新。
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "yolo11n.onnx"


def main():
    from ultralytics import YOLO
    model = YOLO("yolo11n.pt")  # 自动从 ultralytics hub 下载
    model.export(format="onnx", imgsz=640, simplify=True, opset=12, dynamic=False)

    # glob 兜底：遍历常见落点，按 mtime 取最新
    candidates = []
    for pat in ("yolo11n*.onnx", "**/yolo11n*.onnx"):
        for p in glob.glob(str(ROOT / pat), recursive=True):
            candidates.append(Path(p))
    if not candidates:
        raise RuntimeError("未找到导出的 yolo11n onnx 文件")
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    latest.replace(OUT)
    print(f"[ok] 模型已生成: {OUT}  ({OUT.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
