"""JS↔Python 桥：后台线程跑 Pipeline，前端轮询状态。"""
import base64
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.detector import YoloDetector
from pipeline.processor import Pipeline

MAX_IMAGES_NONE = None


class RunState:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"        # idle | running | done | error
        self.progress = 0
        self.total = 0
        self.current = ""
        self.logs = []
        self.result = None
        self.error = None
        self.files = []            # 逐文件状态 [{name,status,det,lane}]
        self.log_file = ""         # 本次运行日志文件路径（输出目录下）


class Api:
    def __init__(self):
        self.state = RunState()
        self.cfg = PipelineConfig()
        self._detector = None
        self._detector_err = None

    # ---- 前端调用 ----
    def ping(self):
        return "pong"

    def pick_folder(self, title="选择目录"):
        """弹出系统文件夹选择对话框，返回 {"ok": bool, "path": str}。

        注意：pywebview 5.x 起模块级 create_file_dialog 已移除，
        必须通过 window.create_file_dialog 调用。
        """
        import webview
        try:
            if not webview.windows:
                return {"ok": False, "path": "", "error": "窗口尚未就绪，请稍后再试"}
            result = webview.windows[0].create_file_dialog(
                webview.FOLDER_DIALOG,   # 20，兼容旧常量（新写法 FileDialog.FOLDER）
                directory=os.path.expanduser("~"))
            if result:
                return {"ok": True, "path": result[0]}
            return {"ok": False, "path": ""}   # 用户取消
        except Exception as e:
            return {"ok": False, "path": "", "error": str(e)}

    def model_status(self):
        p = Path(self.cfg.detector_model)
        if p.exists():
            return {"available": True, "path": str(p), "size_bytes": p.stat().st_size}
        return {"available": False, "path": str(p), "size_bytes": 0}

    def open_folder(self, path):
        """在系统文件管理器中打开目录。"""
        try:
            if not path or not os.path.isdir(path):
                return {"ok": False, "error": "目录不存在"}
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # noqa: 仅 Windows
            else:
                subprocess.Popen(["xdg-open", path])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def scan_directory(self, path):
        from pipeline.data_loader import DataProject
        try:
            proj = DataProject.load(path)
            s = proj.summary()
            sample = str(proj.images[0].path) if proj.images else ""
            return {"images": s["images"], "poses": s["poses"],
                    "matched": s["matched"], "sample": sample}
        except Exception as e:
            return {"images": 0, "poses": 0, "matched": 0, "sample": "", "error": str(e)}

    def start_pipeline(self, data_dir, output_dir, conf=0.20, max_images=None,
                       export_shp=True, export_gdb=True):
        if self.state.state == "running":
            return {"started": False, "error": "已在运行中"}
        t = threading.Thread(
            target=self._run,
            args=(data_dir, output_dir, conf, max_images, export_shp, export_gdb),
            daemon=True)
        t.start()
        return {"started": True}

    def get_status(self):
        with self.state.lock:
            return {
                "state": self.state.state,
                "progress": self.state.progress,
                "total": self.state.total,
                "current": self.state.current,
                "logs": self.state.logs[-200:],
                "result": self.state.result,
                "error": self.state.error,
                "files": self.state.files,
                "log_file": self.state.log_file,
            }

    def preview_image(self, path):
        try:
            with open(path, "rb") as f:
                b = base64.b64encode(f.read()).decode()
            return "data:image/jpeg;base64," + b
        except Exception:
            return ""

    # ---- 内部 ----
    def _get_detector(self):
        if self._detector is not None or self._detector_err:
            return self._detector
        if Path(self.cfg.detector_model).exists():
            try:
                self._detector = YoloDetector(
                    self.cfg.detector_model, self.cfg.imgsz,
                    self.cfg.conf_threshold, self.cfg.iou_threshold,
                    self.cfg.target_classes)
            except Exception as e:
                self._detector_err = str(e)
        return self._detector

    def _run(self, data_dir, output_dir, conf, max_images, export_shp, export_gdb):
        with self.state.lock:
            self.state.state = "running"
            self.state.progress = 0
            self.state.total = 0
            self.state.current = ""
            self.state.logs = []
            self.state.result = None
            self.state.error = None
        self.cfg.conf_threshold = float(conf)

        # 运行日志同步落盘到输出目录（每次运行独立文件，保留历史）
        log_path = None
        log_fh = None
        try:
            os.makedirs(output_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(output_dir, f"解译日志_{ts}.log")
            log_fh = open(log_path, "a", encoding="utf-8")
            log_fh.write(f"==== 众源感知自动解译 {ts} ====\n")
            log_fh.write(f"数据源: {data_dir}\n输出目录: {output_dir}\n"
                         f"置信度阈值: {conf} 最大影像数: {max_images or '不限'} "
                         f"导出SHP: {export_shp} 导出GDB: {export_gdb}\n\n")
            log_fh.flush()
        except Exception:
            log_fh = None
            log_path = None
        with self.state.lock:
            self.state.log_file = log_path or ""

        def wlog(msg):
            """写屏幕日志与文件日志（文件失败不影响运行）"""
            with self.state.lock:
                self.state.logs.append(msg)
            if log_fh:
                try:
                    log_fh.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
                    log_fh.flush()
                except Exception:
                    pass

        det = self._get_detector()
        if det is None:
            wlog("未找到模型 %s，仅运行车道线/地理参考" % self.cfg.detector_model)

        # 预填逐文件状态表（pending → done/failed）
        try:
            from pipeline.data_loader import DataProject
            matched = DataProject.load(data_dir).matched
            if max_images:
                matched = matched[:max_images]
            files = [{"name": rec.filename, "status": "pending", "det": 0, "lane": 0}
                     for rec, _ in matched]
        except Exception:
            files = []
        with self.state.lock:
            self.state.files = files

        pipe = Pipeline(self.cfg, det)

        def cb(i, total, fname, nd, nl, ok=True):
            wlog(f"[{i}/{total}] {fname} 目标:{nd} 车道线:{nl}"
                 + ("" if ok else " [失败]"))
            with self.state.lock:
                self.state.progress = i
                self.state.total = total
                self.state.current = fname
                for f in self.state.files:
                    if f["name"] == fname and f["status"] == "pending":
                        f["status"] = "done" if ok else "failed"
                        f["det"], f["lane"] = nd, nl
                        break

        try:
            res = pipe.process_project(
                data_dir, output_dir, max_images=max_images, progress_cb=cb,
                export_shp=export_shp, export_gdb=export_gdb)
            with self.state.lock:
                self.state.state = "done"
                self.state.result = {
                    "per_session": res.per_session,
                    "change_count": res.change_count,
                    "change_path": res.change_path,
                    "output_dir": res.output_dir,
                }
            for e in res.errors:
                wlog("ERR: " + e)
            total_det = sum(v["det"] for v in res.per_session.values())
            total_lane = sum(v["lane"] for v in res.per_session.values())
            wlog(f"解译完成：{len(res.per_session)} 个批次，目标 {total_det}，"
                 f"车道线 {total_lane}，时序变化 {res.change_count}")
            wlog(f"成果目录: {res.output_dir}")
        except Exception as e:
            with self.state.lock:
                self.state.state = "error"
                self.state.error = str(e)
            wlog("ERR: 解译中断 " + str(e))
        finally:
            if log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass
