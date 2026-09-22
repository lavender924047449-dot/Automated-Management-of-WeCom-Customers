"""setup_logging 在无控制台 (打包 GUI) 环境下不能崩。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

import app.util.logging as logmod


def test_setup_logging_skips_none_stderr(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(logmod.sys, "stderr", None)
    monkeypatch.setattr(logmod.sys, "stdout", None)
    logmod._initialized = False
    try:
        logmod.setup_logging(level="INFO", log_dir=tmp_path, keep_days=1)
        logger.info("windowed-mode ok")
    finally:
        logger.remove()
        logmod._initialized = False

    logs = list(tmp_path.glob("app_*.log"))
    assert logs
    assert "windowed-mode ok" in logs[0].read_text(encoding="utf-8")


def test_console_stream_none_when_both_missing(monkeypatch):
    monkeypatch.setattr(logmod.sys, "stderr", None)
    monkeypatch.setattr(logmod.sys, "stdout", None)
    assert logmod._console_stream() is None
