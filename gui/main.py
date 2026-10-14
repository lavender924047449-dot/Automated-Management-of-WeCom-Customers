"""GUI 程序入口（兼容 `python -m gui.main`）。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.bootstrap.frozen_dispatch import maybe_dispatch_frozen_subcommand
from app.bootstrap.runtime_bootstrap import DEFAULT_ADB_SERIAL, bootstrap_android_runtime
from app.bootstrap.win_job import bind_kill_on_close
from app.bootstrap.win_subprocess import install_global_no_window_patch
from app.config import resolve_path
from app.updater.frida_script_puller import pull_adapter_if_needed
from app.util.logging import setup_logging
from gui.copy_support import install_copy_support
from gui.main_window import MainWindow
from gui.zh_ui import install_zh_ui


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Broadcast GUI")
    p.add_argument("--voice-index-path", default="", help="voice_lib_index.json 路径")
    p.add_argument("--skip-bootstrap", action="store_true", help="跳过 adb/frida-server 自检")
    p.add_argument("--no-auto-adapter", action="store_true", help="禁用启动时自动拉取 Frida 适配脚本")
    p.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL, help="模拟器 adb serial")
    return p.parse_args()

def main() -> int:
    # 一等公民：先把 subprocess 全局无窗口 patch 装上，再做任何 spawn。
    # frozen 下（无控制台的 GUI）必须装，否则 adb / ffmpeg / silk 各种黑框乱闪；
    # dev 态也可以装，不影响 IDE 里终端跑（IDE 的 REPL 不走 subprocess.Popen）。
    if sys.platform == "win32":
        install_global_no_window_patch()

    internal_exit = maybe_dispatch_frozen_subcommand(sys.argv)
    if internal_exit is not None:
        return int(internal_exit)

    args = _parse_args()

    # 打包 GUI 没有控制台，`print(...)` 消失在虚空；补一次 setup_logging
    # 让 bootstrap / 联系人刷新 / adapter 拉取的所有 loguru 消息都进 logs/。
    # 幂等，dev 态重复调用也不会有副作用。
    from loguru import logger  # 延迟 import，避免影响 frozen_dispatch 前置逻辑
    try:
        setup_logging(log_dir=resolve_path("logs"))
    except Exception as exc:  # 极端情况下（比如 logs 无权限）不阻断 GUI
        print(f"[logging] setup failed: {exc}")

    # 「父死子亡」Job Object：主 GUI 一旦退出（正常退出 / 崩溃 / 任务栏关闭 /
    # 任务管理器结束），所有子进程（__runmod__ 派生 exe、adb.exe、CaptureClient
    # 的 Popen、wxa QProcess 等）立即被 Windows 内核回收。彻底根治「托盘退出
    # 后 dist 目录里还残留 exe 进程 + 日志文件被占用」的老问题。
    # 只在 frozen 下启用：dev 态在 IDE 里跑，绑 job 会连带杀掉 IDE 里的其他子进程。
    if bool(getattr(sys, "frozen", False)) or os.environ.get("TESTVOICE_HOME", "").strip():
        bind_kill_on_close()

    if not args.skip_bootstrap:
        warns = bootstrap_android_runtime(adb_serial=args.adb_serial)
        for row in warns:
            logger.warning("[bootstrap] {}", row)
    pull_result = pull_adapter_if_needed(no_auto_adapter=args.no_auto_adapter)
    if pull_result.error:
        logger.warning("[adapter] {}", pull_result.error)
    elif pull_result.message:
        logger.info("[adapter] {}", pull_result.message)

    app = QApplication(sys.argv)
    install_zh_ui(app)
    install_copy_support(app)
    index_path = Path(args.voice_index_path.strip()) if args.voice_index_path.strip() else None
    win = MainWindow(voice_index_path=index_path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
