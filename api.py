"""JS↔Python 桥：后台线程跑 Pipeline，前端轮询状态。"""
import base64
from collections import deque
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from pipeline import coords
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
        # 日志只保留最近 5000 行用于界面回放；log_total 为累计行数，
        # 前端据此取增量（只回传尾部会在 200 行后让界面停止刷新）
        self.logs = deque(maxlen=5000)
        self.log_total = 0
        self.result = None
        self.error = None
        # 结构化错误：前端据此聚焦到具体输入框
        self.error_code = ""
        self.crs_field = ""
        self.hint = ""
        self.crs_candidates = []
        self.crs_info = {}
        self.files = []            # 逐文件状态 [{name,status,det,lane}]
        self.log_file = ""         # 本次运行日志文件路径（输出目录下）


def _record_error(st: RunState, exc: Exception) -> None:
    """把异常写入运行状态；CRS/位姿错误带错误码与字段名。"""
    with st.lock:
        st.state = "error"
        st.error = str(exc)
        st.error_code = getattr(exc, "code", "INTERNAL") or "INTERNAL"
        st.crs_field = getattr(exc, "field", "") or ""
        st.hint = getattr(exc, "hint", "") or ""
        st.crs_candidates = getattr(exc, "candidates", []) or []


def _begin_run(st: RunState) -> bool:
    """原子地开始新任务：已在运行返回 False；否则清空状态并置为 running。

    前端在 start_* 返回后立即轮询，因此 running 必须在启动线程前写入，
    不能等后台线程自己设置——否则首次轮询可能读到上一轮的 done。
    """
    with st.lock:
        if st.state == "running":
            return False
        st.state, st.progress, st.total = "running", 0, 0
        st.current = ""
        st.logs.clear()
        st.log_total = 0
        st.result = st.error = None
        st.error_code = st.crs_field = st.hint = ""
        st.crs_candidates = []
        st.crs_info = {}
        st.log_file = ""
        st.files = []
        return True


def _param_error_payload(message: str, *, field: str, hint: str = "") -> dict:
    """普通参数错误：复用 crs_field 让前端聚焦到对应输入框。"""
    return {"started": False, "error": message, "error_code": "PARAM_INVALID",
            "crs_field": field, "hint": hint}


def _status_payload(st: RunState) -> dict:
    return {
        "state": st.state,
        "progress": st.progress,
        "total": st.total,
        "current": st.current,
        "logs": list(st.logs)[-200:],
        "log_seq": st.log_total,
        "result": st.result,
        "error": st.error,
        "error_code": st.error_code,
        "crs_field": st.crs_field,
        "hint": st.hint,
        "crs_candidates": st.crs_candidates,
        "crs_info": st.crs_info,
        "log_file": st.log_file,
    }


