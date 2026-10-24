# AI 视觉自动解译工具 · 需求与实施方案（AI 可执行交付文档）

> 目标读者：接手实现的 AI / 工程师。本文档自包含——不需要读其他会话或历史即可开工。所有关键决策、约束、坑点、验证方法都写在这里。

---

## 0. 项目一句话

一个 **免训练、双击可用** 的桌面客户端，输入众源感知影像目录（车载前视 / 无人机航拍 + 位姿），自动完成"车道线 / 交通标志 / 信号灯 / 路侧设施 / 动态障碍"识别与时序变化提取，**输出 SHP 与 File GDB**。交付物 = 一个 macOS `.app`（Apple Silicon）和一个 Windows `.exe`，终端用户不需要装 Python、不需要任何运行时依赖。

---

## 1. 招标原始需求（背景，不可省略）

来自投标文件第 5 项"AI 自动化工具"：

需求 (1)：基于深度学习的 AI 视觉感知能力，识别 车道线、交通标志、交通信号灯、路侧设施、动态障碍物。影像源为 车载前视影像 与 无人机航摄影像。

需求 (2)：多期次影像时序对比，自动检测道路及周边要素变化，输出变化要素矢量成果。成果格式为 SHP 与 File GDB。

**用户明确放宽**：准确率 / 召回率**不必**达到 90% / 85%，演示级即可。**用户明确收紧**：不接受任何需要训练的方案；只允许"零训练"，即用现成预训练模型直接推理，或用纯算法（传统 CV）。

---

## 2. 硬约束清单（决策时优先级最高）

1. 交付形态：**原生桌面应用**。macOS 是 `.app`，Windows 是 `.exe`。用户直接双击打开运行。
2. 无签名：允许 Gatekeeper 弹窗"仍要打开"；不要为此引入 Apple Developer 账号。
3. 零训练：只用现成 COCO / Cityscapes 预训练权重，或纯算法。任何"跑一遍训练"的想法作废。
4. 零终端依赖：目标机器上不装 Python、不装 CUDA、不装 torch、不装 GDAL。
5. 资源占用低：CPU 推理即可，不引入 torch 运行时；打包体积可控在 400–800 MB。
6. 数据源目录 `众源感知测试数据源/`（3.5 GB）**绝不入 git**。
7. Git 仓库：`git@github.com:huyang-jiemo/ai_vision_tool.git`，默认分支 **master**（不是 main）。
8. 用户开发机：Apple Silicon（M 系列）Mac；构建通过 GitHub Actions 完成（本地不打包）。
9. Windows 优先度次之，先跑通 macOS 再管 Windows。

---

## 3. 技术选型（附理由，防止下游 AI 换栈）

| 关注点 | 选型 | 为什么不是别的 |
|---|---|---|
| 桌面壳 | **pywebview**（macOS 用 WKWebView / Windows 用 WebView2） | Electron 太重（>200MB 空壳）；Tauri 需要 Rust 工具链；Gradio / Streamlit 是浏览器 URL，不是原生 app；py2app 只出 `.app` 不出 `.exe`。pywebview 允许纯 Python 业务代码 + PyInstaller 一次性出 `.app` 和 `.exe`。 |
| 目标检测 | **Ultralytics YOLO11n** 预训练（COCO 80 类）→ 导出 ONNX → **ONNX Runtime CPU** 推理 | 零训练即用；COCO 里已有 car / truck / bus / traffic light / stop sign / bench 等本项目所需的类；推理只需 onnxruntime，运行时不装 torch。 |
| 车道线 | **传统 CV**：HSV 阈值 + 梯形 ROI + 轮廓 + 多项式拟合 | 现成的 LaneNet / CULane 权重要求转换、训练框架依赖，违反零训练。传统算法在行车记录仪视角下稳定，且完全可控。 |
| 鱼眼矫正 | `cv2.fisheye` 固定标定 | 无人机 / 运动相机有鱼眼；参数在 config 里可调。 |
| 地理参考 | 针孔模型 + 地平面求交 + 相机姿态 (heading/pitch/roll) → 旋转矩阵 | 数据源已含 GPS-IMU 位姿（.hdi 文本），无 SLAM / BA，够用；对演示级足够。 |
| 矢量输出 | **pyshp**（SHP） + **pyogrio**（File GDB, driver=OpenFileGDB） | GDAL pip 装会挂；pyogrio 自带裁剪过的 GDAL，支持 GDB 写。 |
| 打包 | **PyInstaller** spec 文件（macOS 侧再套 `BUNDLE`） | 一个 spec 同时能出 onedir/.exe 与 .app；`collect_all` 对 cv2 / onnxruntime / pyogrio / webview 都很稳。 |
| CI | GitHub Actions `macos-latest`(arm64) + `windows-latest`；tag 触发 Release | 用户不能本地打包（M 芯片本地 PyInstaller 会踩 cv2 dylib 签名问题），CI 干净。 |
| 模型来源 | **CI 构建时**下载 yolo11n.pt + 导出 onnx；产物打进 `.app` | 免训练 + 免把 300MB 权重塞进 repo。 |

