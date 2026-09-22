from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")
from PySide6.QtWidgets import QAbstractItemView, QMessageBox

import app.broadcast.paths as paths_mod
from app.broadcast.report_store import ReportStore
from gui.tabs.run_monitor import RunMonitorPanel


def _history_run_ids(panel: RunMonitorPanel) -> list[str]:
    out: list[str] = []
    for i in range(panel._history.count()):  # noqa: SLF001 - test introspection
        item = panel._history.item(i)  # noqa: SLF001 - test introspection
        out.append(str(item.data(0x0100) or ""))  # Qt.ItemDataRole.UserRole
    return out


def test_run_history_isolated_by_account(qtbot, tmp_path: Path, monkeypatch) -> None:
    def fake_resolve_path(p: str | Path) -> Path:
        target = Path(p)
        return target if target.is_absolute() else (tmp_path / target)

    monkeypatch.setattr(paths_mod, "resolve_path", fake_resolve_path)

    # 先为两个账号分别写入一条运行报告。
    store_a = ReportStore(account_id="acct_a")
    store_b = ReportStore(account_id="acct_b")
    store_a.append("run-a", {"type": "text", "status": "sent", "step_index": 0})
    store_b.append("run-b", {"type": "text", "status": "sent", "step_index": 0})

    panel = RunMonitorPanel(plan_provider=lambda: None)
    qtbot.addWidget(panel)

    panel.set_account("acct_a")
    ids_a = _history_run_ids(panel)
    assert "run-a" in ids_a
    assert "run-b" not in ids_a

    panel.set_account("acct_b")
    ids_b = _history_run_ids(panel)
    assert "run-b" in ids_b
    assert "run-a" not in ids_b


def test_run_history_multi_select_and_delete(qtbot, tmp_path: Path, monkeypatch) -> None:
    def fake_resolve_path(p: str | Path) -> Path:
        target = Path(p)
        return target if target.is_absolute() else (tmp_path / target)

    monkeypatch.setattr(paths_mod, "resolve_path", fake_resolve_path)

    store = ReportStore(account_id="acct_a")
    for run_id in ("run-a", "run-b", "run-c"):
        store.append(run_id, {"type": "text", "status": "sent", "step_index": 0})
        store.export_csv(run_id)

    panel = RunMonitorPanel(plan_provider=lambda: None)
    qtbot.addWidget(panel)
    panel.set_account("acct_a")
    assert panel._history.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection  # noqa: SLF001

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)

    panel._delete_selected_history(["run-a", "run-b"])  # noqa: SLF001

    ids = _history_run_ids(panel)
    assert "run-a" not in ids
    assert "run-b" not in ids
    assert "run-c" in ids
    assert not store.report_path("run-a").exists()
    assert not (store.reports_dir / "plan_run-a.csv").exists()
