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


def test_board_message_activation_opens_preview(qtbot, monkeypatch) -> None:
    called: dict[str, object] = {}

    def fake_open_message_preview(parent, step, *, account_id, preview_service_provider, name=""):
        called["step"] = dict(step)
        called["account_id"] = account_id
        called["name"] = name

    monkeypatch.setattr(
        "gui.widgets.pending_plan_detail.open_message_preview",
        fake_open_message_preview,
    )

    panel = RunMonitorPanel(plan_provider=lambda: None)
    qtbot.addWidget(panel)
    panel.set_account("acct_a")

    panel._on_board_node(  # noqa: SLF001 - test explicit branch
        {
            "kind": "message",
            "preview": {
                "name": "测试消息",
                "step": {"type": "text", "body": "hello"},
            },
        }
    )

    assert called["account_id"] == "acct_a"
    assert called["name"] == "测试消息"
    assert called["step"] == {"type": "text", "body": "hello"}


def test_continue_button_resumes_the_saved_interrupted_run(qtbot, tmp_path: Path, monkeypatch) -> None:
    from app.broadcast.last_run_store import LastRunStore
    from app.broadcast.plan import BroadcastPlan
    from app.broadcast.runner import RunSummary

    def fake_resolve_path(p: str | Path) -> Path:
        target = Path(p)
        return target if target.is_absolute() else (tmp_path / target)

    monkeypatch.setattr(paths_mod, "resolve_path", fake_resolve_path)
    plan = BroadcastPlan.model_validate(
        {
            "contacts": [{"display_name": "甲", "conv_local": "1001"}],
            "sequence": [{"type": "text", "body": "hello"}],
        }
    )
    LastRunStore(account_id="acct_resume").save(
        run_uuid="run-paused",
        plan=plan.model_dump(mode="json"),
        status="interrupted",
    )
    calls: list[dict[str, object]] = []

    class FakeRunService:
        def start(self, _plan, **kwargs):
            calls.append(kwargs)
            return RunSummary(run_uuid="run-paused")

    panel = RunMonitorPanel(plan_provider=lambda: plan)
    qtbot.addWidget(panel)
    panel.set_account("acct_resume")
    panel._last_plan = plan  # noqa: SLF001 - set up resumable UI state
    panel._last_plan_run_uuid = "run-paused"  # noqa: SLF001
    panel._run_uuid = "run-paused"  # noqa: SLF001
    panel.set_run_service_factory(lambda _account, _store: FakeRunService())

    panel._live_clock = ""  # noqa: SLF001 - resume must not depend on the clock string
    panel._on_board_stop()  # noqa: SLF001 - top button path after the run has stopped
    qtbot.waitUntil(lambda: bool(calls), timeout=2000)

    assert calls[0]["resume_uuid"] == "run-paused"
    assert calls[0]["only_failed"] is False