---

## 4. 目录结构（工程根 = `ai_vision_tool/`）

```
ai_vision_tool/
├── app.py                      # pywebview 入口
├── api.py                      # JS↔Python 桥 + 后台任务 + 状态轮询
├── web/
│   └── index.html              # 单文件 UI（内联 CSS/JS，无 CDN）
├── pipeline/
│   ├── __init__.py
│   ├── config.py               # PipelineConfig, 常量, 坐标系 WKT
│   ├── data_loader.py          # .hdi 位姿 + 影像索引 + DataProject
│   ├── undistort.py            # FisheyeCorrector (cv2.fisheye)
│   ├── detector.py             # YoloDetector (ONNX Runtime) + NMS + COCO 类表
│   ├── lane_detector.py        # HSV+梯形 ROI+轮廓+polyfit
│   ├── georeference.py         # GeoReferencer（像素→WGS84→CGCS2000 GK）
│   ├── exporter.py             # SHP (pyshp) + GDB (pyogrio/OpenFileGDB)
│   └── processor.py            # Pipeline 编排, 进度回调
├── models/
│   ├── download_models.py      # 构建期脚本：YOLO11n.pt → yolo11n.onnx
│   ├── yolo11n.onnx            # 由 CI 生成, .gitignore 不入库
│   └── README.md
├── requirements.txt            # 运行时依赖
├── requirements-build.txt      # 仅构建期依赖（ultralytics/torch/onnx）
├── build.spec                  # PyInstaller spec
├── run.command                 # macOS 开发模式一键（venv + pip + run）
├── run.bat                     # Windows 开发模式一键
├── setup_git.sh                # 一次性 git init + commit + push
├── .gitignore
├── .github/workflows/build.yml # Actions
└── README.md
```

**必须存在的最小可执行入口 = `app.py`**，其他都是它引出的模块。

---

## 5. 数据源格式（务必按此解析，别再问用户）

`众源感知测试数据源/`（3.5 GB，用户本地已存在；git 里永远看不到）：

- **影像**：`images/*.jpg`，149 张，7360×3680（车载前视，鱼眼）。部分图 EXIF 旋转要 `ImageOps.exif_transpose` 归一化。
- **位姿**：`pose/*.hdi`（iScan 格式），**Tab 分隔 21 列**：
  - col 12 → **经度**（不是纬度）
  - col 13 → **纬度**
  - col 3 → 时间戳
  - col 4 → 影像文件名
  - col 5–7 → heading / pitch / roll（度）
  - **坑**：曾经把 12 和 13 颠倒，导致解算跑偏到南极。务必先打印前 3 行核对。
- **DOM**：EPSG:3857 Web Mercator，武汉 CGCS2000 GK 带号 114°20'E 中央经线，FE 800000，FN -3000000。SHP/GDB 里 .prj 用 CGCS2000_3_Degree_GK_Zone_39 的 WKT（已在 `config.py: WUHAN_CGCS2000_GK_PROJ` 给出）。
- **影像 ↔ 位姿匹配**：按 `sorted(images)` 与 `.hdi` 顺序做 zip；本项目 149 图 / 78 位姿，取 `min` 交集即可。

