"""把 payload.zip 追加到 onefile 启动器末尾，生成最终交付的单文件 exe。

用法（在 make_payload.py 与 build_launcher.spec 之后）：
    python tools/make_single_exe.py
    # 等价于：
    #   --launcher dist_single/AIVisionTool.exe
    #   --payload build/payload.zip
    #   --output dist_single/AIVisionTool-Windows-x64.exe

为什么可以追加：PyInstaller 6 的引导程序从文件末尾向前搜索 CArchive cookie，
追加在 exe 之后的数据不会被当作自身归档；启动器运行时再用同样的方式从末尾
定位 payload。代码签名追加的证书表也在文件末尾，同样兼容。

校验：
1. 启动器本身不能已包含 payload（防止重复追加）；
2. 追加后能从输出文件末尾定位到 zip，条目数与 payload 一致；
3. 抽查最小条目的 CRC 与内容（zipfile 读取时自动校验 CRC）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launcher  # noqa: E402  （仅标准库，可安全复用其 payload 定位逻辑）

DEFAULT_LAUNCHER = ROOT / "dist_single" / "AIVisionTool.exe"
DEFAULT_PAYLOAD = ROOT / "build" / "payload.zip"
DEFAULT_OUTPUT = ROOT / "dist_single" / "AIVisionTool-Windows-x64.exe"
MAIN_EXE = "AIVisionTool.exe"


def _check_launcher(path: Path) -> None:
    try:
        zf, fp = launcher.open_appended_zip(str(path))
    except Exception:
        return
    try:
        if len(zf.infolist()) > 0:
            raise SystemExit(
                f"{path} 已包含追加的 payload，请先用 pyinstaller 重新生成启动器。")
    finally:
        zf.close()
        fp.close()


def _append(launcher_path: Path, payload_path: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    try:
        with open(launcher_path, "rb") as src, \
                open(payload_path, "rb") as pay, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
            shutil.copyfileobj(pay, out, 1 << 20)
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)


def _verify(output: Path, payload_path: Path) -> int:
    with zipfile.ZipFile(payload_path) as ref:
        ref_infos = ref.infolist()
        names = {i.filename for i in ref_infos}
        if MAIN_EXE not in names:
            raise SystemExit(f"payload 中缺少 {MAIN_EXE}，onedir 构建可能不完整")
        sample = min((i for i in ref_infos if i.file_size > 0),
                     key=lambda i: i.file_size)
        expected = ref.read(sample.filename)

    zf, fp = launcher.open_appended_zip(str(output))
    try:
        infos = zf.infolist()
        if len(infos) != len(ref_infos):
            raise SystemExit(
                f"追加校验失败：条目数 {len(infos)} != {len(ref_infos)}")
        if MAIN_EXE not in {i.filename for i in infos}:
            raise SystemExit("追加校验失败：定位到的 zip 中缺少主程序")
        actual = zf.read(sample.filename)
        if actual != expected:
            raise SystemExit(f"追加校验失败：{sample.filename} 内容不一致")
        return len(infos)
    finally:
        zf.close()
        fp.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="生成追加 payload 的单文件 exe")
    ap.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    ap.add_argument("--payload", type=Path, default=DEFAULT_PAYLOAD)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    for name, path in (("启动器", args.launcher), ("payload", args.payload)):
        if not path.is_file():
            raise SystemExit(
                f"未找到{name}：{path}\n"
                "请先执行 pyinstaller build_launcher.spec 与 tools/make_payload.py")

    _check_launcher(args.launcher)
    _append(args.launcher, args.payload, args.output)
    count = _verify(args.output, args.payload)
    size_mb = args.output.stat().st_size / 1048576
    print(f"[ok] 单文件 exe：{args.output}（{size_mb:.1f} MB，{count} 个条目）")


if __name__ == "__main__":
    main()
