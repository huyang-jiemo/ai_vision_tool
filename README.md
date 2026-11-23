# AI Vision Tool · 众源感知自动解译工具

免训练、双击可用的桌面客户端。输入众源感知影像目录（车载前视 / 无人机航拍 + 位姿），
自动完成 **车道线 / 交通标志 / 信号灯 / 路侧设施 / 动态障碍** 识别与时序变化提取，
输出 **SHP 与 File GDB**。输入输出坐标系不写死，成果 CRS 跟随主输入并可覆盖。

## 坐标系规则（v0.6.0 起）
- **输入 CRS 优先从数据自身读取**：影像直接读文件；矢量缺少或损坏坐标系时，先按
  同项目邻近矢量的坐标范围 + 主输入范围自动判断（如由轨迹 `.shp` 推断存量 GDB）。
  证据不足才报错（`CRS_REQUIRED`）并聚焦对应输入框；填写 EPSG:xxxx、WKT 或
  `.prj` 路径后重试。
- **输出默认跟随主输入**：车载跟随位姿（`.hdi` 经纬度，默认 EPSG:4326）；
  正射跟随 DOM 影像；变化比对跟随新成果 GDB。可用 `output_crs` 显式覆盖。
  车载 `.hdi` 的经纬度列固定为度，位姿坐标系只能填地理坐标系；需要投影成果时
  在“输出坐标系”中填写。
- **次要输入自动转换**：存量 GDB、比对底图逐层读取真实 CRS 后转到工作坐标系；
  GDB 内混合 CRS 也逐层转换，不再统一强制为某一种投影。
- **内部统一米制**：所有阈值、缓冲、匹配、长度/面积都在米制工作坐标系中计算；
  目标投影尺度或子午线收敛角不合格时，自动改用数据中心的局部 TM（不改变输出 CRS）。
- 支持经纬度（度）输入/输出；`DIST/AVGDIST/LEN_M/AREA_M2` 始终为米，与输出 CRS 无关。
- 仅支持 2D 地理/投影 CRS；compound 取水平分量，Engineering/地心 CRS 拒绝并提示。

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
- Windows：`dist/AIVisionTool/AIVisionTool.exe`（onedir 布局，中间产物）

### Windows 单文件交付（推荐）

客户只需要一个 **`AIVisionTool-Windows-x64.exe`**：双击即用、离线可用、无需安装
Python/GDAL/WebView2，也不需要解压一堆文件。

在 onedir 构建并拷入 `webview2-runtime/` 之后执行：

```bash
python tools/make_payload.py                 # → build/payload.zip + payload.version
pyinstaller build_launcher.spec --noconfirm --distpath dist_single
python tools/make_single_exe.py              # → dist_single/AIVisionTool-Windows-x64.exe
```

工作原理：
- 启动器本体只有十几 MB；`payload.zip` 追加在 exe 末尾（不在 PyInstaller datas 里，
  所以每次启动不会重复解压约 600 MB）。PyInstaller 6 从文件末尾向前搜索自身归档
  cookie，追加数据不影响启动，代码签名也兼容（**签名请对最终 exe 进行**）。
- **首次启动**：解压到 `%LOCALAPPDATA%\AIVisionTool\app\<版本>-<内容摘要>`（约需
  1.5 GB 空间），启动画面显示进度；完成后写 `.ready` 标记。
- **之后启动**：直接运行已解压的主程序，秒开；升级时按版本+内容摘要解压到新目录，
  成功后自动清理旧版本。
- IT 可用环境变量 `AVT_HOME` 把运行时目录重定向到共享盘/指定目录。
- 排障日志：`%LOCALAPPDATA%\AIVisionTool\启动器日志.log`。

> ⚠️ 交付 exe 约 600 MB 且会自解压到用户目录，未签名时可能触发 SmartScreen / 杀软
> 拦截。正式分发建议对**最终 exe**做代码签名，并在交付说明中注明首次启动需 1.5 GB
> 磁盘空间。CI 同时生成 `SHA256SUMS.txt` 供离线核验。

> `build/`、`dist/` 为本地构建缓存，已在 `.gitignore` 中，不随仓库分发。
>
> ⚠️ Windows 交付包还需内置 **WebView2 固定版运行时**（解压即用、目标机零安装零下载），
> 这一步由 CI 自动完成（见 `.github/workflows/build.yml`）。本地手工打包默认不含，
> 在缺少 WebView2 的内网机器上会因回退 IE 内核而白屏。

构建与发布由 GitHub Actions 完成（见 `.github/workflows/build.yml`，master 分支触发构建、tag `v*` 发布）。

## 目录结构
```
app.py / api.py        桌面壳与 JS↔Python 桥
launcher.py            单文件启动器（首启解压 payload，之后秒开）
web/                   唯一界面源（index.html + css/ + js/）
pipeline/              解译核心（config/coords/crs_plan/data_loader/undistort/detector/
                       lane_detector/georeference/change_detector/exporter/imgio/processor/
                       dom_extractor/comparator）
models/                yolo11n.onnx（入库权重）+ download_models.py（重新导出用）
tools/                 预览构建、DOM 车道提取、坐标/启动器/回归自检、payload 与单文件生成
docs/                  交付文档（说明 / 技术设计 / 测试文档，md + docx）
requirements*.txt      运行时 / 构建期依赖
build.spec             PyInstaller onedir 打包（主程序）
build_launcher.spec    PyInstaller onefile 打包（启动器）
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
