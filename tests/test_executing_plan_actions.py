from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView

from app.broadcast.dto import AssetRowDTO
from app.broadcast.report_store import ReportStore
from gui.tabs.plan_composer import PlanComposerPanel
from gui.tabs.run_monitor import RunMonitorPanel
from gui.widgets.send_board import BoardNode


def test_sequence_text_shows_named_doc_and_channel(qtbot) -> None:
    panel = PlanComposerPanel()
    qtbot.addWidget(panel)
    panel.set_library_assets(
        [
            AssetRowDTO(asset_type="channel", item_id="ch-1", name="春季视频"),
            AssetRowDTO(asset_type="doc", item_id="doc-1", name="产品说明"),
        ]
    )
    assert panel._sequence_text({"type": "channel", "template_id": "ch-1", "display_name": "春季视频"}) == "视频号：春季视频"
    assert panel._sequence_text({"type": "doc", "template_id": "doc-1", "display_name": "未命名"}) == "企微文档：产品说明"


def test_resend_button_asks_which_task(qtbot, monkeypatch, tmp_path) -> None:
    store = ReportStore(reports_dir=tmp_path / "reports")
    store.append(
        "run-1",
        {
            "type": "plan_meta",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "name": "回访任务",
            "plan": {
                "name": "回访任务",
                "contacts": [{"conv_local": "1", "display_name": "甲"}],
                "sequence": [{"type": "text", "body": "你好"}],
            },
        },
    )
    panel = RunMonitorPanel(plan_provider=lambda: None, report_store_provider=lambda _account_id: store)
    qtbot.addWidget(panel)
    assert panel._board.restart_button.isEnabled()
    asked: dict[str, object] = {}

    def fake_get_item(*args, **kwargs):
        asked["args"] = args
        labels = args[3]
        return labels[0], True

    started: list[dict] = []
    monkeypatch.setattr("gui.tabs.run_monitor.QInputDialog.getItem", fake_get_item)
    panel._start_worker = lambda **kwargs: started.append(kwargs)  # type: ignore[method-assign]
    panel._on_board_restart()
    assert asked["args"][1] == "重新发送任务"
    assert started and started[0]["plan"].name == "回访任务"


def test_executing_plans_support_multi_select(qtbot) -> None:
    panel = RunMonitorPanel(plan_provider=lambda: None)
    qtbot.addWidget(panel)
    tree = panel._board.executing
    assert tree.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    panel._board.set_lists(
        executing=[
            BoardNode(title="计划A", kind="plan", payload={"run_uuid": "a", "plan_name": "计划A", "kind": "plan"}),
            BoardNode(title="计划B", kind="plan", payload={"run_uuid": "b", "plan_name": "计划B", "kind": "plan"}),
        ],
        pending=[],
        cron=[],
    )
    tree.topLevelItem(0).setSelected(True)
    tree.topLevelItem(1).setSelected(True)
    selected = panel._selected_executing_plans()
    assert {row[1] for row in selected} == {"a", "b"}
    assert tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)["kind"] == "plan"
