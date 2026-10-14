"""@和群公告发布页：只列外部群，勾选后带上 @所有人，并走原有执行通道。"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt

from app.broadcast.dto import ContactRowDTO, PlanContact
from app.broadcast.notice_plan import build_notice_plan
from app.broadcast.schedule_spec import ImmediateSpec
from gui.main_window import MainWindow
from gui.tabs.group_picker import GroupPicker
from gui.tabs.notice_publisher import NoticePublisherPanel
from gui.tabs.plan_composer import PlanComposerPanel


def _row(conv_local: str, name: str, category: str) -> ContactRowDTO:
    return ContactRowDTO(
        conv_local=conv_local,
        display_name=name,
        peer_name=name,
        category=category,
    )


def _groups() -> list[ContactRowDTO]:
    return [
        _row("c1", "海鸟与鱼-老客", "外部客户"),
        _row("g1", "测试群000", "外部群"),
        _row("g2", "群组123", "外部群"),
        _row("i1", "内部同事甲", "内部同事"),
    ]


def test_group_picker_keeps_external_groups_only(qtbot) -> None:
    picker = GroupPicker()
    qtbot.addWidget(picker)
    picker.set_rows(_groups())

    assert picker._table.columnCount() == 2
    headers = [picker._table.horizontalHeaderItem(i).text() for i in range(2)]
    assert headers == ["", "群聊名称"]
    assert [picker._table.item(i, 1).text() for i in range(picker._table.rowCount())] == ["测试群000", "群组123"]
    assert picker._checked_count.text() == "已勾选：0个"

    picker._table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert picker._checked_count.text() == "已勾选：1个"
    assert [item.display_name for item in picker.selected_groups()] == ["测试群000"]

    picker._search.setText("群组")
    assert picker._table.rowCount() == 1
    assert picker._table.item(0, 1).text() == "群组123"

    picker._on_confirm()
    picker._search.clear()
    assert [picker._table.item(i, 1).text() for i in range(picker._table.rowCount())] == ["测试群000"]

    picker._on_reselect()
    assert picker._table.rowCount() == 2
    assert picker._checked_count.text() == "已勾选：0个"


def test_at_editor_appears_when_checked(qtbot) -> None:
    panel = NoticePublisherPanel()
    qtbot.addWidget(panel)
    assert panel._at_box.isHidden()
    assert panel._schedule.submit_button.text() == "开始执行"
    assert panel._schedule.submit_button.isEnabled() is False

    panel._at_check.setChecked(True)
    assert panel._at_box.isHidden() is False
    panel._schedule._schedule_mode.setCurrentIndex(1)
    assert panel._schedule.submit_button.text() == "提交调度"


def test_submit_sends_notice_then_at(qtbot) -> None:
    seen: dict[str, object] = {}

    def _submit(plan, spec, after_label: str) -> None:
        seen["plan"] = plan
        seen["spec"] = spec
        seen["after_label"] = after_label

    panel = NoticePublisherPanel(submit_plan=_submit)
    qtbot.addWidget(panel)
    panel.set_contact_rows(_groups())
    panel._groups._table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    panel._notice_edit.setPlainText("今晚 8 点开会")
    panel._at_check.setChecked(True)
    panel._at_edit.setPlainText("请大家看公告")
    panel._schedule._gap_spin.setValue(2)
    assert panel._schedule.submit_button.isEnabled() is True

    panel._on_submit()
    plan = seen["plan"]
    assert [step.type for step in plan.sequence] == ["notice", "at"]
    assert plan.sequence[0].body == "今晚 8 点开会"
    assert plan.sequence[1].body == "请大家看公告"
    assert [item.display_name for item in plan.contacts] == ["群组123"]
    assert plan.contact_gap == 2
    assert seen["spec"].mode == "immediate"


def test_notice_page_submit_reaches_composer_run(qtbot) -> None:
    composer = PlanComposerPanel()
    qtbot.addWidget(composer)
    panel = NoticePublisherPanel(submit_plan=composer.submit_prepared_plan)
    qtbot.addWidget(panel)
    received: list[object] = []
    composer.runPlanRequested.connect(received.append)
    panel.set_contact_rows(_groups())
    panel._groups._table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    panel._notice_edit.setPlainText("今晚开会")
    panel._at_check.setChecked(True)
    panel._at_edit.setPlainText("请看公告")

    panel._on_submit()

    assert len(received) == 1
    plan = received[0]
    assert [step.type for step in plan.sequence] == ["notice", "at"]
    assert plan.sequence[0].body == "今晚开会"
    assert plan.sequence[1].body == "请看公告"
    assert plan.contacts[0].conv_local == "g1"
    assert plan.contacts[0].display_name == "测试群000"


def test_prepared_plan_reaches_run_signal(qtbot) -> None:
    composer = PlanComposerPanel()
    qtbot.addWidget(composer)
    received: list[object] = []
    composer.runPlanRequested.connect(received.append)
    plan = build_notice_plan(
        groups=[PlanContact(conv_local="g1", display_name="测试群000")],
        notice_body="公告",
        at_enabled=False,
        at_body="",
        contact_gap=1,
        name="公告",
        schedule_mode="immediate",
        schedule_label="立即执行",
    )
    composer.submit_prepared_plan(plan, ImmediateSpec(), after_label="")
    assert received
    assert received[0].name == "公告"
    assert received[0].sequence[0].type == "notice"


def test_main_window_lists_notice_tab_and_external_groups(qtbot, monkeypatch) -> None:
    monkeypatch.setattr("gui.main_window.MainWindow._post_startup_tasks", lambda self: None)
    monkeypatch.setattr("gui.main_window.AppTrayIcon.is_available", staticmethod(lambda: False))
    monkeypatch.setattr(
        "gui.tabs.contacts_tab.ContactsTab.refresh_contacts",
        lambda self, *, force_refresh, show_error=True: None,
    )
    window = MainWindow()
    qtbot.addWidget(window)
    titles = [window._tabs.tabText(i) for i in range(window._tabs.count())]
    assert titles.index("编辑和发送消息") < titles.index("@和群公告发布") < titles.index("执行结果")

    contacts = window._composer_tab._contacts_panel
    contacts._rows = _groups()
    contacts._rebuild()
    table = window._notice_tab._groups._table
    assert table.rowCount() == 2
    assert [table.item(i, 1).text() for i in range(2)] == ["测试群000", "群组123"]
