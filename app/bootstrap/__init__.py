"""运行时引导辅助。"""

from app.bootstrap.frozen_dispatch import maybe_dispatch_frozen_subcommand
from app.bootstrap.runtime_bootstrap import ADB_ENV_KEY, DEFAULT_ADB_SERIAL, bootstrap_android_runtime, resolve_adb_binary

__all__ = [
    "maybe_dispatch_frozen_subcommand",
    "bootstrap_android_runtime",
    "resolve_adb_binary",
    "ADB_ENV_KEY",
    "DEFAULT_ADB_SERIAL",
]

