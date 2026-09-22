"""兼容导入：RunMonitorPanel 已迁移到 gui.tabs.run_monitor。"""

from __future__ import annotations

import warnings

warnings.warn(
    "`gui.run_monitor` 已弃用，将在后续版本移除；请改用 `gui.tabs.run_monitor`。",
    DeprecationWarning,
    stacklevel=2,
)

from gui.tabs.run_monitor import RunMonitorPanel

__all__ = ["RunMonitorPanel"]
