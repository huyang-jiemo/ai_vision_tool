"""启动器逻辑自检（不需要 PyInstaller / Windows，任何平台可跑）。

覆盖：首次解压、二次启动跳过、版本升级与清理、版本号清洗、磁盘空间不足、
并发锁（存活进程等待超时 / 崩溃残留锁清理）、payload 缺失、payload 损坏。

用法：python tools/selftest_launcher.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launcher  # noqa: E402


def _utf8_stdout() -> None:
    """Windows 控制台默认可能是 cp1252/cp936，打印中文会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _payload(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def _run(mod, sandbox: Path, calls: list):
    """模拟冻结后的 _MEIPASS，拦截子进程与错误弹窗。

    Windows 上 ``_fatal`` 会调用 MessageBoxW；CI/无桌面会话中该调用可能
    一直等待用户点击，导致自检挂起。测试只验证返回码与文件状态，因此把
    错误弹窗替换为空操作。
    """
    old_meipass = getattr(sys, "_MEIPASS", None)
    old_popen = mod.subprocess.Popen
    old_fatal = getattr(mod, "_fatal", None)
    sys._MEIPASS = str(sandbox)
    mod.subprocess.Popen = lambda args, **kw: calls.append((args, kw))
    if old_fatal is not None:
        mod._fatal = lambda _msg: None
    try:
        return mod.main()
    finally:
        mod.subprocess.Popen = old_popen
        if old_fatal is not None:
            mod._fatal = old_fatal
        if old_meipass is None:
            del sys._MEIPASS
        else:
            sys._MEIPASS = old_meipass


def main() -> int:
    _utf8_stdout()
    sandbox = Path(tempfile.mkdtemp(prefix="avt_launcher_selftest_"))
    work = sandbox / "work"
    work.mkdir()
    shutil.copy(ROOT / "launcher.py", sandbox / "launcher.py")
    (sandbox / "payload.version").write_text("v1.0.0-abc1234", encoding="utf-8")
    _payload(sandbox / "payload.zip", {
        "AIVisionTool.exe": "fake-exe-v1",
        "_internal/runtime.dat": "x" * 5000,
    })
    os.environ["LOCALAPPDATA"] = str(work / "localappdata")

    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append((name, bool(cond), extra))
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}".rstrip())

    mod = launcher
    app = Path(os.environ["LOCALAPPDATA"]) / "AIVisionTool" / "app"
    target = app / "v1.0.0-abc1234"
    exe = target / "AIVisionTool.exe"
    ready = target / ".ready"

    # 1. 首次启动
    calls: list = []
    rc = _run(mod, sandbox, calls)
    check("首次启动返回 0", rc == 0)
    check("首次启动解压出主程序", exe.is_file())
    check("首次启动写入 .ready", ready.is_file())
    check("首次启动日志存在", (app.parent / "启动器日志.log").is_file())
    check("首次启动传参并设置 cwd",
          len(calls) == 1 and calls[0][0] == [str(exe)] and calls[0][1].get("cwd") == str(target))

    # 2. 二次启动不重复解压
    stamp = ready.stat().st_mtime
    time.sleep(1.1)
    calls = []
    rc = _run(mod, sandbox, calls)
    check("二次启动返回 0", rc == 0)
    check("二次启动跳过解压", abs(ready.stat().st_mtime - stamp) < 0.5)

    # 3. 升级：解压到新目录，保留最近一个旧版本
    (sandbox / "payload.version").write_text("v2.0.0-def5678", encoding="utf-8")
    _payload(sandbox / "payload.zip", {"AIVisionTool.exe": "fake-exe-v2"})
    calls = []
    rc = _run(mod, sandbox, calls)
    v2 = app / "v2.0.0-def5678"
    check("升级返回 0", rc == 0)
    check("升级解压到新目录", (v2 / "AIVisionTool.exe").is_file())
    check("升级保留最近旧版本", target.is_dir())
    check("升级后启动新版本", calls and calls[0][0][0] == str(v2 / "AIVisionTool.exe"))

    # 4. 再次升级：更早的版本被清理
    (sandbox / "payload.version").write_text("v2.1.0-aaa1111", encoding="utf-8")
    _payload(sandbox / "payload.zip", {"AIVisionTool.exe": "fake-exe-v21"})
    calls = []
    rcode = _run(mod, sandbox, calls)
    v21 = app / "v2.1.0-aaa1111"
    check("二次升级返回 0", rcode == 0)
    check("二次升级清理更早版本", not target.exists())
    check("二次升级保留上一版本", v2.is_dir())
    check("二次升级启动最新版本", calls and calls[0][0][0] == str(v21 / "AIVisionTool.exe"))

    # 5. 版本号清洗（路径穿越/非法字符）
    check("版本号清洗路径穿越", mod._safe_version("../../evil") == "evil")
    check("版本号清洗中文与空格", mod._safe_version("v1 版.本!") == "v1")

    # 6. 磁盘空间不足：拒绝解压
    (sandbox / "payload.version").write_text("v3.0.0-zzz9999", encoding="utf-8")
    old_free = mod._free_space
    mod._free_space = lambda _p: 1
    calls = []
    rc = _run(mod, sandbox, calls)
    mod._free_space = old_free
    check("空间不足返回 1", rc == 1)
    check("空间不足不解压", not (app / "v3.0.0-zzz9999").exists())
    check("空间不足不启动", not calls)

    # 7. 锁被存活进程持有：等待超时，不误删、不重复解压
    (sandbox / "payload.version").write_text("v3.1.0-live", encoding="utf-8")
    app.mkdir(parents=True, exist_ok=True)
    live_lock = app / ".extract.lock"
    live_lock.write_text(str(os.getpid()), encoding="ascii")
    old_wait = mod.LOCK_WAIT_SECONDS
    mod.LOCK_WAIT_SECONDS = 1.2
    calls = []
    t0 = time.time()
    rc = _run(mod, sandbox, calls)
    elapsed = time.time() - t0
    mod.LOCK_WAIT_SECONDS = old_wait
    check("存活锁返回 1", rc == 1)
    check("存活锁不被误删", live_lock.exists())
    check("存活锁等待超时", elapsed >= 1.0, f"{elapsed:.1f}s")
    check("存活锁下不启动", not calls)
    live_lock.unlink()

    # 8. 崩溃残留锁（无存活进程）：立即清理并完成解压
    (sandbox / "payload.version").write_text("v3.2.0-stale", encoding="utf-8")
    stale_lock = app / ".extract.lock"
    stale_lock.write_text("999999", encoding="ascii")
    old = time.time() - 60
    os.utime(stale_lock, (old, old))
    calls = []
    rc = _run(mod, sandbox, calls)
    check("残留锁返回 0", rc == 0)
    check("残留锁已清理", not stale_lock.exists())
    check("残留锁下完成解压", (app / "v3.2.0-stale" / ".ready").is_file())

    # 9. payload 缺失
    payload = sandbox / "payload.zip"
    bak = sandbox / "payload.zip.bak"
    payload.rename(bak)
    (sandbox / "payload.version").write_text("v4.0.0-nopay", encoding="utf-8")
    calls = []
    rc = _run(mod, sandbox, calls)
    bak.rename(payload)
    check("payload 缺失返回 1", rc == 1)
    check("payload 缺失不启动", not calls)

    # 10. payload 损坏：不留临时目录、不启动
    payload.write_bytes(b"not a zip")
    (sandbox / "payload.version").write_text("v4.1.0-badzip", encoding="utf-8")
    calls = []
    rc = _run(mod, sandbox, calls)
    leftovers = [p.name for p in app.iterdir() if ".tmp-" in p.name]
    check("损坏包返回 1", rc == 1)
    check("损坏包不启动", not calls)
    check("损坏包不留临时目录", not leftovers, str(leftovers))

    shutil.rmtree(sandbox, ignore_errors=True)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"LAUNCHER_SELFTEST: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
