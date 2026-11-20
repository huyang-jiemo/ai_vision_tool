"""单文件启动器：把随包的 onedir 运行时解压到本机后启动主程序。

交付形态是一个 exe（AIVisionTool.exe），双击即用、离线可用、无需安装环境。

为什么不直接 `pyinstaller --onefile` 打主程序：
主程序解压后约 1.3 GB（其中 WebView2 固定版运行时约 878 MB、2300 多个文件），
onefile 每次启动都要重新解压全部文件，启动慢且大量写临时目录。本启动器把
onedir 结果打成一个 payload.zip **追加在启动器 exe 末尾**（不在 PyInstaller
datas 里，所以启动器自身只有十几 MB，每次启动不会拷贝 payload）：
- 首次启动：从自身末尾定位并解压 payload 到 %LOCALAPPDATA%\\AIVisionTool\\app\\
  <版本>-<内容摘要>，显示进度；解压完成写 .ready 标记；
- 之后启动：直接运行该目录下的 AIVisionTool.exe，秒开；
- 新版本：按版本号解压到新目录，成功后清理旧版本（被占用时忽略）。

PyInstaller 6 的引导程序从文件末尾向前搜索 CArchive cookie 来定位自身数据，
因此追加在 exe 之后的数据不影响启动，代码签名（追加在文件末尾）同理。

构建流程：build.spec → tools/make_payload.py → build_launcher.spec →
tools/make_single_exe.py（追加 payload 并校验）。
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

APP = "AIVisionTool"
LOCK_WAIT_SECONDS = 600       # 等待其他实例完成首次解压的最长时间
_ZIP_EOCD = b"PK\x05\x06"
_ZIP_EOCD_LEN = 22
_SCAN_CHUNK = 1 << 20         # 从文件末尾向前按 1 MB 扫描
_SCAN_LIMIT = 64 << 20        # 最多扫描 64 MB（覆盖常见的代码签名体积）


# ---------------------------------------------------------------- 路径与日志

def _meipass() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _free_space(path: str) -> int:
    """向上找到第一个已存在的目录再取可用空间（首次启动时目标目录尚不存在）。"""
    p = path
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(p or os.getcwd()).free
    except OSError:
        return 0


def _data_root() -> str:
    """运行时根目录；IT 可用 AVT_HOME 重定向到共享盘/指定目录。"""
    override = os.environ.get("AVT_HOME", "").strip()
    if override:
        return os.path.abspath(override)
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, APP)


def _read_text(name: str, default: str = "") -> str:
    try:
        with open(os.path.join(_meipass(), name), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return default


def _safe_version(raw: str) -> str:
    v = re.sub(r"[^0-9A-Za-z._-]+", "-", raw or "").strip("-.")
    return v or "dev"


def _log(msg: str) -> None:
    """启动器日志：离线排障时让客户回传。失败静默。"""
    try:
        d = _data_root()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "启动器日志.log")
        if os.path.exists(p) and os.path.getsize(p) > 512 * 1024:
            os.replace(p, p + ".old")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


# -------------------------------------------------------------------- splash

def _has_splash() -> bool:
    # 只有带 splash 的 Windows 引导程序才会注入该环境变量；
    # 先判断再导入，避免非 splash 构建打印 pyi_splash 的报错。
    return bool(os.environ.get("_PYI_SPLASH_IPC"))


def _splash_text(msg: str) -> None:
    if not _has_splash():
        return
    try:
        import pyi_splash  # PyInstaller 注入，仅 Windows 构建带 splash
        pyi_splash.update_text(msg)
    except Exception:
        pass


def _splash_close() -> None:
    if not _has_splash():
        return
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass


def _fatal(msg: str) -> None:
    _splash_close()
    _log("FATAL: " + msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "启动失败", 0x10)
    except Exception:
        pass


# --------------------------------------------------------- 追加式 payload 读取

class _SliceReader:
    """把文件中 [start, end) 区间包装成 zipfile 需要的只读文件对象。"""

    def __init__(self, fp, start: int, end: int):
        self._fp = fp
        self._start = start
        self._end = end
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        remaining = self._end - self._start - self._pos
        if n is None or n < 0 or n > remaining:
            n = remaining
        if n <= 0:
            return b""
        self._fp.seek(self._start + self._pos)
        data = self._fp.read(n)
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 1:
            offset += self._pos
        elif whence == 2:
            offset += self._end - self._start
        self._pos = max(0, min(offset, self._end - self._start))
        return self._pos

    def tell(self) -> int:
        return self._pos

    # zipfile（3.14+ 的 _SharedFile）会读取这些属性
    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False


def locate_payload(fp, size: int):
    """在文件末尾查找追加的 zip，返回 (start, end)；找不到返回 None。

    代码签名会把证书表追加在文件末尾，因此不能只看最后 22 字节，改为从后
    向前分块搜索 EOCD 并校验中央目录位置。
    """
    pos, scanned, tail = size, 0, b""
    while pos > 0 and scanned < _SCAN_LIMIT:
        step = min(_SCAN_CHUNK, pos)
        pos -= step
        fp.seek(pos)
        buf = fp.read(step) + tail
        i = buf.rfind(_ZIP_EOCD)
        while i >= 0:
            eocd = pos + i
            if eocd + _ZIP_EOCD_LEN <= size:
                fp.seek(eocd)
                rec = fp.read(_ZIP_EOCD_LEN)
                if len(rec) == _ZIP_EOCD_LEN and rec[:4] == _ZIP_EOCD:
                    (_sig, disk, cd_disk, n_disk, n_total,
                     size_cd, off_cd, comment_len) = struct.unpack("<4s4H2LH", rec)
                    if disk == 0 and cd_disk == 0 and n_disk == n_total:
                        start = eocd - size_cd - off_cd
                        if 0 <= start < eocd:
                            fp.seek(start)
                            if fp.read(4) in (b"PK\x03\x04", _ZIP_EOCD):
                                return start, eocd + _ZIP_EOCD_LEN + comment_len
            i = buf.rfind(_ZIP_EOCD, 0, i)
        tail = buf[:3]
        scanned += step
    return None


def open_appended_zip(path: str):
    """打开末尾追加了 payload 的文件，返回 (ZipFile, 需关闭的底层文件)。"""
    fp = open(path, "rb")
    try:
        size = os.fstat(fp.fileno()).st_size
        rng = locate_payload(fp, size)
        if not rng:
            raise RuntimeError(
                "未在程序末尾找到运行文件包。请重新获取完整的 "
                f"{APP}.exe（文件可能被截断或被修改）。")
        return zipfile.ZipFile(_SliceReader(fp, *rng)), fp
    except Exception:
        fp.close()
        raise


def _open_payload():
    """返回 (ZipFile, 需关闭的底层文件或 None)。"""
    if getattr(sys, "frozen", False):
        return open_appended_zip(os.path.abspath(sys.executable))
    path = os.path.join(_meipass(), "payload.zip")
    if not os.path.isfile(path):
        raise RuntimeError(
            "交付包不完整：缺少 payload.zip。请先执行 tools/make_payload.py。")
    return zipfile.ZipFile(path), None


# -------------------------------------------------------------------- 解压

def _extract(zf: zipfile.ZipFile, target: str) -> None:
    """解压到 target（先写临时目录再原子改名，避免留下半成品）。"""
    tmp = f"{target}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        infos = zf.infolist()
        total = max(len(infos), 1)
        for i, zi in enumerate(infos, 1):
            dest = zf.extract(zi, tmp)
            # zipfile 不恢复 Unix 权限；非 Windows 下补齐可执行位
            if os.name != "nt" and zi.create_system == 3:
                mode = (zi.external_attr >> 16) & 0o777
                if mode:
                    try:
                        os.chmod(dest, mode)
                    except OSError:
                        pass
            if i % 200 == 0 or i == total:
                _splash_text(f"{i * 100 // total}%")
        with open(os.path.join(tmp, ".ready"), "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        os.replace(tmp, target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _is_ready(target: str, exe: str) -> bool:
    return (os.path.isfile(os.path.join(target, ".ready"))
            and os.path.isfile(exe))


def _lock_owner(lock: str) -> int:
    try:
        with open(lock, "r", encoding="ascii", errors="ignore") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _pid_alive(pid: int) -> bool:
    """判断锁文件里的进程是否仍存活（崩溃残留的锁可立即清掉）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                # 87 = ERROR_INVALID_PARAMETER：进程不存在；其余（如拒绝访问）按存活处理
                return kernel32.GetLastError() != 87
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True   # 无法判断时按存活处理，宁可等待
    try:
        os.kill(pid, 0)   # POSIX：仅探测，不发送信号
        return True
    except OSError:
        return False


