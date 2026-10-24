"""JS↔Python 桥：后台线程跑 Pipeline，前端轮询状态。"""
import base64
import os
import subprocess
import sys
import threading
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

    def start_pipeline(self, data_dir, output_dir, conf=0.35, max_images=None,
                       use_fisheye=True, export_gdb=True):
        if self.state.state == "running":
            return {"started": False, "error": "已在运行中"}
        t = threading.Thread(
            target=self._run,
            args=(data_dir, output_dir, conf, max_images, use_fisheye, export_gdb),
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

    def _run(self, data_dir, output_dir, conf, max_images, use_fisheye, export_gdb):
        with self.state.lock:
            self.state.state = "running"
            self.state.progress = 0
            self.state.total = 0
            self.state.current = ""
            self.state.logs = []
            self.state.result = None
            self.state.error = None
        self.cfg.conf_threshold = float(conf)
        det = self._get_detector()
        if det is None:
            msg = "未找到模型 %s，仅运行车道线/地理参考" % self.cfg.detector_model
            with self.state.lock:
                self.state.logs.append(msg)

        pipe = Pipeline(self.cfg, det)

        def cb(i, total, fname, nd, nl):
            with self.state.lock:
                self.state.progress = i
                self.state.total = total
                self.state.current = fname
                self.state.logs.append(f"[{i}/{total}] {fname} 目标:{nd} 车道线:{nl}")

        try:
            res = pipe.process_project(
                data_dir, output_dir, max_images=max_images,
                progress_cb=cb, export_gdb=export_gdb, use_fisheye=use_fisheye)
            with self.state.lock:
                self.state.state = "done"
                self.state.result = {
                    "per_session": res.per_session,
                    "change_count": res.change_count,
                    "change_path": res.change_path,
                    "output_dir": res.output_dir,
                }
                for e in res.errors:
                    self.state.logs.append("ERR: " + e)
        except Exception as e:
            with self.state.lock:
                self.state.state = "error"
                self.state.error = str(e)