---

## 6. 关键模块契约

### 6.1 `pipeline/config.py`

```python
@dataclass
class PipelineConfig:
    detector_model: Path = MODELS_DIR / "yolo11n.onnx"
    imgsz: int = 640
    conf_threshold: float = 0.35
    iou_threshold: float = 0.45
    # COCO 类别子集：0=person,1=bicycle,2=car,3=motorbike,5=bus,7=truck,8=traffic light,10=stop sign
    target_classes: list[int] = field(default_factory=lambda: [0,1,2,3,5,7,8,10])
    fisheye: dict = field(default_factory=lambda: dict(k1=-1.0e-2, k2=1.0e-4, fx=3800, fy=3800, cx=3680, cy=2456))
    cam_height: float = 2.2            # 米
    resize_max_dim: int = 2048
```

Wuhan CGCS2000 3° 带 39 WKT 常量放这里；导出器直接用。

### 6.2 `pipeline/data_loader.py`

- `CameraPose(index, filename, timestamp, lon, lat, alt, heading, pitch, roll)`
- `ImageRecord(index, path, width, height, exif_orient)`
- `DataProject.load(root: Path) -> DataProject`：扫描 images + 解析 .hdi。暴露 `.images`, `.poses`, `.matched`（列表，长度 = min）。
- `_load_hdi(path)`：`.read_text().splitlines()` → `line.split("\t")`，**col 12 lon / col 13 lat**（硬编码，别改）。

### 6.3 `pipeline/undistort.py`

`FisheyeCorrector(fx, fy, cx, cy, k1, k2, w, h)` 用 `cv2.fisheye.initUndistortRectifyMap`；`.correct(img)` 走 `cv2.remap`。默认对车载前视启用；无人机图跳过。

### 6.4 `pipeline/detector.py`

- `Detection(bbox: tuple[int,int,int,int], class_id, class_name, score)`
- `YoloDetector(onnx_path, imgsz, conf, iou, target_classes)`
  - **坑**：`import cv2` 必须放在方法内部而不是文件顶，否则会让 georeference 模块在无 cv2 环境里 import 失败。
  - `_preprocess`：letterbox 到 imgsz、BGR→RGB、HWC→CHW、归一化 /255。
  - `_postprocess`：YOLO11 输出形状 (1, 84, 8400)，需要 transpose。
  - `_nms`：手写 IoU NMS（不引入 torch）。

### 6.5 `pipeline/lane_detector.py`

`LaneDetector`：HSV 提取白色 + 黄色 mask；梯形 ROI 保留下半屏；`findContours` → `polyfit(deg=2)`；拟合残差 >5 px 的丢弃。返回 `LaneLine(points_px, poly, fit_residual)`。

### 6.6 `pipeline/georeference.py`

**核心公式**（已验证与武汉测区 0.2° 对齐、地物距相机 4–80 m 合理）：

```
R_base    = [[1,0,0],[0,0,1],[0,-1,0]]                # X_cam→East, Z_cam→North, -Y_cam→Up
R_heading = [[cos_h, sin_h, 0],[-sin_h, cos_h, 0],[0,0,1]]
R         = R_heading @ R_base @ R_pitch @ R_roll

ray_world = R @ K^{-1} @ [u, v, 1]
t         = cam_height / (-ray_world.z)               # 地面 z=0
t         = clamp(t, 2, 120)                          # 米
ground_ecef = camera_origin + t * ray_world
lon, lat  = inverse_local_tangent(ground_ecef, ref_lon, ref_lat)
x, y      = pyproj.TransformFromProjection('EPSG:4326', WUHAN_CGCS2000_GK_PROJ)  # 输出平面米
```

`Detection` / `LaneLine` 类型必须走 `TYPE_CHECKING` 延迟导入，否则又踩 cv2 import 链。

