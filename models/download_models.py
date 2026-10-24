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
    # dynamic=True：空间维度动态，运行期按 imgsz=1280 推理以提升小目标召回
    result = model.export(format="onnx", dynamic=True, simplify=True,
                          opset=17, imgsz=640)

    # ultralytics 的 export() 返回导出路径（字符串）；不同版本落点不同
    # （可能落在仓库根目录或 models/），统一兜底搜索后移动到 models/yolo11n.onnx
    src = None
    if isinstance(result, (str, Path)) and Path(result).exists():
        src = Path(result)
    if src is None:
        candidates = []
        for base in (ROOT, ROOT.parent):
            candidates += list(base.glob("yolo11n*.onnx"))
        candidates = [p for p in candidates if p != OUT]
        if candidates:
            src = max(candidates, key=lambda p: p.stat().st_mtime)
    if src is None or not src.exists():
        raise RuntimeError("未找到导出的 yolo11n onnx 文件")
    src.replace(OUT)
    print(f"[ok] 模型已生成: {OUT}  ({OUT.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