def _extract_guarded(target: str, exe: str) -> None:
    """加锁解压；并发启动时等待另一实例完成，避免互相删除半成品。"""
    app_dir = os.path.dirname(target)
    os.makedirs(app_dir, exist_ok=True)
    lock = os.path.join(app_dir, ".extract.lock")
    deadline = time.time() + LOCK_WAIT_SECONDS
    fd = None
    while not _is_ready(target, exe):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, str(os.getpid()).encode())
            except OSError:
                pass
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock)
            except OSError:
                age = 0.0
            # 超过 5 秒仍无存活进程持有 → 视为崩溃残留，立即清理
            if age > 5 and not _pid_alive(_lock_owner(lock)):
                try:
                    os.remove(lock)
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                raise TimeoutError(
                    "另一个实例正在准备运行文件，等待超时，请稍后重试。")
            _splash_text("等待其他实例…")
            time.sleep(0.5)
        except OSError:
            # 无法创建锁文件（只读目录/权限不足）：退化为无锁解压
            fd = None
            break
    try:
        if not _is_ready(target, exe):
            zf, raw = _open_payload()
            try:
                need = sum(zi.file_size for zi in zf.infolist())
                free = _free_space(app_dir)
                _log(f"首次解压：需要 {need / 1048576:.0f} MB，"
                     f"可用 {free / 1048576:.0f} MB")
                if free and free < need + 100 * 1048576:
                    raise OSError(
                        f"磁盘空间不足：首次启动需要约 {need / 1073741824:.1f} GB 空间。"
                        f"\n\n目标目录：{target}")
                _splash_text("0%")
                t0 = time.time()
                _extract(zf, target)
                _log(f"首次解压完成，用时 {time.time() - t0:.1f}s")
            finally:
                zf.close()
                if raw is not None:
                    raw.close()
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(lock)
            except OSError:
                pass


