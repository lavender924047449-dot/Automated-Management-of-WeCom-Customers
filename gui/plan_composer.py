"""兼容导出：保留旧路径 `gui.plan_composer`。"""

from __future__ import annotations

import warnings

warnings.warn(
    "`gui.plan_composer` 已弃用，将在后续版本移除；请改用 `gui.tabs.plan_composer`。",
    DeprecationWarning,
    stacklevel=2,
)

from gui.tabs.plan_composer import PlanComposerPanel

__all__ = ["PlanComposerPanel"]
