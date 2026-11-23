"""pywebview 桌面入口：双击打开即用，零终端依赖（打包后）。"""
import os
import sys
import time

import webview

from api import Api


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _utf8_stdio() -> None:
    """冻结的 windowed 程序 stdout 可能是 cp1252/cp936，打印中文会
    UnicodeEncodeError；统一改为 UTF-8，编码不了的字符替换而不是抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _selftest_mode() -> str:
    """自检模式：环境变量 AVT_SELFTEST 或命令行 --selftest=<mode>。

    命令行参数用于启动器/CI：不依赖环境变量是否被正确继承。
    """
    mode = os.environ.get("AVT_SELFTEST", "").strip()
    if mode:
        return mode
    for arg in sys.argv[1:]:
        if arg.startswith("--selftest="):
            return arg.split("=", 1)[1].strip()
    return ""


def _write_boot_marker(mode: str) -> None:
    """自检时先落一个标记：区分「程序没启动到 main」和「自检逻辑失败」。"""
    import tempfile
    try:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        d = os.path.join(base, "AIVisionTool")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "启动标记.txt"), "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
                    f"mode={mode} argv={sys.argv[1:]!r}\n")
    except Exception:
        pass


def frozen_selftest():
    """冻结环境自检（发布验证用，正常用户不触发）：
    AVT_SELFTEST=gdb 或 --selftest=gdb 验证 GDB 导出依赖；
    =rasterio 验证正射影像读取依赖；=dom 验证标线提取；
    =deps 验证 pyproj/geopandas/pyogrio 依赖链；=crs 输出坐标系诊断
    （AVT_SELFTEST_TIF 指定影像；未指定时合成 EPSG:3857 图；
     AVT_SELFTEST_INPUT_CRS 可指定影像缺失 CRS 时的覆盖值）。"""
    mode = _selftest_mode()
    if not mode:
        return False
    try:
        return _run_selftest(mode)
    except Exception:
        # windowed 程序里异常会弹窗阻塞，CI/客户排障都看不到；
        # 统一落盘 traceback 后正常退出
        import traceback
        _write_selftest_error(mode, traceback.format_exc())
        return True


def _write_selftest_error(mode: str, tb: str) -> None:
    import tempfile
    try:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        d = os.path.join(base, "AIVisionTool")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "自检错误.txt"), "w", encoding="utf-8") as f:
            f.write(f"mode={mode}\n{tb}\n")
    except Exception:
        pass


def _run_selftest(mode: str):
    if mode == "gdb":
        import tempfile
        from pyproj import CRS
        from pipeline.exporter import export_points_gdb
        feats = [{"x": 500000.0, "y": 3000000.0, "class_id": "2", "class_cn": "自检",
                  "session": "S", "image": "I", "dist": 0.0, "score": 0.9}]
        p = os.path.join(tempfile.mkdtemp(prefix="avt_selftest_"), "t.gdb")
        ok = export_points_gdb(p, "detections", feats, crs=CRS.from_epsg(4547))
        print("GDB_SELFTEST:", "PASS" if ok and os.path.exists(p) else "FAIL")
        return True
    if mode == "rasterio":
        try:
            import numpy as np
            import rasterio
            from rasterio.transform import from_origin
            with rasterio.MemoryFile() as mf:
                with mf.open(driver="GTiff", height=10, width=10, count=1,
                             dtype="uint8", crs="EPSG:3857",
                             transform=from_origin(0, 10, 1, 1)) as ds:
                    ds.write(np.zeros((1, 10, 10), dtype="uint8"))
            print("RASTERIO_SELFTEST: PASS")
        except Exception as e:
            print("RASTERIO_SELFTEST: FAIL", e)
        return True
    if mode == "deps":
        # 验证 GIS 依赖链（pyproj → geopandas/pyogrio）在冻结环境可用：
        # 曾因 libproj 多副本冲突导致 pyproj 加载失败、正射提取直接中断。
        # 除 CRS 解析外，必须实际建立 Transformer（正射提取失败点就在这里）。
        try:
            import geopandas
            import pyogrio
            from pyproj import CRS
            from pipeline import coords, crs_plan
            crs = CRS.from_epsg(3857)
            gdf = geopandas.GeoDataFrame({"a": [1]}, geometry=geopandas.points_from_xy([0], [0]),
                                         crs=crs)
            ok = gdf.crs is not None and gdf.crs.to_epsg() == 3857
            # 任意基准的局部 TM ↔ Web Mercator 往返（不依赖任何写死的本地坐标）
            local = crs_plan.local_tm_crs(CRS.from_epsg(4326), 114.3)
            fwd = coords.build_transformer(local, crs)
            back = coords.build_transformer(crs, local)
            x, y = back.transform(*fwd.transform(800000.0, 385000.0))
            ok_tf = abs(x - 800000.0) < 1e-3 and abs(y - 385000.0) < 1e-3
            # 同 CRS 应识别为「无需转换」
            ok_same = coords.build_transformer(CRS.from_epsg(4326),
                                               CRS.from_epsg(4326)) is None
            # working 选择：3857 尺度偏大 → 局部 TM；4547 尺度/收敛角合格 → 直接用目标
            _w1, r1 = crs_plan.working_crs_for(crs, 114.3, 30.6)
            _w2, r2 = crs_plan.working_crs_for(CRS.from_epsg(4547), 114.3, 30.6)
            ok_work = (r1 == "local_tm" and r2 == "target")
            ok = ok and ok_tf and ok_same and ok_work
            print("DEPS_SELFTEST:", "PASS" if ok else "FAIL",
                  f"(gpd={geopandas.__version__}, pyogrio={pyogrio.__version__}, "
                  f"转换往返={'OK' if ok_tf else 'FAIL'}, "
                  f"同投影识别={'OK' if ok_same else 'FAIL'}, "
                  f"工作系选择={'OK' if ok_work else 'FAIL'}, "
                  f"{coords.runtime_info()})")
        except Exception as e:
            print("DEPS_SELFTEST: FAIL", type(e).__name__, e)
        return True
    if mode == "crs":
        # 坐标系诊断（客户机器排障用）：
        #   设置 AVT_SELFTEST=crs、AVT_SELFTEST_TIF=<正射影像或目录> 后运行程序，
        #   结果同时写入 %LOCALAPPDATA%\AIVisionTool\坐标系诊断.txt 供回传。
        import tempfile

        base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
        diag_dir = os.path.join(base, 'AIVisionTool')
        diag_path = os.path.join(diag_dir, '坐标系诊断.txt')
        lines = []

        def flush():
            # 每步立即落盘：即使后续导入崩溃/卡住，也能看到最后一步
            try:
                os.makedirs(diag_dir, exist_ok=True)
                with open(diag_path, 'w', encoding='utf-8') as f:
                    f.write("\n".join(lines) + "\n")
            except Exception:
                pass

        try:
            lines.append("stage=start")
            flush()
            import pyproj
            lines.append(f"stage=pyproj ok ({pyproj.__version__})")
            flush()
            from pipeline import coords, crs_plan
            lines.append("stage=pipeline ok")
            flush()
            import rasterio
            lines.append(f"rasterio={rasterio.__version__} GDAL={rasterio.__gdal_version__}")
            lines.append(coords.runtime_info())
            flush()
            src_input = os.environ.get("AVT_SELFTEST_TIF", "")
            input_crs = os.environ.get("AVT_SELFTEST_INPUT_CRS", "").strip() or None
            tif, tip = Api._resolve_dom_tif(src_input)
            lines.append(f"影像选择：{tif or '未找到'} {tip}".rstrip())
            if not tif:
                # CI/客户机器通常没有现成 TIF：合成一张带 CRS 的小图，
                # 让自检真正覆盖 rasterio 读取 → CRS 解析 → 工作系选择 → 往返转换。
                try:
                    import numpy as np
                    from rasterio.transform import from_origin
                    synth_dir = tempfile.mkdtemp(prefix="avt_crs_")
                    tif = os.path.join(synth_dir, "synthetic.tif")
                    with rasterio.open(
                            tif, "w", driver="GTiff", height=32, width=32, count=1,
                            dtype="uint8", crs="EPSG:3857",
                            transform=from_origin(12730000, 3577000, 0.1, 0.1)) as sds:
                        sds.write(np.zeros((1, 32, 32), dtype="uint8"))
                    lines.append("未提供 AVT_SELFTEST_TIF，使用合成 EPSG:3857 影像")
                    flush()
                except Exception as e:
                    tif = None
                    lines.append(f"合成影像失败：{e}")
            if tif:
                with rasterio.open(tif) as dom:
                    lines.append(f"尺寸={dom.width}x{dom.height} 波段={dom.count}")
                    lines.append(f"范围={tuple(round(v, 2) for v in dom.bounds)}")
                    lines.append(f"文件 CRS={dom.crs}")
                    esri = coords.dataset_esri_wkt(dom)
                    lines.append(f"ESRI PE String={'有' if esri else '无'}")
                    try:
                        src, origin, note = coords.resolve_raster_crs(
                            dom, input_crs, field="input_crs")
                    except coords.CRSMissingError as e:
                        lines.append(f"坐标系=缺失：{e}")
                        cands = coords.suggest_crs_candidates(dom.bounds)
                        if cands:
                            lines.append("候选=" + "、".join(
                                f"EPSG:{c['epsg']}（{c['reason']}）" for c in cands))
                        src = None
                    if src is not None:
                        lines.append(f"坐标系={coords.describe(src)} 来源={origin}")
                        if note:
                            lines.append(f"说明={note}")
                        plan = crs_plan.plan_for_bounds(
                            src, None, dom.bounds, source_origin=origin)
                        lines.append(
                            f"工作坐标系={coords.crs_label(plan.working_crs)} "
                            f"原因={plan.working_reason}")
                        lines.append(f"working_reason={plan.working_reason}")
                        try:
                            tf = plan.to_working()
                            lines.append("转换=" + ("无需转换" if tf is None else "可建立"))
                            if tf is None:
                                lines.append("roundtrip=skip")
                            else:
                                left, bottom, right, top = dom.bounds
                                cx = (left + right) / 2.0
                                cy = (bottom + top) / 2.0
                                wx, wy = tf.transform(cx, cy)
                                bx, by = plan.to_source().transform(wx, wy)
                                lines.append(f"中心往返偏差={abs(bx - cx):.6g},"
                                             f"{abs(by - cy):.6g}")
                                ok_rt = abs(bx - cx) <= 1e-6 and abs(by - cy) <= 1e-6
                                lines.append("roundtrip=" + ("ok" if ok_rt else "fail"))
                        except Exception as e:
                            lines.append(f"转换=失败：{e}")
                            lines.append("roundtrip=fail")
            lines.append("stage=done")
        except Exception as e:
            import traceback
            lines.append(f"诊断异常：{type(e).__name__}: {e}")
            lines.append(traceback.format_exc())
        text = "\n".join(lines)
        # 先落盘再打印：打印失败（编码/无控制台）不应影响诊断结果
        flush()
        try:
            print("CRS_SELFTEST:\n" + text)
        except Exception:
            pass
        return True
    if mode == "dom":
        # 验证正射影像（BigTIFF）读取与标线提取在打包环境下可用
        try:
            import numpy as np
            import rasterio
            from rasterio.windows import Window
            from pipeline.dom_extractor import DomExtractor
            tif = os.environ.get("AVT_SELFTEST_TIF", "")
            if not tif or not os.path.exists(tif):
                print("DOM_SELFTEST: SKIP (no tif)")
                return True
            dom = rasterio.open(tif)
            cx, cy = dom.width // 2, dom.height // 2
            win = Window(max(0, cx - 1024), max(0, cy - 1024), 2048, 2048)
            img = dom.read([1, 2, 3], window=win).transpose(1, 2, 0)
            wm, ym, road = DomExtractor.masks(img)
            n = int(wm.sum() + ym.sum() + road.sum())
            print("DOM_SELFTEST:", "PASS" if n > 0 else "FAIL", f"(mask_px={n}, size={dom.width}x{dom.height})")
        except Exception as e:
            print("DOM_SELFTEST: FAIL", e)
        return True
    return False


# 退出确认框文案：pywebview 的原生确认框默认英文，这里统一覆盖为中文。
# 文案同时点明「任务运行中退出会中断」，避免用户以为只是关掉窗口。
QUIT_LOCALIZATION = {
    "global.quitConfirmation": (
        "确定要退出众源感知解译工具吗？\n\n"
        "如仍有任务正在运行，退出将中断当前任务。"
    ),
    "global.quit": "退出",
    "global.ok": "确定",
    "global.cancel": "取消",
}


# ─────────── Windows 环境检查 ───────────
# pywebview 在检测不到 WebView2 运行时（或 .NET < 4.6.2）时会静默回退到
# IE 内核渲染，现代界面直接白屏且无任何提示。这里在开窗前显式检测，
# 缺什么就用系统对话框讲清楚，并指引导用户使用随包分发的离线安装器。

_WEBVIEW2_KEYS = (
    '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',   # Evergreen Runtime
    '{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}',   # Beta
    '{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}',   # Developer
    '{65C35B14-6C1D-4122-AC46-7148CC9D6497}',   # Canary
)
_WEBVIEW2_MIN = (86, 0, 622, 0)


def _win_runtime_ok():
    """返回 (是否满足, 缺失项)；判定口径与 pywebview 保持一致。"""
    import winreg

    def read_pv(root, key):
        paths = [rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{key}']
        if root == winreg.HKEY_LOCAL_MACHINE:
            paths.append(rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{key}')
        for path in paths:
            try:
                with winreg.OpenKey(root, path) as k:
                    v, _ = winreg.QueryValueEx(k, 'pv')
                    parts = tuple(int(x) for x in str(v).split('.') if x.isdigit())
                    if parts >= _WEBVIEW2_MIN:
                        return True
            except (OSError, ValueError):
                continue
        return False

    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as k:
            release, _ = winreg.QueryValueEx(k, 'Release')
        if int(release) < 394802:          # .NET 4.6.2
            return False, 'NET'
    except (OSError, ValueError):
        return False, 'NET'

    for key in _WEBVIEW2_KEYS:
        if (read_pv(winreg.HKEY_CURRENT_USER, key)
                or read_pv(winreg.HKEY_LOCAL_MACHINE, key)):
            return True, ''
    return False, 'WEBVIEW2'


def windows_runtime_setup():
    """确保 Windows 有可用的 WebView2 运行时。

    优先使用随包分发的固定版运行时（webview2-runtime/，与程序同目录）：
    内网/无网环境零安装、零下载、无需管理员权限，解压即可用。
    固定版缺失时回退系统安装的运行时；两者都缺失才弹提示退出。
    """
    if sys.platform != 'win32':
        return
    if getattr(sys, 'frozen', False):
        # onedir：运行时在 exe 同目录；onefile：在 _MEIPASS 临时目录
        candidates = [os.path.join(os.path.dirname(sys.executable),
                                   'webview2-runtime')]
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            candidates.insert(0, os.path.join(meipass, 'webview2-runtime'))
        for bundled in candidates:
            if os.path.isfile(os.path.join(bundled, 'msedgewebview2.exe')):
                webview.settings['WEBVIEW2_RUNTIME_PATH'] = bundled   # 固定版随包运行时
                return
    ok, missing = _win_runtime_ok()
    if ok:
        return
    import ctypes
    if missing == 'WEBVIEW2':
        msg = ('未检测到微软 WebView2 运行时，工具界面无法渲染（打开后窗口空白）。\n\n'
               '当前使用的交付包不含随包运行时。请向交付方索取包含 webview2-runtime\n'
               '目录的完整交付包：解压后 webview2-runtime 文件夹与程序放在同一目录，\n'
               '无需安装、无需联网即可运行。')
    else:
        msg = ('未检测到 .NET Framework 4.6.2 或更高版本，工具界面无法渲染。\n'
               '请先安装 .NET Framework 4.6.2 及以上版本后再运行本工具。')
    MB_ICONWARNING = 0x30
    MB_SETFOREGROUND = 0x10000
    MB_TOPMOST = 0x40000
    ctypes.windll.user32.MessageBoxW(
        0, msg, '环境检查提示',
        MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST)
    sys.exit(1)


def dump_startup_error():
    """把启动/运行异常写入本地日志，供离线排障（路径返回给用户）。"""
    import tempfile
    import traceback
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    d = os.path.join(base, 'AIVisionTool')
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, '启动错误.log')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(traceback.format_exc())
        return p
    except Exception:
        return None


def main():
    _utf8_stdio()
    mode = _selftest_mode()
    if mode:
        # 自检不需要 WebView2，也不应因缺少运行时而弹框阻塞（CI/排障场景）
        _write_boot_marker(mode)
    else:
        windows_runtime_setup()
    if frozen_selftest():
        return
    api = Api()
    webview.create_window(
        title="",   # 不显示原生标题文字
        url=resource_path("web/index.html"),
        js_api=api,
        width=1360,
        height=900,
        min_size=(1080, 700),
        background_color="#F2F3F5",
        # 关闭窗口前弹系统确认框（macOS 为 NSAlert，Windows 为系统 MessageBox）
        confirm_close=True,
        localization=QUIT_LOCALIZATION,
    )
    try:
        webview.start(debug=("--debug" in sys.argv),
                      gui="cocoa" if sys.platform == "darwin" else None)
    except Exception:
        log_path = dump_startup_error()
        if sys.platform == 'win32':
            import ctypes
            msg = '工具启动异常，已退出。\n\n错误日志：%s' % (log_path or '写入失败')
            ctypes.windll.user32.MessageBoxW(0, msg, '环境检查提示', 0x30 | 0x10000)


if __name__ == "__main__":
    main()