### 6.7 `pipeline/exporter.py`

三个入口：

- `export_points(shp_path, features, wkt)` / `export_lines(...)`  — 用 pyshp，纯 python 稳定。
- `export_points_gdb(gdb_path, layer_name, features)` / `export_lines_gdb(...)` — 用 `pyogrio.write_arrays(driver="OpenFileGDB")`；shapely Point/LineString 转 numpy array。
- `export_summary(...)` 写一个统计点要素（图幅中心 + 计数）。
- **坑**：GDB 写入必须包 try/except，失败时**不能**拖垮整个流程；SHP 无论如何都要出。

### 6.8 `pipeline/processor.py`

`Pipeline.process_project(data_dir, output_dir, max_images=None, progress_cb=None, export_gdb=True) -> PipelineResult`

单张图 8 步：
1. 载入 & EXIF 归一化
2. 可选鱼眼矫正
3. 缩到 `resize_max_dim`
4. `YoloDetector.detect(img)`
5. `LaneDetector.detect(img)`
6. `GeoReferencer` 把每个 bbox 底边中点 + 每条 lane 的采样点 → 经纬度 + GK 平面
7. `_draw_results(img, dets, lanes)` → 存 jpg
8. `progress_cb(i, total, filename, n_det, n_lane)`

聚合完毕后：`ShapefileExporter.export_points(...)` + `export_lines(...)` + 可选 `export_*_gdb(...)`。

### 6.9 `app.py`

```python
import os, sys, webview
from api import Api
def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)
api = Api()
webview.create_window(
    title="AI Vision Tool - 众源感知自动解译工具",
    url=resource_path("web/index.html"),
    js_api=api, width=1280, height=860, min_size=(960, 640),
    background_color="#f7f8fa",
)
webview.start(debug=("--debug" in sys.argv),
              gui="cocoa" if sys.platform == "darwin" else None)
```

### 6.10 `api.py`

- `RunState`（threading.Lock + status / progress / logs / result / error）
- `Api` 方法（**这些名字必须与前端一致**）：
  - `ping()`
  - `model_status() -> {available: bool, path: str, size_bytes: int}`
  - `scan_directory(path) -> {images: int, poses: int, matched: int, sample: str}`
  - `start_pipeline(data_dir, output_dir, conf, max_images, use_fisheye, export_gdb) -> {started: bool}`（起一个 daemon 线程）
  - `get_status() -> {state, progress, current, total, logs, result, error}`（前端每 400 ms 轮询）
  - `preview_image(path) -> "data:image/png;base64,..."`
- 后台线程里调 `Pipeline.process_project(..., progress_cb=...)`，把每次回调写进 RunState。

### 6.11 `web/index.html`

单文件内联 CSS/JS。核心交互：数据目录输入 → 扫描 → 显示样本预览 → 参数（conf/maxImages/fisheye/GDB/输出目录） → 开始 → 轮询 `get_status()` → 进度条 + 日志 + 结果统计。`window.pywebview.api[name](args)` 返回 Promise；启动前等 `pywebviewready` 事件。

---

## 7. 打包与 CI

### 7.1 `requirements.txt`（运行时）

```
opencv-python
numpy>=1.24,<2.0
onnxruntime
Pillow
shapely
pyshp
pywebview>=4.4
pyproj
pyogrio>=0.7
```

### 7.2 `requirements-build.txt`（仅 CI）

```
ultralytics
torch
torchvision
onnx
onnxsim
```

### 7.3 `build.spec` 关键点

- `APP_NAME = "AI-Vision-Tool"`
- `datas = [("web", "web"), ("models/yolo11n.onnx", "models")]`
- `for pkg in ["webview","pyogrio","onnxruntime","cv2","numpy","shapely","pyproj"]: collect_all(pkg)`
- `hiddenimports += collect_submodules("webview")`
- `excludes = ["torch","torchvision","ultralytics","matplotlib","PyQt5","PySide6"]`
- `console=False`
- macOS 追加 `BUNDLE(coll, name="AI-Vision-Tool.app", bundle_identifier="com.zhongxiang.aivisiontool", info_plist={...})`

