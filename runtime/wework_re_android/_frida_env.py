"""Frida 导入与环境提示（Android 运行时脚本共用）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 与 app.bootstrap.runtime_bootstrap.ADB_ENV_KEY 保持一致。
# 这里不直接 import app，避免探针作为独立子进程启动时形成重依赖。
_ADB_ENV_KEY = "TESTVOICE_ADB_BINARY"


def import_frida():
    """返回 frida 模块；缺失时给出可执行的修复提示并退出。"""

    try:
        import frida  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        root = Path(__file__).resolve().parents[2]
        venv_py = root / ".venv" / "Scripts" / "python.exe"
        hint = (
            "缺少 Python 包 `frida`（当前解释器: "
            f"{sys.executable}）。\n"
            "Android 探针/发送脚本请使用项目虚拟环境，例如:\n"
        )
        if venv_py.is_file():
            hint += f'  "{venv_py}" -m runtime.wework_re_android.probe_customer_labels ...\n'
        hint += "或先安装: pip install frida==16.7.19"
        raise SystemExit(hint) from None
    return frida


def adb_binary() -> str:
    """返回 adb 可执行文件路径。

    优先读 ``TESTVOICE_ADB_BINARY``（由 ``resolve_adb_binary`` 在启动时写入，
    指向 exe 旁的 ``vendor/adb.exe``）。环境变量缺失时回退到 PATH 里的 ``adb``。
    """

    value = os.environ.get(_ADB_ENV_KEY, "").strip()
    return value or "adb"
