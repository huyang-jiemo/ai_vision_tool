# AI Vision Tool · 众源感知自动解译工具

免训练、双击可用的桌面客户端。输入众源感知影像目录（车载前视 / 无人机航拍 + 位姿），
自动完成 **车道线 / 交通标志 / 信号灯 / 路侧设施 / 动态障碍** 识别与时序变化提取，
输出 **SHP 与 File GDB**（武汉本地横墨卡托，中央经线 114°20'E）。

## 硬约束
- 原生桌面应用（macOS `.app` / Windows `.exe`），双击即用，目标机无需装 Python / torch / GDAL。
- 零训练：仅用 YOLO11n 预训练权重（COCO）直接推理 + 传统 CV 车道线。
- 无签名：macOS 允许 Gatekeeper「仍要打开」。

## 快速开始（开发）
```bash
bash run.command        # macOS
run.bat                # Windows
```
构建与发布由 GitHub Actions 完成（见 `.github/workflows/build.yml`，master 分支触发）。

## 目录结构
```
app.py / api.py        桌面壳与 JS↔Python 桥
web/index.html         单文件 UI
pipeline/              解译核心（config/data_loader/undistort/detector/
                       lane_detector/georeference/change_detector/exporter/processor）
models/                download_models.py 构建期导出 yolo11n.onnx
requirements*.txt     运行时 / 构建期依赖
build.spec             PyInstaller 打包
```

## 数据源
目录约定（已按真实数据核对）：
- `照片/<期次>/CAM_1/*.jpg` 影像
- `轨迹/<期次>/iScan-Image-1.hdi` 位姿（21 列，col9/10/11=相机 TM 坐标 X/Y/Z，col12/13=经纬度，col14/15/16=航向/俯仰/横滚）

## 输出
- `result_<期次>.shp` / `.gdb`：检测目标点 + 车道线
- `result_change.shp` / `.gdb`：多期次时序变化要素（appeared / disappeared / stable）
- `preview/`：逐张标注预览
