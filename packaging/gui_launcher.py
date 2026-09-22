"""
GUI 版打包入口（新 GUI 链路）。

该入口直接复用 `python -m gui.main` 的启动逻辑，确保打包态与开发态
进入同一套界面与引导流程。
"""

from __future__ import annotations

from gui.main import main


if __name__ == "__main__":
    raise SystemExit(main())
