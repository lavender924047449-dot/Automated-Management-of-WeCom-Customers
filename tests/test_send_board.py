from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from gui.widgets.send_board import BoardNode, SendBoard


def test_pending_payload_kind_is_not_overwritten(qtbot) -> None:
    board = SendBoard()
    qtbot.addWidget(board)
    pending_node = BoardNode(
        title="▣ 08:00:00 计划A",
        kind="plan",
        payload={"kind": "pending", "entry_id": "p-1"},
    )
    board.set_lists(executing=[], pending=[pending_node], cron=[])
    item = board.pending.topLevelItem(0)
    payload = item.data(0, Qt.ItemDataRole.UserRole)
    assert isinstance(payload, dict)
    assert payload.get("kind") == "pending"
    assert payload.get("entry_id") == "p-1"


def test_double_clicking_message_emits_preview_payload(qtbot) -> None:
    board = SendBoard()
    qtbot.addWidget(board)
    message = BoardNode(
        title="1. 语音 · 问候",
        kind="message",
        payload={
            "preview": {
                "step": {"type": "voice", "voice_id": "voice-1"},
                "name": "问候",
            }
        },
    )
    board.set_lists(executing=[message], pending=[], cron=[])
    board.show()

    with qtbot.waitSignal(board.nodeActivated, timeout=1000) as blocker:
        rect = board.executing.visualItemRect(board.executing.topLevelItem(0))
        QTest.mouseDClick(board.executing.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())

    payload = blocker.args[0]
    assert payload["kind"] == "message"
    assert payload["preview"]["step"]["voice_id"] == "voice-1"


def test_bundle_progress_is_independent_from_current_child_phase() -> None:
    from app.broadcast.plan import BroadcastPlan
    from gui.widgets.send_board_model import build_plan_node

    plan = BroadcastPlan.model_validate(
        {
            "contacts": [{"conv_local": "1", "display_name": "甲"}],
            "sequence": [
                {
                    "type": "bundle",
                    "bundle_id": "b1",
                    "bundle_name": "素材包",
                    "presentation": "expanded",
                    "items": [
                        {"type": "text", "body": "一"},
                        {"type": "text", "body": "二"},
                    ],
                    "item_gaps": [3],
                }
            ],
        }
    )
    node = build_plan_node(
        plan,
        title="测试",
        clock="10:00:00",
        states={
            (0, 0): {"status": "sent", "phase": ""},
            (0, 1): {"status": "before_send", "phase": "sending"},
        },
    )
    bundle = node.children[0].children[0]

    assert "1/2" in bundle.status
    assert bundle.status.startswith("素材包进度：")
    assert bundle.children[0].status == "发送成功"
    assert bundle.children[2].status == "70%"

    sending_state = {(0, 0): {"status": "sent", "phase": ""}, (0, 1): {"status": "before_send", "phase": "sending"}}
    preparing_state = {(0, 0): {"status": "sent", "phase": ""}, (0, 1): {"status": "before_send", "phase": "prepare"}}
    sending_bundle = build_plan_node(plan, title="测试", clock="10:00:00", states=sending_state).children[0].children[0]
    preparing_bundle = build_plan_node(plan, title="测试", clock="10:00:00", states=preparing_state).children[0].children[0]
    assert sending_bundle.status == preparing_bundle.status
    assert sending_bundle.children[2].status != preparing_bundle.children[2].status
    assert "70%" not in sending_bundle.status
    assert "30%" not in preparing_bundle.status


def test_bundle_progress_stays_zero_while_a_child_is_sending() -> None:
    from app.broadcast.plan import BroadcastPlan
    from gui.widgets.send_board_model import build_plan_node

    plan = BroadcastPlan.model_validate(
        {
            "contacts": [{"conv_local": "1", "display_name": "甲"}],
            "sequence": [
                {
                    "type": "bundle",
                    "bundle_id": "b1",
                    "bundle_name": "素材包",
                    "presentation": "expanded",
                    "items": [
                        {"type": "text", "body": "一"},
                        {"type": "text", "body": "二"},
                    ],
                    "item_gaps": [3],
                }
            ],
        }
    )
    node = build_plan_node(
        plan,
        title="测试",
        clock="10:00:00",
        states={(0, 1): {"status": "before_send", "phase": "sending"}},
    )
    bundle = node.children[0].children[0]
    assert bundle.status == "素材包进度：0/2 条素材"
    assert bundle.children[2].status == "70%"


def test_notice_plan_board_shows_body() -> None:
    from app.broadcast.plan import BroadcastPlan
    from gui.widgets.send_board_model import build_plan_node

    plan = BroadcastPlan.model_validate(
        {
            "contacts": [{"conv_local": "g1", "display_name": "测试群000"}],
            "sequence": [
                {"type": "notice", "body": "今晚开会"},
                {"type": "at", "body": "请看公告"},
            ],
            "step_gaps": [3],
        }
    )
    node = build_plan_node(plan, title="公告", clock="10:00:00", states={})
    titles = [child.title for child in node.children[0].children]
    assert "群公告 · 今晚开会" in titles[0]
    assert "@所有人 · 请看公告" in titles[2]


def test_atomic_bundle_children_use_library_names() -> None:
    from types import SimpleNamespace

    from app.broadcast.plan import BroadcastPlan
    from gui.widgets.send_board_model import build_plan_node

    plan = BroadcastPlan.model_validate(
        {
            "contacts": [{"conv_local": "1", "display_name": "甲"}],
            "sequence": [
                {
                    "type": "bundle",
                    "bundle_id": "b1",
                    "bundle_name": "素材包",
                    "presentation": "atomic",
                }
            ],
        }
    )
    bundle = SimpleNamespace(
        name="素材包",
        items=[
            SimpleNamespace(asset_type="channel", item_id="ch-1"),
            SimpleNamespace(asset_type="doc", item_id="doc-1"),
        ],
        step_gaps=[3],
    )
    names = {"channel": "春季视频", "doc": "产品说明"}
    node = build_plan_node(
        plan,
        title="测试",
        clock="10:00:00",
        load_bundle=lambda _bundle_id: bundle,
        name_of=lambda kind, _item_id, _path: names.get(kind, ""),
    )
    titles: list[str] = []

    def walk(item) -> None:
        titles.append(item.title)
        for child in item.children:
            walk(child)

    walk(node)
    assert any("春季视频" in title for title in titles)
    assert any("产品说明" in title for title in titles)


def test_restart_button_stays_enabled_without_a_running_task(qtbot) -> None:
    from PySide6.QtWidgets import QAbstractItemView

    board = SendBoard()
    qtbot.addWidget(board)
    board.set_running(False, paused=False, can_retry=False, can_restart=False)
    assert board.restart_button.isEnabled()
    assert board.executing.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