### 7.4 `.github/workflows/build.yml`

- `on: push: branches:[master], tags:['v*']; workflow_dispatch`
- Job A `build-macos`：`runs-on: macos-latest`（默认 arm64）→ `pip install -r requirements.txt pyinstaller` → `pip install ultralytics torch torchvision onnx` → `python models/download_models.py` → `pyinstaller build.spec --noconfirm` → `ditto -c -k --sequesterRsrc dist/AI-Vision-Tool.app AI-Vision-Tool-macOS-arm64.zip` → `upload-artifact@v4`
- Job B `build-windows`：`runs-on: windows-latest`，同上，产物用 `Compress-Archive`
- Job C `release`（仅 tag）：`softprops/action-gh-release@v2` 附件 = artifacts

### 7.5 `models/download_models.py`

CI 构建期跑；本地开发期 `run.command` 也跑一次。核心：`YOLO("yolo11n.pt").export(format="onnx", imgsz=640, simplify=True, opset=12)`。ultralytics 版本对产物落地路径不稳定，**用 glob `yolo11n*.onnx` + `**/yolo11n*.onnx` 遍历 + 按 mtime 取最新**再 `Path.replace` 到 `models/yolo11n.onnx`。

### 7.6 `setup_git.sh`

- 清 `.git/index.lock` + `find .git/objects/pack -name 'tmp_pack_*' -delete`
- `rm -rf .git && git init && git symbolic-ref HEAD refs/heads/master`
- `git config user.name huyang-jiemo` + `user.email huyang-jiemo@users.noreply.github.com`
- `git add -A && git commit -m "feat: AI Vision Tool v0.1"`
- `git remote add origin git@github.com:huyang-jiemo/ai_vision_tool.git`
- `git push -u origin master --force`

---

## 8. 已知坑（不要重复踩）

1. **`.hdi` 第 12 列 = 经度、第 13 列 = 纬度**（不是相反）。颠倒一次就会把解算跑到南极。
2. **cv2 顶层 import 会传染**：任何间接 import detector 的模块（比如 georeference）在没有 cv2 的环境里会连锁 fail。detector 内部改成 lazy；georeference 用 `TYPE_CHECKING`。
3. **GDAL 不能 pip 直装**（沙箱/CI 都会挂）。用 **pyogrio**（自带裁剪版 GDAL，支持 OpenFileGDB 写）。
4. **Windows .app/.exe 需要 WebView2 Runtime**：Windows 10/11 默认自带；老版本 pywebview 会提示用户装，不用管。
5. **numpy 2.x + opencv-python 老版本 wheel 会崩**：requirements 里必须 `numpy>=1.24,<2.0`。
6. **PyInstaller 打包 WKWebView 需要 Info.plist**：`NSHighResolutionCapable=true`；否则 retina 上 UI 模糊。
7. **Actions 上传 artifact 里 `dist/AI-Vision-Tool.app` 是 bundle**，必须 `ditto -c -k` 而不是 `zip -r`（zip 会丢扩展属性、codesign 结构）。
8. **GitHub 默认分支不是 master 是 main**：用户仓库已改到 master；workflow `branches: [master]` 别漏。
9. **首次 git 提交 3.5 GB 数据源会撑爆 Actions runner**：`.gitignore` 必须先写、`.git` 必须先 `rm -rf` 再 `git init`。
10. **ultralytics 导出 onnx 的落点跨版本不一致**：见 7.5，glob 兜底。

---

## 9. 验收清单（每个都必须绿勾才算交付）

