"""计划名称、对象间隔和发送时间。编辑消息页与群公告页共用同一套模式。"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QTime, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.pending_plans import delay_label
from app.broadcast.plan import DEFAULT_CONTACT_GAP_FLAT
from app.broadcast.schedule_spec import AfterSpec, AtSpec, CronSpec, ImmediateSpec, ScheduleSpec
from app.broadcast.scheduler import cron_expr_label


class ScheduleSettings(QGroupBox):
    """参数与调度。间隔行的标题由调用方传入（联系人间隔 / 群聊间隔）。"""

    submitRequested = Signal()

    def __init__(self, *, gap_label: str, gap_tooltip: str) -> None:
        super().__init__("参数与调度")
        self._build_ui(gap_label=gap_label, gap_tooltip=gap_tooltip)
        self._bind_events()
        self._on_schedule_mode_changed(self._schedule_mode.currentIndex())
        self._on_cycle_mode_changed(self._cycle_mode.currentText())

    def _build_ui(self, *, gap_label: str, gap_tooltip: str) -> None:
        params_layout = QVBoxLayout(self)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("计划名称"))
        self._plan_name = QLineEdit()
        self._plan_name.setPlaceholderText("可留空，留空时用调度说明")
        name_row.addWidget(self._plan_name, 1)
        params_layout.addLayout(name_row)

        gap_row = QHBoxLayout()
        gap_row.addWidget(QLabel(gap_label))
        self._gap_spin = QSpinBox()
        self._gap_spin.setRange(1, 86400)
        self._gap_spin.setValue(int(DEFAULT_CONTACT_GAP_FLAT))
        self._gap_unit = QComboBox()
        self._gap_unit.addItems(["秒", "分", "时"])
        self._gap_unit_prev = "秒"
        self._gap_spin.setToolTip(gap_tooltip)
        gap_row.addWidget(self._gap_spin, 1)
        gap_row.addWidget(self._gap_unit)
        gap_row.addStretch(1)
        params_layout.addLayout(gap_row)

        schedule_group = QGroupBox("发送时间")
        schedule_form = QFormLayout(schedule_group)
        self._schedule_mode = QComboBox()
        self._schedule_mode.addItems(["立即执行", "N 时间后", "具体时刻", "循环"])
        self._schedule_stack = QStackedWidget()
        self._schedule_stack.addWidget(QWidget())

        delay_page = QWidget()
        delay_row = QHBoxLayout(delay_page)
        delay_row.setContentsMargins(0, 0, 0, 0)
        self._delay_value = QSpinBox()
        self._delay_value.setRange(1, 1440)
        self._delay_value.setValue(5)
        self._delay_unit = QComboBox()
        self._delay_unit.addItems(["秒", "分钟", "小时"])
        delay_row.addWidget(self._delay_value)
        delay_row.addWidget(self._delay_unit)
        delay_row.addStretch(1)
        self._schedule_stack.addWidget(delay_page)

        at_page = QWidget()
        at_row = QHBoxLayout(at_page)
        at_row.setContentsMargins(0, 0, 0, 0)
        self._run_date = QDateEdit()
        self._run_date.setDisplayFormat("yyyy-MM-dd")
        self._run_date.setCalendarPopup(True)
        self._run_date.setDate(QDate.currentDate())
        self._run_date.setMinimumDate(QDate.currentDate())
        self._run_date.setMinimumWidth(128)
        self._run_clock = QTimeEdit()
        self._run_clock.setDisplayFormat("HH:mm:ss")
        self._run_clock.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self._run_clock.setTime(QTime.currentTime())
        self._run_clock.setMinimumWidth(118)
        self._run_clock.setToolTip("先点中小时、分钟或秒，再用右侧箭头或键盘修改")
        at_row.addWidget(self._run_date)
        at_row.addWidget(self._run_clock)
        at_row.addStretch(1)
        self._schedule_stack.addWidget(at_page)

        cycle_page = QWidget()
        cycle_row = QHBoxLayout(cycle_page)
        cycle_row.setContentsMargins(0, 0, 0, 0)
        self._cycle_mode = QComboBox()
        self._cycle_mode.addItems(["每天", "每周"])
        self._cycle_weekday = QComboBox()
        self._cycle_weekday.addItems(["周一", "周二", "周三", "周四", "周五", "周六", "周日"])
        self._cycle_time = QTimeEdit()
        self._cycle_time.setDisplayFormat("HH:mm")
        cycle_row.addWidget(self._cycle_mode)
        cycle_row.addWidget(self._cycle_weekday)
        cycle_row.addWidget(self._cycle_time)
        cycle_row.addStretch(1)
        self._schedule_stack.addWidget(cycle_page)

        schedule_form.addRow("模式", self._schedule_mode)
        schedule_form.addRow("配置", self._schedule_stack)
        params_layout.addWidget(schedule_group)

        self._btn_submit = QPushButton("开始执行")
        params_layout.addWidget(self._btn_submit)
        params_layout.addStretch(1)

    def _bind_events(self) -> None:
        self._schedule_mode.currentIndexChanged.connect(self._on_schedule_mode_changed)
        self._cycle_mode.currentTextChanged.connect(self._on_cycle_mode_changed)
        self._gap_unit.currentTextChanged.connect(self._on_gap_unit_changed)
        self._delay_unit.currentTextChanged.connect(self._on_delay_unit_changed)
        self._btn_submit.clicked.connect(self.submitRequested)

    @property
    def submit_button(self) -> QPushButton:
        return self._btn_submit

    def gap_seconds(self) -> float:
        value = max(1, int(self._gap_spin.value()))
        unit = self._gap_unit.currentText()
        if unit == "分":
            return float(value * 60)
        if unit == "时":
            return float(value * 3600)
        return float(value)

    def typed_name(self) -> str:
        return self._plan_name.text().strip()

    def schedule_identity(self) -> tuple[str, str]:
        mode = self._schedule_mode.currentText()
        if mode == "立即执行":
            return "immediate", "立即执行"
        if mode == "N 时间后":
            return "after", self.after_label()
        if mode == "具体时刻":
            return "at", self._selected_run_at().strftime("%Y-%m-%d %H:%M:%S")
        return "cron", cron_expr_label(self._build_cycle_expr())

    def fallback_plan_name(self) -> str:
        mode = self._schedule_mode.currentText()
        if mode == "立即执行":
            return f"（立即执行）{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        if mode == "N 时间后":
            delay = self.after_label()
            target = datetime.fromtimestamp(datetime.now().timestamp() + self._delay_seconds())
            return f"（{delay}）{target.strftime('%Y-%m-%d %H:%M:%S')}"
        if mode == "具体时刻":
            return f"（具体时刻）{self._selected_run_at().strftime('%Y-%m-%d %H:%M:%S')}"
        return f"（循环任务）{cron_expr_label(self._build_cycle_expr())}"

    def after_label(self) -> str:
        return delay_label(int(self._delay_value.value()), self._delay_unit.currentText())

    def build_spec(self) -> ScheduleSpec:
        mode = self._schedule_mode.currentText()
        if mode == "立即执行":
            return ImmediateSpec()
        if mode == "N 时间后":
            return AfterSpec(delay_seconds=self._delay_seconds())
        if mode == "具体时刻":
            self._sync_run_at_floor()
            when = self._selected_run_at()
            now = datetime.now().replace(microsecond=0)
            if when < now:
                when = now
                self._set_run_at(when)
            return AtSpec(run_at=when.isoformat(timespec="seconds"))
        return CronSpec(expr=self._build_cycle_expr())

    def _on_schedule_mode_changed(self, index: int) -> None:
        self._schedule_stack.setCurrentIndex(max(0, min(index, self._schedule_stack.count() - 1)))
        self._btn_submit.setText("开始执行" if index == 0 else "提交调度")
        if self._schedule_mode.currentText() == "具体时刻":
            self._sync_run_at_floor()

    def _on_cycle_mode_changed(self, text: str) -> None:
        self._cycle_weekday.setVisible(text == "每周")

    def _on_gap_unit_changed(self, unit: str) -> None:
        old = self._gap_unit_prev
        new = str(unit or "").strip() or "秒"
        seconds = self._gap_amount_to_seconds(int(self._gap_spin.value()), old)
        self._gap_unit_prev = new
        shown = self._seconds_to_gap_amount(seconds, new)
        lo, hi = {"秒": (1, 86400), "分": (1, 1440), "时": (1, 168)}.get(new, (1, 86400))
        self._gap_spin.blockSignals(True)
        self._gap_spin.setRange(lo, hi)
        self._gap_spin.setValue(min(hi, max(lo, shown)))
        self._gap_spin.blockSignals(False)

    def _on_delay_unit_changed(self, unit: str) -> None:
        if unit == "秒":
            self._delay_value.setRange(1, 86400)
        elif unit == "分钟":
            self._delay_value.setRange(1, 1440)
        else:
            self._delay_value.setRange(1, 24)

    def _delay_seconds(self) -> float:
        base = float(self._delay_value.value())
        unit = self._delay_unit.currentText()
        if unit == "小时":
            return base * 3600.0
        if unit == "分钟":
            return base * 60.0
        return base

    def _build_cycle_expr(self) -> str:
        hhmm = self._cycle_time.time().toString("HH:mm")
        if self._cycle_mode.currentText() == "每天":
            return f"daily@{hhmm}"
        index = self._cycle_weekday.currentIndex() + 1
        return f"weekly@{index}/{hhmm}"

    def _selected_run_at(self) -> datetime:
        date = self._run_date.date()
        clock = self._run_clock.time()
        return datetime(date.year(), date.month(), date.day(), clock.hour(), clock.minute(), clock.second())

    def _set_run_at(self, when: datetime) -> None:
        self._run_date.setDate(QDate(when.year, when.month, when.day))
        self._run_clock.setTime(QTime(when.hour, when.minute, when.second))

    def _sync_run_at_floor(self) -> None:
        """日期不能早于今天。组合后的时刻若已过去，拉回当前时间。"""
        today = QDate.currentDate()
        self._run_date.setMinimumDate(today)
        if self._run_date.date() < today:
            self._run_date.setDate(today)
        now = datetime.now().replace(microsecond=0)
        if self._selected_run_at() < now:
            self._set_run_at(now)

    @staticmethod
    def _gap_amount_to_seconds(amount: int, unit: str) -> int:
        if unit == "分":
            return max(1, int(amount) * 60)
        if unit == "时":
            return max(1, int(amount) * 3600)
        return max(1, int(amount))

    @staticmethod
    def _seconds_to_gap_amount(seconds: int, unit: str) -> int:
        if unit == "分":
            return max(1, int(round(seconds / 60)))
        if unit == "时":
            return max(1, int(round(seconds / 3600)))
        return max(1, int(seconds))
