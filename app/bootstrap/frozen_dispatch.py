"""冻结态子命令分发 + 跨 dev/frozen 的 python -m 命令构造器。

背景
-----
PyInstaller onefile 打包后，``sys.executable`` = exe 本身，而不是 python.exe。
如果代码里用 ``[sys.executable, "-m", "runtime.xxx", ...]`` 拉子进程，在 frozen 下
就等于「拉起一份新的 exe」——不识别 `-m` 参数，落到 `main()` 分支再开一个 GUI 窗口。

历史上 `runner.py` 单独走 ``__send_engine__`` 哨兵解决了发送链路。本模块把该机制
推广到所有需要 spawn Python 子命令的位置，避免 GUI 被无限拉起。

约定
-----
- 内部命令均以 ``__`` 前缀开头，避免与用户 CLI 冲突。
- ``__send_engine__``：兼容旧调用，直接跑 send_engine.main。
- ``__runmod__ <dotted.module> [args...]``：通用「跑一个模块的 main()」。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence


def _dispatch_runmod(args: list[str]) -> int:
    # 尽量早地把日志打通：子进程也应该往 logs/ 落盘，才好排查 frozen 特有 bug。
    # 这里 try/except 完全兜底 —— 记日志失败绝不能挡住子命令本身。
    try:
        from app.util.logging import setup_logging
        from app.config import resolve_path
        setup_logging(log_dir=resolve_path("logs"))
    except Exception:
        pass

    try:
        from loguru import logger
    except Exception:
        logger = None  # type: ignore[assignment]

    if not args:
        msg = "[frozen_dispatch] __runmod__ 需要模块名参数"
        print(msg, file=sys.stderr)
        if logger:
            logger.error(msg)
        return 2
    module_name = args[0]
    forwarded = args[1:]

    if logger:
        logger.info("[__runmod__] dispatch {} args={}", module_name, forwarded)

    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:
        msg = f"[frozen_dispatch] import {module_name} 失败: {exc}"
        print(msg, file=sys.stderr)
        if logger:
            logger.exception(msg)
        return 3

    main_fn = getattr(mod, "main", None)
    if not callable(main_fn):
        msg = f"[frozen_dispatch] {module_name} 缺 main() 入口"
        print(msg, file=sys.stderr)
        if logger:
            logger.error(msg)
        return 4

    # 让子模块像被 ``python -m xxx`` 拉起时一样，看到 argv 里带自己的模块名。
    old_argv = sys.argv
    sys.argv = [module_name, *forwarded]
    try:
        rc = main_fn(forwarded) if _accepts_args(main_fn) else main_fn()
    except SystemExit as exc:
        rc = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception as exc:
        if logger:
            logger.exception("[__runmod__] {} raised: {}", module_name, exc)
        else:
            print(f"[frozen_dispatch] {module_name} raised: {exc}", file=sys.stderr)
        return 5
    finally:
        sys.argv = old_argv
    if logger:
        logger.info("[__runmod__] {} exit rc={}", module_name, rc)
    return int(rc or 0)


def _accepts_args(fn) -> bool:
    """粗略判断 main() 是否接受一个位置参数（args 列表）。"""
    try:
        import inspect

        sig = inspect.signature(fn)
        positional = [
            p for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        return len(positional) >= 1
    except Exception:
        return False


def maybe_dispatch_frozen_subcommand(argv: Sequence[str] | None = None) -> int | None:
    """处理打包态内部子命令；非内部命令时返回 None。"""

    args = list(argv) if argv is not None else list(sys.argv)
    if len(args) < 2:
        return None

    token = args[1]
    if token == "__send_engine__":
        from runtime.wework_re_android import send_engine
        return int(send_engine.main(args[2:]))
    if token == "__runmod__":
        return _dispatch_runmod(args[2:])
    return None


def build_python_module_cmd(module: str, *module_args: str) -> list[str]:
    """构造「跑一个 Python 模块」的命令。

    - dev: ``[python.exe, "-m", <module>, *args]``
    - frozen: ``[exe, "__runmod__", <module>, *args]``（走 dispatcher）

    调用方无需再做 frozen 判断，全部经此 helper。
    """
    if bool(getattr(sys, "frozen", False)):
        return [sys.executable, "__runmod__", module, *[str(a) for a in module_args]]
    return [sys.executable, "-m", module, *[str(a) for a in module_args]]


__all__ = [
    "build_python_module_cmd",
    "maybe_dispatch_frozen_subcommand",
]