- [ ] `python -m py_compile app.py api.py pipeline/*.py models/*.py` 全部 OK
- [ ] 加载 3.5 GB 数据源目录：`DataProject.load(...)` 返回 149 图 / 78 位姿 / 78 匹配
- [ ] 一张样例图跑完 8 步 pipeline：无异常；输出标注 jpg 可见 box / lane 覆盖合理
- [ ] YOLO 后处理 + NMS 用合成张量单测通过（IoU=0.5 抑制，score 阈值生效）
- [ ] 拟合车道线残差中位数 < 3 px
- [ ] 地理参考：任意 bbox 底边中点 → lon/lat 距相机 GPS 4–80 m，heading 与 GNSS 对齐误差 < 2°
- [ ] SHP 导出：至少 1 个 pts 层 + 1 个 lines 层；`pyshp.Reader` 能读，字段与 prj 完整
- [ ] GDB 导出：`pyogrio.list_layers` 能列出同名 2 层；ArcGIS Pro / QGIS 打开可看到
- [ ] GitHub Actions：`build-macos` 与 `build-windows` 两个 job 均绿
- [ ] 用户下载 `AI-Vision-Tool-macOS-arm64.zip` → 双击 `.app` → Gatekeeper "仍要打开" → 应用窗体出现，模型徽标"已就绪"
- [ ] 用户选择数据源目录 → 扫描显示 149 图 / 78 位姿 → 点开始 → 进度条到 100% → 输出目录里有 `result.shp`（点） + `result_lanes.shp`（线） + `result.gdb` + 预览图

---

## 10. 交付流程（按顺序执行，不许跳步）

1. 依 §4 建目录 → §6 填模块 → §7 填打包/CI 文件。
2. 本地 `python -m py_compile` 通过。
3. `bash setup_git.sh`（在用户 Mac 终端跑；沙箱不能推，原因见 §11）。
4. 打开 `https://github.com/huyang-jiemo/ai_vision_tool/actions` 看首次 run；失败就读日志、改代码、重推。
5. 两个 job 全绿后，从 Actions run 页下载 `AI-Vision-Tool-macOS-arm64.zip`；解压 → 拖到 Applications → 双击。
6. 在应用里选数据目录 → 扫描 → 跑一遍 pipeline → 检查输出。
7. 一切就绪后，在 CI 上打 tag `v0.1.0` 触发 Release job 生成正式版本产物。

---

## 11. 环境说明（下游 AI 必读）

- **执行 git push 必须在用户的 Mac 上做**。原因：QoderWork 沙箱没有 SSH 私钥、22 端口被封、GitHub 令牌页需要 2FA 验证码；Computer Use 出于安全策略禁止驱动 Terminal。用户 Mac 已配 SSH 到 `git@github.com:huyang-jiemo/ai_vision_tool`。
- Actions 构建日志、Repo 页面、Releases 页可以用 browser 工具（用户 Chrome 已登录 GitHub）远程查看，无需用户在 Mac 上打开。
- 数据源目录：`/Users/huyang/Downloads/ai_vision_tool/众源感知测试数据源/`（沙箱内对应 `/sessions/.../mnt/ai_vision_tool/众源感知测试数据源/`），**只读、只用于验证、不入 git**。

---

## 12. 明确不做的事

- 不做训练、不做微调、不做半监督。
- 不做 Web 版部署、不做 Docker 化。
- 不做多语种 UI（只中文）。
- 不做时序变化的"分类级 diff"（先只做像素 + bbox 层的变化输出，用户可接受）；如需真正变化检测，走 v0.2。
- 不做用户手册、不做安装向导（README 一页纸即可）。
- 不追求准确率 / 召回率达标；不接外场真值。

---

## 13. 里程碑建议

- **M1（1 天）**：代码骨架完整、`py_compile` 通过、本地跑通 pipeline 逻辑（不打包）。
- **M2（0.5 天）**：推到 GitHub、Actions 首次绿。
- **M3（0.5 天）**：`.app` 装机、双击可用、端到端出 SHP/GDB。
- **M4（0.5 天）**：Windows 打包补齐；打 tag 出 Release。

---

## 14. 一句话给下一个执行者

**先照 §4 建文件，§6 抄签名，§7 抄 CI，写完就 §9 逐条自检；一条不绿就回头改，别加功能。零训练、零终端依赖、双击可用 = 唯一硬指标。**
