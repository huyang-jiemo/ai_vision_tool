"""JS↔Python 桥：后台线程跑 Pipeline，前端轮询状态。"""
import base64
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from pipeline.config import BUSINESS_CATEGORIES, PipelineConfig
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
        self.dom_state = RunState()   # 正射影像提取
        self.cmp_state = RunState()   # 变化比对
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

    def pick_file(self, title="选择文件", file_types=None):
        """弹出系统文件选择对话框。file_types: [".tif", ".tiff"]"""
        import webview
        try:
            if not webview.windows:
                return {"ok": False, "path": "", "error": "窗口尚未就绪"}
            ft = list(file_types or [])
            patterns = ["*{} (*{})".format(e, e) for e in ft] + ["所有文件 (*.*)"]
            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=patterns)
            if result:
                return {"ok": True, "path": result[0]}
            return {"ok": False, "path": ""}
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

    def read_text_file(self, path, limit=200000):
        """读取 JSON 报告等文本文件，供前端展示。"""
        try:
            if not path or not os.path.isfile(path):
                return {"ok": False, "error": "文件不存在"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return {"ok": True, "text": f.read(limit)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- 正射影像提取 ----
    def start_dom(self, tif_path, output_dir, stock_gdb=None, min_len=2.0,
                  use_stock_clip=True, use_corridor=True):
        if self.dom_state.state == "running":
            return {"started": False, "error": "正射解译正在运行中"}
        if not tif_path or not os.path.isfile(tif_path):
            return {"started": False, "error": "正射影像文件不存在"}
        t = threading.Thread(target=self._run_dom,
                             args=(tif_path, output_dir, stock_gdb, min_len,
                                   use_stock_clip, use_corridor),
                             daemon=True)
        t.start()
        return {"started": True}

    def get_dom_status(self):
        with self.dom_state.lock:
            return {
                "state": self.dom_state.state,
                "progress": self.dom_state.progress,
                "total": self.dom_state.total,
                "logs": self.dom_state.logs[-200:],
                "result": self.dom_state.result,
                "error": self.dom_state.error,
                "log_file": self.dom_state.log_file,
            }

    def _run_dom(self, tif_path, output_dir, stock_gdb, min_len, use_stock_clip,
                 use_corridor=True):
        from pipeline.dom_extractor import DomExtractor
        st = self.dom_state
        with st.lock:
            st.state, st.progress, st.total = "running", 0, 0
            st.logs, st.result, st.error = [], None, None
        log_fh = self._open_job_log(st, output_dir, "正射影像提取",
                                    f"影像: {tif_path}\n输出: {output_dir}\n"
                                    f"存量裁剪: {use_stock_clip} 最短标线: {min_len}m "
                                    f"走廊约束: {use_corridor}")
        wlog = self._wlog(st, log_fh)

        def cb(i, total, msg):
            with st.lock:
                st.progress, st.total = i, total

        try:
            extractor = DomExtractor(min_len=float(min_len))
            stock = stock_gdb if (use_stock_clip and stock_gdb
                                  and os.path.exists(stock_gdb)) else None
            res = extractor.run(tif_path, output_dir, stock_gdb=stock,
                                wlog=wlog, progress_cb=cb,
                                use_corridor=use_corridor and stock is not None)
            with st.lock:
                st.state = "done"
                st.result = res
        except Exception as e:
            with st.lock:
                st.state = "error"
                st.error = str(e)
            wlog("ERR: 正射提取中断 " + str(e))
        finally:
            if log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass

    # ---- 变化比对 ----
    def start_compare(self, new_gdb, stock_gdb, output_dir, dom_tif=None):
        if self.cmp_state.state == "running":
            return {"started": False, "error": "比对正在运行中"}
        if not new_gdb or not os.path.exists(new_gdb):
            return {"started": False, "error": "新成果 GDB 不存在"}
        if not stock_gdb or not os.path.exists(stock_gdb):
            return {"started": False, "error": "存量 GDB 不存在"}
        t = threading.Thread(target=self._run_compare,
                             args=(new_gdb, stock_gdb, output_dir, dom_tif),
                             daemon=True)
        t.start()
        return {"started": True}

    def get_cmp_status(self):
        with self.cmp_state.lock:
            return {
                "state": self.cmp_state.state,
                "progress": self.cmp_state.progress,
                "total": self.cmp_state.total,
                "logs": self.cmp_state.logs[-200:],
                "result": self.cmp_state.result,
                "error": self.cmp_state.error,
                "log_file": self.cmp_state.log_file,
            }

    def _run_compare(self, new_gdb, stock_gdb, output_dir, dom_tif):
        from pipeline.comparator import Comparator
        st = self.cmp_state
        with st.lock:
            st.state, st.progress, st.total = "running", 0, 0
            st.logs, st.result, st.error = [], None, None
        log_fh = self._open_job_log(st, output_dir, "变化比对",
                                    f"新成果: {new_gdb}\n存量: {stock_gdb}\n输出: {output_dir}")
        wlog = self._wlog(st, log_fh)

        def cb(i, total, msg):
            with st.lock:
                st.progress, st.total = i, total

        try:
            res = Comparator().run(new_gdb, stock_gdb, output_dir,
                                   dom_tif=dom_tif or None, wlog=wlog, progress_cb=cb)
            with st.lock:
                st.state = "done"
                st.result = res
        except Exception as e:
            with st.lock:
                st.state = "error"
                st.error = str(e)
            wlog("ERR: 比对中断 " + str(e))
        finally:
            if log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass

    # ---- 后台任务通用 ----
    def _open_job_log(self, state, output_dir, title, header):
        log_fh = None
        try:
            os.makedirs(output_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(output_dir, f"{ts}.log")
            log_fh = open(log_path, "a", encoding="utf-8")
            log_fh.write(f"==== {title} {ts} ====\n{header}\n\n")
            log_fh.flush()
            with state.lock:
                state.log_file = log_path
        except Exception:
            log_fh = None
            with state.lock:
                state.log_file = ""
        return log_fh

    def _wlog(self, state, log_fh):
        def wlog(msg):
            with state.lock:
                state.logs.append(msg)
            if log_fh:
                try:
                    log_fh.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
                    log_fh.flush()
                except Exception:
                    pass
        return wlog

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
            log_path = os.path.join(output_dir, f"{ts}.log")
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
            files = [{"name": rec.filename, "session": rec.session, "status": "pending",
                      "det": 0, "lane": 0, "cats": {}}
                     for rec, _ in matched]
        except Exception:
            files = []
        with self.state.lock:
            self.state.files = files

        pipe = Pipeline(self.cfg, det)

        def cb(i, total, fname, nd, nl, ok=True, cats=None, session=""):
            if ok:
                detail = " ".join(f"{c}:{(cats or {}).get(c, 0)}"
                                  for c in BUSINESS_CATEGORIES)
                wlog(f"[{i}/{total}] {fname} 目标:{nd}（{detail}） 车道线:{nl}")
            else:
                wlog(f"[{i}/{total}] {fname} 解译失败")
            with self.state.lock:
                self.state.progress = i
                self.state.total = total
                self.state.current = fname
                for f in self.state.files:
                    if (f["name"] == fname and f.get("session") == session
                            and f["status"] == "pending"):
                        f["status"] = "done" if ok else "failed"
                        f["det"], f["lane"] = nd, nl
                        f["cats"] = cats or {}
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
            cat_totals = {}
            for v in res.per_session.values():
                for c, n in v.get("cats", {}).items():
                    cat_totals[c] = cat_totals.get(c, 0) + n
            detail = " ".join(f"{c}:{cat_totals.get(c, 0)}"
                              for c in BUSINESS_CATEGORIES)
            wlog(f"解译完成：{len(res.per_session)} 个批次，目标 {total_det}（{detail}），"
                 f"车道线 {total_lane}，时序变化 {res.change_count}")
            wlog(f"成果目录: {res.output_dir}（preview/ 标注预览 · shp/ 矢量 · gdb/ 地理数据库）")
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
