"""@和群公告发布页：外部群 + 公告文字 + 可选 @所有人 + 调度。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.notice_plan import build_notice_plan
from app.broadcast.plan import BroadcastPlan
from app.broadcast.schedule_spec import ScheduleSpec
from gui.tabs.group_picker import GroupPicker
from gui.widgets.schedule_settings import ScheduleSettings

SubmitPlan = Callable[[BroadcastPlan, ScheduleSpec, str], None]


class NoticePublisherPanel(QWidget):
    """左侧选外部群，右侧写群公告，勾选后同一轮再 @所有人。"""

    def __init__(self, *, submit_plan: SubmitPlan | None = None) -> None:
        super().__init__()
        self._submit_plan = submit_plan
        self._account_id = "default"
        self._build_ui()
        self._bind_events()
        self._update_submit_enabled()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        top = QHBoxLayout()

        groups_box = QGroupBox("群聊")
        groups_layout = QVBoxLayout(groups_box)
        self._groups = GroupPicker()
        groups_layout.addWidget(self._groups, 1)

        announce_box = QGroupBox("公告发布")
        announce_layout = QVBoxLayout(announce_box)
        announce_layout.addWidget(QLabel("公告文字编辑"))
        self._notice_edit = QPlainTextEdit()
        self._notice_edit.setPlaceholderText("输入要发布的群公告")
        announce_layout.addWidget(self._notice_edit, 2)

        self._at_check = QCheckBox("@所有人")
        self._at_check.setToolTip("勾选后，发布群公告之后再发一条 @所有人 消息")
        announce_layout.addWidget(self._at_check)

        self._at_box = QWidget()
        at_layout = QVBoxLayout(self._at_box)
        at_layout.setContentsMargins(0, 0, 0, 0)
        at_layout.addWidget(QLabel("@所有人文字"))
        self._at_edit = QPlainTextEdit()
        self._at_edit.setPlaceholderText("输入 @所有人 时一起发送的文字")
        at_layout.addWidget(self._at_edit, 1)
        self._at_box.hide()
        announce_layout.addWidget(self._at_box, 1)

        self._schedule = ScheduleSettings(
            gap_label="群聊间隔",
            gap_tooltip="发完一个群的公告和 @所有人 后，再隔多久发下一个群",
        )

        right = QVBoxLayout()
        right.addWidget(announce_box, 3)
        right.addWidget(self._schedule, 1)
        top.addWidget(groups_box, 1)
        top.addLayout(right, 1)
        root.addLayout(top, 1)

    def _bind_events(self) -> None:
        self._groups.selectedGroupsChanged.connect(lambda _groups: self._update_submit_enabled())
        self._notice_edit.textChanged.connect(self._update_submit_enabled)
        self._at_check.toggled.connect(self._on_at_toggled)
        self._at_edit.textChanged.connect(self._update_submit_enabled)
        self._schedule.submitRequested.connect(self._on_submit)

    def set_account(self, account_id: str) -> None:
        account_id = account_id.strip() or "default"
        if account_id == self._account_id:
            return
        self._account_id = account_id
        self._notice_edit.clear()
        self._at_check.setChecked(False)
        self._at_edit.clear()

    def set_contact_rows(self, rows: object) -> None:
        self._groups.set_rows(rows)

    def set_refreshing(self, refreshing: bool) -> None:
        self._groups.set_refreshing(bool(refreshing))

    @property
    def refreshRequested(self):
        return self._groups.refreshRequested

    def _on_at_toggled(self, checked: bool) -> None:
        self._at_box.setVisible(bool(checked))
        self._update_submit_enabled()

    def _update_submit_enabled(self) -> None:
        ready = bool(self._groups.selected_groups()) and bool(self._notice_edit.toPlainText().strip())
        if self._at_check.isChecked():
            ready = ready and bool(self._at_edit.toPlainText().strip())
        self._schedule.submit_button.setEnabled(ready)

    def _on_submit(self) -> None:
        if self._submit_plan is None:
            QMessageBox.warning(self, "无法执行", "调度还未就绪。")
            return
        schedule_mode, schedule_label = self._schedule.schedule_identity()
        name = self._schedule.typed_name() or self._schedule.fallback_plan_name()
        try:
            plan = build_notice_plan(
                groups=self._groups.selected_groups(),
                notice_body=self._notice_edit.toPlainText(),
                at_enabled=self._at_check.isChecked(),
                at_body=self._at_edit.toPlainText(),
                contact_gap=self._schedule.gap_seconds(),
                name=name,
                schedule_mode=schedule_mode,
                schedule_label=schedule_label,
            )
            spec = self._schedule.build_spec()
        except ValueError as exc:
            QMessageBox.warning(self, "计划不可用", str(exc))
            return
        self._submit_plan(plan, spec, after_label=self._schedule.after_label())
