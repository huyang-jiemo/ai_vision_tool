"""Pipeline 全局配置与常量。

坐标系 WKT / proj 直接取自真实参考成果 `轨迹/*东湖路.prj`，
保证输出与参考矢量完全重合（武汉本地横墨卡托，中央经线 114°20'E）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"

# 武汉本地横墨卡托（取自参考 .prj，中央经线 114.3333333° = 114°20'E）
WUHAN_TM_PROJ = (
    "+proj=tmerc +lat_0=0 +lon_0=114.3333333333333 +k=1 "
    "+x_0=800000 +y_0=-3000000 +ellps=GRS80 +units=m +no_defs +type=crs"
)

WUHAN_CGCS2000_GK_WKT = (
    'PROJCS["WuHan_WH2000_at_11420_E_CGCS2000",'
    'GEOGCS["GCS_WuHanCGCS2000",'
    'DATUM["D_WuHanCGCS2000",SPHEROID["ElliCGCS2000",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["false_easting",800000.0],'
    'PARAMETER["false_northing",-3000000.0],'
    'PARAMETER["central_meridian",114.3333333333333],'
    'PARAMETER["scale_factor",1.0],'
    'PARAMETER["latitude_of_origin",0.0],'
    'UNIT["Meter",1.0]]'
)

# COCO 80 类（标准顺序，索引即类别号）
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]

# COCO 标准索引：9=traffic light，10=fire hydrant，11=stop sign，
# 12=parking meter，13=bench（10/12/13 归入路侧设施）
TARGET_CLASSES = [0, 1, 2, 3, 5, 7, 9, 10, 11, 12, 13]

# 输出要素的中文类别名（用于导出属性）
CLASS_CN = {
    0: "行人", 1: "自行车", 2: "小汽车", 3: "摩托车", 5: "公交车",
    7: "卡车", 9: "交通信号灯", 10: "路侧设施", 11: "交通标志",
    12: "路侧设施", 13: "路侧设施",
}

# 变化检测要素（v0.1 时序对比关注的类别）
CHANGE_TRACKED_CLASSES = [2, 3, 5, 7, 9, 11]  # 车辆类 + 信号灯 + 停止标志

# 业务四大类别归属（日志/汇总按此口径统计）
CATEGORY_CN = {
    0: "动态障碍", 1: "动态障碍", 2: "动态障碍", 3: "动态障碍",
    5: "动态障碍", 7: "动态障碍",
    9: "信号灯",
    10: "路侧设施", 12: "路侧设施", 13: "路侧设施",
    11: "交通标志",
}
BUSINESS_CATEGORIES = ["动态障碍", "交通标志", "信号灯", "路侧设施"]


@dataclass
class PipelineConfig:
    detector_model: Path = MODELS_DIR / "yolo11n.onnx"
    # 动态尺寸导出的 ONNX：1536 相比 1280 检出提升约 30%（行人/卡车尤明显），
    # CPU 单图推理约 0.16s，实测误检可控
    imgsz: int = 1536
    conf_threshold: float = 0.15
    iou_threshold: float = 0.50
    target_classes: list[int] = field(default_factory=lambda: list(TARGET_CLASSES))
    # 鱼眼内参（7360x4912）；cx/cy 为真实尺寸一半，k1/k2 为初值，可在配置中微调
    fisheye: dict = field(default_factory=lambda: dict(
        k1=-1.0e-2, k2=1.0e-4, fx=3800.0, fy=3800.0, cx=3680.0, cy=2456.0))
    cam_height_default: float = 2.2          # 仅当位姿缺失高程时兜底（米）
    resize_max_dim: int = 2048               # 推理前最长边缩放到此
    use_fisheye: bool = True
    enable_change_detection: bool = True     # v0.1 必做：多期次时序变化