def _cleanup_old(current: str) -> None:
    """清理旧版本目录。

    保留当前版本和最近一个旧版本：旧版本可能正在运行，直接删除可能让它在
    加载后续 DLL 时出错；更早的版本和被中断的 .tmp-* 目录直接清理。
    """
    app_dir = os.path.dirname(current)
    try:
        old_dirs = []
        for name in os.listdir(app_dir):
            path = os.path.join(app_dir, name)
            if not os.path.isdir(path):
                continue
            if os.path.abspath(path) == os.path.abspath(current):
                continue
            if ".tmp-" in name:
                shutil.rmtree(path, ignore_errors=True)
                continue
            old_dirs.append(path)
        old_dirs.sort(key=os.path.getmtime, reverse=True)
        for path in old_dirs[1:]:
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def main() -> int:
    version = _safe_version(_read_text("payload.version", "dev"))
    target = os.path.join(_data_root(), "app", version)
    exe = os.path.join(target, APP + ".exe")

    _log(f"启动器：版本={version} 目标={target}")
    try:
        if not _is_ready(target, exe):
            _extract_guarded(target, exe)
            _cleanup_old(target)
    except Exception as e:
        _fatal(f"准备运行文件失败：{e}\n\n目标目录：{target}\n"
               "请确认磁盘空间充足，并将 "
               f"{APP}.exe 加入杀毒软件信任列表后重试。")
        return 1

    _splash_close()
    try:
        _log(f"启动主程序：{exe}")
        subprocess.Popen([exe] + sys.argv[1:], cwd=target, close_fds=True)
    except Exception as e:
        _fatal(f"启动主程序失败：{e}\n\n{exe}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