def _crs_error_payload(exc: coords.CRSError, *, candidates=None) -> dict:
    payload = exc.to_dict()
    payload["started"] = False
    if candidates:
        payload["crs_candidates"] = candidates
    return payload


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
        """弹出系统文件选择对话框。file_types: [".tif", ".tiff"]

        pywebview 过滤器格式硬校验：'描述 (*.a;*.b)'，描述仅允许字母/数字/汉字/空格，
        且描述与括号之间不能有空格，否则抛 'not a valid file filter'。
        """
        import webview
        try:
            if not webview.windows:
                return {"ok": False, "path": "", "error": "窗口尚未就绪"}
            exts = [e if e.startswith(".") else "." + e for e in (file_types or [])]
            patterns = []
            if exts:
                patterns.append("文件 (*{})".format(";*".join(exts)))
            patterns.append("所有文件 (*.*)")
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
                       export_shp=True, export_gdb=False, pose_crs=None,
                       output_crs=None):
        """车载影像提取：输出 SHP（export_gdb 默认关闭，按需求只交付 SHP）。

        位姿经纬度默认为 WGS84；输出默认跟随位姿坐标系。CRS 字符串在启动前
        同步校验，非法值直接返回 CRS_INVALID，不进入后台线程。
        """
        if self.state.state == "running":
            return {"started": False, "error": "已在运行中"}
        try:
            pose_crs_obj = coords.parse_crs(pose_crs, field="pose_crs")
            coords.parse_crs(output_crs, field="output_crs")
        except coords.CRSError as e:
            return _crs_error_payload(e)
        # HDI 的 col12/13 固定是经纬度（度）。若把投影坐标系当作位姿坐标系，
        # pyproj 会把 114.3/30.6 当成米，成果会静默偏移数百万米。
        if pose_crs_obj is not None and not pose_crs_obj.is_geographic:
            return _crs_error_payload(coords.CRSInvalidError(
                f"位姿文件使用经纬度列，位姿坐标系不能是投影坐标系"
                f"「{coords.describe(pose_crs_obj)}」。",
                field="pose_crs",
                hint="请填写地理坐标系（如 EPSG:4326 / EPSG:4490）；"
                     "需要投影成果请在“输出坐标系”中填写。"))
        # 输入框清空时 pywebview 会传 null；按默认值处理，而不是让后台
        # 线程在 float(None) 处崩溃并永久停在 running。
        if conf is None or (isinstance(conf, str) and not conf.strip()):
            conf_val = 0.20
        else:
            try:
                conf_val = float(conf)
            except (TypeError, ValueError):
                return _param_error_payload(
                    f"置信度阈值不是有效数字：{conf!r}", field="conf",
                    hint="请输入 0–1 之间的数值，例如 0.20。")
        if not math.isfinite(conf_val) or not (0.0 <= conf_val <= 1.0):
            return _param_error_payload(
                f"置信度阈值超出范围：{conf}（应在 0–1 之间）", field="conf",
                hint="请输入 0–1 之间的数值，例如 0.20。")
        if not _begin_run(self.state):
            return {"started": False, "error": "已在运行中"}
        t = threading.Thread(
            target=self._guarded,
            args=(self.state, self._run, data_dir, output_dir, conf_val,
                  max_images, export_shp, export_gdb, pose_crs, output_crs),
            daemon=True)
        try:
            t.start()
        except Exception as e:
            _record_error(self.state, e)
            return {"started": False, "error": f"无法启动后台任务：{e}",
                    "error_code": "INTERNAL"}
        return {"started": True}

    def get_status(self):
        with self.state.lock:
            payload = _status_payload(self.state)
            payload["files"] = self.state.files
            return payload

    def preview_image(self, path):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            # 预览图既有 JPEG（逐帧）也有 PNG（正射提取），按文件头返回 MIME
            mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(raw).decode()
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
    @staticmethod
    def _resolve_dom_tif(path):
        """正射影像支持「选目录」：目录内递归找 .tif/.tiff，取像素规模最大的一张。
        返回 (tif_path, 提示语)；找不到返回 (None, 错误语)。"""
        if not path:
            return None, "未选择正射影像目录"
        if os.path.isfile(path):
            return path, ""
        if not os.path.isdir(path):
            return None, "目录不存在"
        cands = []
        for root, _dirs, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith((".tif", ".tiff")):
                    fp = os.path.join(root, fn)
                    try:
                        size = os.path.getsize(fp)
                    except OSError:
                        size = 0
                    # 按像素总量而非文件大小选择（压缩率会让二者不一致）；
                    # 读不了元数据时退回文件大小排序
                    px = 0
                    try:
                        import rasterio
                        with rasterio.open(fp) as ds:
                            px = int(ds.width) * int(ds.height)
                    except Exception:
                        pass
                    cands.append((px, size, fp))
        if not cands:
            return None, "目录中未找到 .tif/.tiff 影像"
        cands.sort(reverse=True)
        tif = cands[0][2]
        tip = ""
        if len(cands) > 1:
            tip = (f"目录内发现 {len(cands)} 个 TIF，取其中像素规模最大的 "
                   f"{os.path.basename(tif)}（其余未处理）")
        return tif, tip

    def start_dom(self, tif_path, output_dir, stock_gdb=None, min_len=2.0,
                  use_stock_clip=True, use_corridor=True,
                  input_crs=None, stock_crs=None, output_crs=None):
        """正射提取：启动前同步校验影像/存量 CRS，缺失时立即返回结构化错误。"""
        if self.dom_state.state == "running":
            return {"started": False, "error": "正射解译正在运行中"}
        tif_path, tip = self._resolve_dom_tif(tif_path)
        if not tif_path:
            return {"started": False, "error": tip or "正射影像不存在"}
        # 存量 GDB 同时服务于“范围裁剪”和“道路走廊”两个独立开关：
        # 只关掉裁剪时仍必须保留存量，否则走廊会被静默禁用。
        stock = stock_gdb if (stock_gdb and (use_stock_clip or use_corridor)) else None
        if stock and not os.path.exists(stock):
            return {"started": False, "error": "存量 GDB 不存在",
                    "error_code": "INPUT_NOT_FOUND", "crs_field": "stock_gdb"}
        try:
            coords.parse_crs(output_crs, field="output_crs")
            import rasterio
            with rasterio.open(tif_path) as ds:
                try:
                    src_crs, _origin, _note = coords.resolve_raster_crs(
                        ds, input_crs, field="input_crs")
                except coords.CRSMissingError as e:
                    return _crs_error_payload(
                        e, candidates=coords.suggest_crs_candidates(ds.bounds))
                if stock:
                    # 存量缺 CRS 时按邻近矢量 + 影像范围自动判断；
                    # 只有证据不足才返回 CRS_REQUIRED 让用户填写。
                    coords.inspect_gdb_crs(
                        stock, stock_crs, field="stock_crs",
                        infer_roots=coords.inference_roots(tif_path, stock),
                        anchor_crs=src_crs, anchor_bounds=ds.bounds)
        except coords.CRSError as e:
            return _crs_error_payload(e)
        except Exception as e:
            return {"started": False, "error": f"输入预检失败：{e}",
                    "error_code": "INTERNAL"}
        if not _begin_run(self.dom_state):
            return {"started": False, "error": "正射解译正在运行中"}
        t = threading.Thread(target=self._guarded,
                             args=(self.dom_state, self._run_dom, tif_path,
                                   output_dir, stock, min_len, use_stock_clip,
                                   use_corridor, tip, input_crs, stock_crs,
                                   output_crs),
                             daemon=True)
        try:
            t.start()
        except Exception as e:
            _record_error(self.dom_state, e)
            return {"started": False, "error": f"无法启动后台任务：{e}",
                    "error_code": "INTERNAL"}
        return {"started": True, "tif": tif_path, "note": tip}

    def get_dom_status(self):
        with self.dom_state.lock:
            return _status_payload(self.dom_state)

    def _run_dom(self, tif_path, output_dir, stock_gdb, min_len, use_stock_clip,
                 use_corridor=True, note="", input_crs=None, stock_crs=None,
                 output_crs=None):
        st = self.dom_state
        stock = stock_gdb if (stock_gdb and os.path.exists(stock_gdb)) else None
        clip_active = bool(use_stock_clip and stock)
        corridor_active = bool(use_corridor and stock)
        log_fh = self._open_job_log(st, output_dir, "正射影像提取",
                                    f"影像: {tif_path}\n输出: {output_dir}\n"
                                    f"存量裁剪: {clip_active} 最短标线: {min_len}m "
                                    f"走廊约束: {corridor_active}\n"
                                    f"影像坐标系: {input_crs or '（跟随文件）'} "
                                    f"存量坐标系: {stock_crs or '（跟随文件）'} "
                                    f"输出坐标系: {output_crs or '（跟随影像）'}"
                                    + (f"\n{note}" if note else ""))
        wlog = self._wlog(st, log_fh)
        wlog(f"影像：{tif_path}")
        if note:
            wlog(note)

        def cb(i, total, msg):
            with st.lock:
                st.progress, st.total = i, total
                st.current = msg or ""

        try:
            from pipeline.dom_extractor import DomExtractor
            extractor = DomExtractor(min_len=float(min_len))
            res = extractor.run(tif_path, output_dir, stock_gdb=stock,
                                wlog=wlog, progress_cb=cb,
                                use_stock_clip=use_stock_clip,
                                use_corridor=corridor_active,
                                input_crs=input_crs, stock_crs=stock_crs,
                                output_crs=output_crs)
            with st.lock:
                st.state = "done"
                st.result = res
                st.crs_info = {k: res.get(k) for k in
                               ("source_crs", "target_crs", "working_crs",
                                "source_origin", "target_origin")}
        except Exception as e:
            _record_error(st, e)
            wlog("ERR: 正射提取中断 " + str(e))
        finally:
            if log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass

    # ---- 变化比对 ----
    def start_compare(self, new_gdb, stock_gdb, dom_tif=None,
                      new_crs=None, stock_crs=None, dom_crs=None,
                      output_crs=None):
        """变化比对：无需选择输出目录，自动写到存量 GDB 同级的 变化比对_时间戳/。

        启动前同步检查三个输入的坐标系；DOM 仅作预览底图，缺 CRS 只告警不阻断。
        """
        if self.cmp_state.state == "running":
            return {"started": False, "error": "比对正在运行中"}
        if not new_gdb or not os.path.exists(new_gdb):
            return {"started": False, "error": "新成果 GDB 不存在",
                    "error_code": "INPUT_NOT_FOUND", "crs_field": "new_gdb"}
        if not stock_gdb or not os.path.exists(stock_gdb):
            return {"started": False, "error": "存量 GDB 不存在",
                    "error_code": "INPUT_NOT_FOUND", "crs_field": "stock_gdb"}
        dom_note = ""
        anchor_crs = anchor_bounds = None
        try:
            coords.parse_crs(output_crs, field="output_crs")
            # 先解析底图，作为“矢量缺 CRS”自动推断的锚点；底图缺失只影响
            # 预览与推断，不阻断比对。
            if dom_tif:
                import rasterio
                try:
                    with rasterio.open(dom_tif) as ds:
                        anchor_crs, _origin, _note = coords.resolve_raster_crs(
                            ds, dom_crs, field="dom_crs")
                        anchor_bounds = ds.bounds
                except coords.CRSMissingError as e:
                    dom_note = f"比对预览将跳过：{e}"
                except coords.CRSInvalidError:
                    raise
                except Exception as e:
                    dom_note = f"比对预览将跳过：无法打开底图（{e}）"
            infer_roots = coords.inference_roots(new_gdb, stock_gdb, dom_tif)
            coords.inspect_gdb_crs(
                new_gdb, new_crs, field="new_crs", infer_roots=infer_roots,
                anchor_crs=anchor_crs, anchor_bounds=anchor_bounds)
            coords.inspect_gdb_crs(
                stock_gdb, stock_crs, field="stock_crs", infer_roots=infer_roots,
                anchor_crs=anchor_crs, anchor_bounds=anchor_bounds)
        except coords.CRSError as e:
            return _crs_error_payload(e)
        except Exception as e:
            return {"started": False, "error": f"输入预检失败：{e}",
                    "error_code": "INTERNAL"}
        parent = os.path.dirname(os.path.abspath(stock_gdb)) or os.path.expanduser("~")
        out_dir = os.path.join(parent, "变化比对_" + time.strftime("%Y%m%d_%H%M%S"))
        if not _begin_run(self.cmp_state):
            return {"started": False, "error": "比对正在运行中"}
        t = threading.Thread(target=self._guarded,
                             args=(self.cmp_state, self._run_compare, new_gdb,
                                   stock_gdb, out_dir, dom_tif, new_crs,
                                   stock_crs, dom_crs, output_crs, dom_note),
                             daemon=True)
        try:
            t.start()
        except Exception as e:
            _record_error(self.cmp_state, e)
            return {"started": False, "error": f"无法启动后台任务：{e}",
                    "error_code": "INTERNAL"}
        return {"started": True, "output_dir": out_dir, "note": dom_note}

    def get_cmp_status(self):
        with self.cmp_state.lock:
            return _status_payload(self.cmp_state)

    def _run_compare(self, new_gdb, stock_gdb, output_dir, dom_tif,
                     new_crs=None, stock_crs=None, dom_crs=None,
                     output_crs=None, note=""):
        st = self.cmp_state
        log_fh = self._open_job_log(st, output_dir, "变化比对",
                                    f"新成果: {new_gdb}\n存量: {stock_gdb}\n"
                                    f"输出: {output_dir}（自动生成，无需选择）\n"
                                    f"新成果坐标系: {new_crs or '（跟随文件）'} "
                                    f"存量坐标系: {stock_crs or '（跟随文件）'} "
                                    f"底图坐标系: {dom_crs or '（跟随文件）'} "
                                    f"输出坐标系: {output_crs or '（跟随新成果）'}"
                                    + (f"\n{note}" if note else ""))
        wlog = self._wlog(st, log_fh)
        if note:
            wlog(note)

        def cb(i, total, msg):
            with st.lock:
                st.progress, st.total = i, total
                st.current = msg or ""

        try:
            from pipeline.comparator import Comparator
            res = Comparator().run(new_gdb, stock_gdb, output_dir,
                                   dom_tif=dom_tif or None, wlog=wlog, progress_cb=cb,
                                   new_crs=new_crs, stock_crs=stock_crs,
                                   dom_crs=dom_crs, output_crs=output_crs)
            with st.lock:
                st.state = "done"
                st.result = res
                st.crs_info = {k: res.get(k) for k in
                               ("source_crs", "target_crs", "working_crs",
                                "source_origin", "target_origin")}
        except Exception as e:
            _record_error(st, e)
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
            log_path = os.path.join(output_dir, f"运行日志_{ts}.log")
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
                state.log_total += 1
            if log_fh:
                try:
                    log_fh.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
                    log_fh.flush()
                except Exception:
                    pass
        return wlog

    def _guarded(self, st, fn, *args):
        """所有后台线程的统一入口：异常与意外返回都必须写入终态。

        否则一条 import/DLL 错误就能让界面永久停在 running，且没有可见错误。
        """
        try:
            fn(*args)
        except Exception as e:
            with st.lock:
                terminal = st.state in ("done", "error")
            if terminal:
                return
            _record_error(st, e)
            try:
                self._wlog(st, None)("ERR: 任务中断 " + str(e))
            except Exception:
                pass
            return
        with st.lock:
            still_running = st.state == "running"
        if still_running:
            err = RuntimeError("任务线程意外结束，未写入完成状态")
            _record_error(st, err)
            try:
                self._wlog(st, None)("ERR: " + str(err))
            except Exception:
                pass

    # ---- 内部 ----
    def _get_detector(self):
        if self._detector is not None:
            # 阈值可在两次运行之间被前端修改，缓存的检测器需要同步，
            # 否则改阈值要重启程序才生效
            self._detector.conf = float(self.cfg.conf_threshold)
            self._detector.iou = float(self.cfg.iou_threshold)
            return self._detector
        if not Path(self.cfg.detector_model).exists():
            self._detector_err = f"模型不存在：{self.cfg.detector_model}"
            return None
        try:
            self._detector = YoloDetector(
                self.cfg.detector_model, self.cfg.imgsz,
                self.cfg.conf_threshold, self.cfg.iou_threshold,
                self.cfg.target_classes)
            self._detector_err = None
        except Exception as e:
            # 不永久缓存错误：模型/依赖恢复后下次运行会自动重试
            self._detector_err = str(e)
        return self._detector

    def _run(self, data_dir, output_dir, conf, max_images, export_shp, export_gdb,
             pose_crs=None, output_crs=None):
        self.cfg.conf_threshold = float(conf)

        # 运行日志同步落盘到输出目录（每次运行独立文件，保留历史）
        log_path = None
        log_fh = None
        try:
            os.makedirs(output_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(output_dir, f"运行日志_{ts}.log")
            log_fh = open(log_path, "a", encoding="utf-8")
            log_fh.write(f"==== 车载影像提取 {ts} ====\n")
            log_fh.write(f"数据源: {data_dir}\n输出目录: {output_dir}\n"
                         f"置信度阈值: {conf} 最大影像数: {max_images or '不限'} "
                         f"导出: SHP\n"
                         f"位姿坐标系: {pose_crs or '（默认 EPSG:4326）'} "
                         f"输出坐标系: {output_crs or '（跟随位姿）'}\n\n")
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
                self.state.log_total += 1
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
                export_shp=export_shp, export_gdb=export_gdb,
                pose_crs=pose_crs, output_crs=output_crs, wlog=wlog)
            with self.state.lock:
                self.state.state = "done"
                self.state.result = {
                    "per_session": res.per_session,
                    "change_count": res.change_count,
                    "change_path": res.change_path,
                    "output_dir": res.output_dir,
                    "crs": res.crs,
                    "report_path": res.report_path,
                }
                self.state.crs_info = dict(res.crs)
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
            wlog(f"成果目录: {res.output_dir}（preview/ 标注预览 · shp/ 矢量 SHP）")
        except Exception as e:
            _record_error(self.state, e)
            wlog("ERR: 解译中断 " + str(e))
        finally:
            if log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass
