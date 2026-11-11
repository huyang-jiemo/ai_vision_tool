"""pywebview 桌面入口：双击打开即用，零终端依赖（打包后）。"""
import os
import sys

import webview

from api import Api


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def frozen_selftest():
    """冻结环境自检（发布验证用，正常用户不触发）：
    AVT_SELFTEST=gdb 验证 GDB 导出依赖；=rasterio 验证正射影像读取依赖；
    =dom 验证标线提取；=deps 验证 pyproj/geopandas/pyogrio 依赖链。"""
    mode = os.environ.get("AVT_SELFTEST", "")
    if mode == "gdb":
        import tempfile
        from pipeline.exporter import export_points_gdb
        feats = [{"x": 500000.0, "y": 3000000.0, "class_id": "2", "class_cn": "自检",
                  "session": "S", "image": "I", "dist": 0.0, "score": 0.9}]
        p = os.path.join(tempfile.mkdtemp(prefix="avt_selftest_"), "t.gdb")
        ok = export_points_gdb(p, "detections", feats)
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
        # 曾因 libproj 多副本冲突导致 pyproj 加载失败、正射提取直接中断
        try:
            import pyproj
            from pyproj import CRS
            import geopandas
            import pyogrio
            crs = CRS.from_epsg(3857)
            gdf = geopandas.GeoDataFrame({"a": [1]}, geometry=geopandas.points_from_xy([0], [0]),
                                         crs=crs)
            ok = gdf.crs is not None and gdf.crs.to_epsg() == 3857
            print("DEPS_SELFTEST:", "PASS" if ok else "FAIL",
                  f"(pyproj={pyproj.__version__}, gpd={geopandas.__version__}, "
                  f"pyogrio={pyogrio.__version__}, crs={gdf.crs})")
        except Exception as e:
            print("DEPS_SELFTEST: FAIL", type(e).__name__, e)
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


def windows_env_check():
    """仅 Windows：缺失运行时时弹系统对话框说明原因与装法，避免白屏。"""
    if sys.platform != 'win32':
        return
    ok, missing = _win_runtime_ok()
    if ok:
        return
    import ctypes
    if missing == 'WEBVIEW2':
        msg = ('未检测到微软 WebView2 运行时（或版本过低），\n'
               '工具界面无法渲染，打开后会是空白窗口。\n\n'
               '解决方法：\n'
               '  1. 打开本程序所在目录下的 webview2-runtime 文件夹，\n'
               '     双击 MicrosoftEdgeWebView2RuntimeInstallerX64.exe 安装\n'
               '     （离线安装包，无需联网；如提示需要管理员权限，请联系 IT）；\n'
               '  2. 或在有网的电脑上从微软官网下载 WebView2 离线安装包，\n'
               '     拷贝到本机安装。\n\n'
               '安装完成后重新打开本工具即可。')
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
    windows_env_check()
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
