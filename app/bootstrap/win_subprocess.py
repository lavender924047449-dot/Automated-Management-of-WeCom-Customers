"""Windows 下隐藏控制台窗口的 subprocess 包装 + 全局默认无窗口 patch。"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

_CREATE_NO_WINDOW = 0x08000000

# 保活：patch 只装一次
_GLOBAL_PATCH_INSTALLED = False


def _with_platform_flags(kwargs: dict[str, Any]) -> dict[str, Any]:
    out = dict(kwargs)
    if os.name == "nt":
        out.setdefault("creationflags", _CREATE_NO_WINDOW)
    return out


def run_hidden(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(*args, **_with_platform_flags(kwargs))


def check_output_hidden(*args: Any, **kwargs: Any) -> bytes:
    return subprocess.check_output(*args, **_with_platform_flags(kwargs))


def install_global_no_window_patch() -> bool:
    """把 CREATE_NO_WINDOW 打成全局默认，覆盖所有 subprocess.Popen 调用。

    背景
    ----
    项目里 spawn 子进程的调用点分布在几十个文件（adb、frida、ffmpeg、silk、
    第三方库 adbutils / uiautomator2 内部也会 Popen）。逐个包 run_hidden 不现实，
    且每次引入新依赖都可能带出新的 subprocess 位点。

    做法
    ----
    在 Windows 上 monkey-patch ``subprocess.Popen.__init__``，如果调用方没有显式
    指定 ``creationflags``，就自动 or 上 CREATE_NO_WINDOW。调用方显式传了
    creationflags 的（比如需要 CREATE_NEW_CONSOLE / DETACHED_PROCESS 的特殊场景）
    则完全不干预，尊重原意。

    幂等；非 Windows 直接返回 False；返回 True 表示已安装。
    """
    global _GLOBAL_PATCH_INSTALLED
    if sys.platform != "win32":
        return False
    if _GLOBAL_PATCH_INSTALLED:
        return True

    original_init = subprocess.Popen.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # 调用方显式传了 creationflags → 尊重（可能是 CREATE_NEW_CONSOLE 之类）
        if kwargs.get("creationflags") is None or kwargs.get("creationflags") == 0:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        # 中文 Windows 上 text=True 默认按 GBK 解码。adb / 设备路径是 UTF-8，
        # 文件名一带中文，读 stdout 的线程就会 UnicodeDecodeError，采集直接断掉。
        text_mode = bool(kwargs.get("text") or kwargs.get("universal_newlines"))
        if text_mode and not kwargs.get("encoding"):
            kwargs["encoding"] = "utf-8"
            kwargs.setdefault("errors", "replace")
        return original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _patched_init  # type: ignore[method-assign]
    _GLOBAL_PATCH_INSTALLED = True

    try:
        from loguru import logger

        logger.info("subprocess 全局 CREATE_NO_WINDOW patch 已安装")
    except Exception:
        pass
    return True


__all__ = [
    "run_hidden",
    "check_output_hidden",
    "install_global_no_window_patch",
]
