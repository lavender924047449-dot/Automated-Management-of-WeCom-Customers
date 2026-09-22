"""
日志集中初始化 — 基于 loguru。

用法：
    from app.util.logging import setup_logging
    from loguru import logger
    setup_logging()               # 从配置读取
    logger.info("hello")

行为：
    - 控制台：彩色，INFO 及以上
    - 文件：logs/app_YYYY-MM-DD.log，DEBUG 及以上，按天切分，保留 N 天
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from loguru import logger


_initialized = False


def _console_stream():
    """
    可用的控制台流。

    PyInstaller GUI (console=False) 下 sys.stdout / sys.stderr 是 None,
    loguru.add(None) 会抛 TypeError: Cannot log to objects of type 'NoneType'。
    """
    for stream in (sys.stderr, sys.stdout):
        if stream is not None and hasattr(stream, "write"):
            return stream
    return None


def _prepare_windows_console() -> None:
    """在 Windows 上：强制 stderr 用 UTF-8 + 开启 ANSI 转义处理"""
    if sys.platform != "win32":
        return
    # 让中文不再乱码 (Python 3.7+)
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    # 让 loguru 的 ANSI 颜色能被 Windows 控制台渲染
    try:
        k32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STDOUT, STDERR
            h = k32.GetStdHandle(handle_id)
            mode = ctypes.c_ulong()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
                k32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str = "logs",
    keep_days: int = 14,
) -> None:
    """初始化 logger (幂等：重复调用只生效一次)"""
    global _initialized
    if _initialized:
        return

    _prepare_windows_console()

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()  # 清掉 loguru 默认 handler

    # 控制台 (无控制台的打包 GUI 跳过; 非 TTY 关闭颜色)
    console = _console_stream()
    if console is not None:
        is_tty = getattr(console, "isatty", lambda: False)()
        logger.add(
            console,
            level=level.upper(),
            colorize=is_tty,
            format=(
                "<green>{time:HH:mm:ss.SSS}</green> "
                "<level>{level:<7}</level> "
                "<cyan>{name}:{line}</cyan> - <level>{message}</level>"
            ),
        )

    # 文件（按天切分）
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",              # 每天 0 点切分
        retention=f"{keep_days} days", # 只保留 N 天
        encoding="utf-8",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | "
            "{name}:{function}:{line} | {message}"
        ),
        enqueue=True,                  # 多进程/线程安全
    )

    _initialized = True
    logger.debug(f"logging initialized  level={level}  dir={log_dir}")
