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
bash run.command        # macOS：建 venv → 装依赖 → 起界面
run.bat                # Windows
```

## 从源码构建安装包
仓库只保留源码（约 30 MB），克隆即可构建；推理权重 `models/yolo11n.onnx` 已入库，
**无需联网下载模型**。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-build.txt
pyinstaller build.spec --noconfirm
```

产物：
- macOS：`dist/AIVisionTool.app`（交付包用 `ditto -c -k --sequesterRsrc` 压成 zip）
- Windows：`dist/AIVisionTool/AIVisionTool.exe`（onedir 布局）

> `build/`、`dist/` 为本地构建缓存，已在 `.gitignore` 中，不随仓库分发。
>
> ⚠️ Windows 交付包还需内置 **WebView2 固定版运行时**（解压即用、目标机零安装零下载），
> 这一步由 CI 自动完成（见 `.github/workflows/build.yml`）。本地手工打包默认不含，
> 在缺少 WebView2 的内网机器上会因回退 IE 内核而白屏。

构建与发布由 GitHub Actions 完成（见 `.github/workflows/build.yml`，master 分支触发构建、tag `v*` 发布）。

## 目录结构
```
app.py / api.py        桌面壳与 JS↔Python 桥
web/                   唯一界面源（index.html + css/ + js/）
pipeline/              解译核心（config/data_loader/undistort/detector/
                       lane_detector/georeference/change_detector/exporter/imgio/processor）
models/                yolo11n.onnx（入库权重）+ download_models.py（重新导出用）
tools/                 预览构建、DOM 车道提取等辅助脚本
docs/                  交付文档（说明 / 技术设计 / 测试文档，md + docx）
requirements*.txt      运行时 / 构建期依赖
build.spec             PyInstaller 打包
```
本地 IDE 与工具目录（`.idea/`、`.workbuddy/`、`.venv/`）不入库。

## 数据源
目录约定（已按真实数据核对）：
- `照片/<期次>/CAM_1/*.jpg` 影像
- `轨迹/<期次>/iScan-Image-1.hdi` 位姿（21 列，col9/10/11=相机 TM 坐标 X/Y/Z，col12/13=经纬度，col14/15/16=航向/俯仰/横滚）

## 输出
- `result_<期次>.shp` / `.gdb`：检测目标点 + 车道线
- `result_change.shp` / `.gdb`：多期次时序变化要素（appeared / disappeared / stable）
- `preview/`：逐张标注预览
