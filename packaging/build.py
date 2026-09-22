"""
一键打包脚本。

GUI 交付物（--gui-only）：
    dist/企微客户自动化管理工具.exe
    dist/vendor/ffmpeg/ffmpeg.exe
    dist/vendor/frida-server-16.7.19-android-x86_64
    dist/vendor/adb.exe            # Google 官方 platform-tools
    dist/vendor/AdbWinApi.dll      # adb.exe 运行时依赖
    dist/vendor/AdbWinUsbApi.dll   # adb.exe 运行时依赖
    dist/使用说明.txt
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
SPEC_CLI = PACKAGING / "wecom-voice-blaster-cli.spec"
SPEC_GUI = PACKAGING / "wecom-voice-blaster-gui.spec"
FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
FRIDA_SERVER = ROOT / "vendor" / "frida-server-16.7.19-android-x86_64"
ADB_EXE = ROOT / "vendor" / "adb.exe"
ADB_DLLS = [
    ROOT / "vendor" / "AdbWinApi.dll",
    ROOT / "vendor" / "AdbWinUsbApi.dll",
]
GUI_EXE_NAME = "企微客户自动化管理工具.exe"
GUI_README_SOURCE = PACKAGING / "使用说明-gui-desktop.txt"
GUI_README_DIST_NAME = "使用说明.txt"


def _run_pyinstaller(spec: Path) -> None:
    print(f"\n─── pyinstaller: {spec.name} ───")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(spec),
        "--clean",
        "--noconfirm",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
    ]
    print("  $ " + " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))


def _ensure_ffmpeg() -> None:
    if FFMPEG.is_file():
        print(f"[ok] 已内置 ffmpeg: {FFMPEG}  ({FFMPEG.stat().st_size / 1024 / 1024:.1f} MB)")
        return
    fetch = ROOT / "scripts" / "fetch_ffmpeg.py"
    print("! 未找到 vendor/ffmpeg/ffmpeg.exe，尝试自动下载…")
    if not fetch.is_file():
        print("  缺少 scripts/fetch_ffmpeg.py，打包后语音预处理可能不可用")
        return
    rc = subprocess.call([sys.executable, str(fetch)], cwd=str(ROOT))
    if rc != 0 or not FFMPEG.is_file():
        print("  自动下载失败。可稍后手动把 ffmpeg.exe 放到 vendor/ffmpeg/ 再重新打包。")


def _copy_required_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise FileNotFoundError(f"缺少文件: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  + {dst.relative_to(ROOT)}")


def _prepare_gui_delivery() -> None:
    dist_dir = ROOT / "dist"
    gui_exe = dist_dir / GUI_EXE_NAME
    if not gui_exe.is_file():
        raise FileNotFoundError(f"未找到 GUI 产物: {gui_exe}")

    _copy_required_file(
        FFMPEG,
        dist_dir / "vendor" / "ffmpeg" / "ffmpeg.exe",
    )
    _copy_required_file(
        FRIDA_SERVER,
        dist_dir / "vendor" / FRIDA_SERVER.name,
    )
    # adb 三件套：`app/bootstrap/runtime_bootstrap.resolve_adb_binary` 会优先
    # 找 exe 旁的 vendor/adb.exe；缺失会回退到 PATH，销售机器上很可能没有配。
    # AdbWinApi.dll / AdbWinUsbApi.dll 是 adb.exe 的运行时依赖，必须同目录。
    _copy_required_file(ADB_EXE, dist_dir / "vendor" / ADB_EXE.name)
    for dll in ADB_DLLS:
        _copy_required_file(dll, dist_dir / "vendor" / dll.name)
    _copy_required_file(
        GUI_README_SOURCE,
        dist_dir / GUI_README_DIST_NAME,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 wecom-voice-blaster")
    ap.add_argument("--cli-only", action="store_true")
    ap.add_argument("--gui-only", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="先删 build/ dist/ 再打包")
    args = ap.parse_args()

    if args.clean:
        for d in ("build", "dist"):
            p = ROOT / d
            if p.exists():
                print(f"  rm -rf {p}")
                shutil.rmtree(p)

    _ensure_ffmpeg()

    do_cli = not args.gui_only
    do_gui = not args.cli_only

    if do_cli:
        _run_pyinstaller(SPEC_CLI)
        cli_dir = ROOT / "dist" / "wecom-voice-blaster"
        readme = PACKAGING / "使用说明.txt"
        if cli_dir.is_dir() and readme.is_file():
            shutil.copy2(readme, cli_dir / "使用说明.txt")
    if do_gui:
        _run_pyinstaller(SPEC_GUI)
        _prepare_gui_delivery()

    print("\n[done] 完成. 产物:")
    if do_cli:
        print(f"   {ROOT / 'dist' / 'wecom-voice-blaster' / 'wecom-voice-blaster.exe'}")
    if do_gui:
        print(f"   {ROOT / 'dist' / GUI_EXE_NAME}")
        print(f"   {ROOT / 'dist' / 'vendor'}")
        print(f"   {ROOT / 'dist' / GUI_README_DIST_NAME}")
        print("   请将 exe、vendor 文件夹、使用说明.txt 放在同一目录后双击 exe。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
