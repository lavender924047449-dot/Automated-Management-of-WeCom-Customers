from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
import uuid

from loguru import logger
from PySide6.QtCore import QDate, QObject, QTime, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.asset_bundles import canon_asset_type
from app.broadcast.asset_names import sequence_step_removed
from app.broadcast.asset_validator import validate_step_payload
from app.broadcast.dto import AssetRowDTO, PlanContact
from app.broadcast.plan import DEFAULT_CONTACT_GAP_FLAT, DEFAULT_STEP_GAP, BroadcastPlan
from app.broadcast.pending_plans import delay_label
from app.broadcast.preview import PreviewService
from app.broadcast.schedule_spec import AfterSpec, AtSpec, CronSpec, ImmediateSpec, ScheduleSpec
from app.broadcast.scheduler import BroadcastScheduler, ScheduleEntry, cron_expr_label, next_cron_fire
from gui.asset_order import newest_first
from gui.copy_support import add_copy_action
from gui.i18n import ASSET_TYPE_FILTER_OPTIONS, asset_type_from_filter, asset_type_to_zh
from gui.tabs.contacts_tab import ContactsTab
from gui.widgets.schedule_confirm_dialog import ScheduleConfirmDialog

def _asset_steps(row: dict[str, Any]) -> list:
    extra = row.get("extra")
    if not isinstance(extra, dict):
        return []
    steps = extra.get("steps")
    return list(steps) if isinstance(steps, list) else []


_VALIDATION_ROLE = Qt.ItemDataRole.UserRole + 2
_VALIDATION_ERROR_ROLE = Qt.ItemDataRole.UserRole + 3
_SEQUENCE_DESC_ROLE = Qt.ItemDataRole.UserRole + 4


class _ValidateWorker(QObject):
    finished = Signal(int, bool, str)

    def __init__(self, row: int, payload: dict, account_id: str) -> None:
        super().__init__()
        self._row = row
        self._payload = dict(payload)
        self._account_id = account_id

    @Slot()
    def run(self) -> None:
        try:
            ok, err = validate_step_payload(self._payload, account_id=self._account_id)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(self._row, False, f"校验异常：{exc}")
            return
        self.finished.emit(self._row, bool(ok), str(err or ""))


class PlanComposerPanel(QWidget):
    """Step 4: 编辑和发送消息页（联系人 + 序列 + 参数/调度）。"""

    runPlanRequested = Signal(object)
    scheduledPlanPending = Signal(str)
    pendingPlansChanged = Signal()
    _scheduleTriggered = Signal(object)
    scheduleDialogAboutToShow = Signal()
    contactsSnapshotChanged = Signal(object)
    contactsRefreshStateChanged = Signal(bool)

    def __init__(
        self,
        *,
        scheduler_provider: Callable[[], BroadcastScheduler] | None = None,
        preview_service_provider: Callable[[], PreviewService] | None = None,
    ) -> None:
        super().__init__()
        self._account_id = "default"
        self._selected_contacts: dict[str, PlanContact] = {}
        self._asset_rows: list[dict[str, Any]] = []
        self._scheduler_provider = scheduler_provider
        self._preview_service_provider = preview_service_provider
        self._validate_jobs: dict[int, tuple[QThread, _ValidateWorker]] = {}
        self._pending_timers: dict[str, QTimer] = {}
        self._build_ui()
        self._bind_events()
        self._refresh_asset_picker()
        self._update_submit_enabled()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        top = QHBoxLayout()

        contacts_group = QGroupBox("联系人")
        contacts_layout = QVBoxLayout(contacts_group)
        self._contacts_panel = ContactsTab()
        contacts_layout.addWidget(self._contacts_panel, 1)

        sequence_group = QGroupBox("素材序列（可拖拽排序）")
        sequence_layout = QVBoxLayout(sequence_group)
        add_row = QHBoxLayout()
        self._asset_type_filter = QComboBox()
        self._asset_type_filter.addItems(ASSET_TYPE_FILTER_OPTIONS)
        # 与素材库对齐的搜索输入：按名字子串过滤下方素材下拉
        self._asset_search = QLineEdit()
        self._asset_search.setPlaceholderText("搜索素材（名字子串）")
        self._asset_search.setClearButtonEnabled(True)
        self._asset_pick = QComboBox()
        self._btn_add_asset = QPushButton("添加素材步骤")
        add_row.addWidget(QLabel("类型"))
        add_row.addWidget(self._asset_type_filter)
        add_row.addWidget(self._asset_search, 1)
        add_row.addWidget(self._asset_pick, 2)
        add_row.addWidget(self._btn_add_asset)
        sequence_layout.addLayout(add_row)

        self._sequence = QListWidget()
        self._sequence.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._sequence.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._sequence.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._sequence.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        sequence_layout.addWidget(self._sequence, 1)
        seq_actions = QHBoxLayout()
        self._btn_del = QPushButton("删除选中")
        self._btn_clear = QPushButton("清空序列")
        self._btn_retry_validate = QPushButton("重试校验")
        self._btn_retry_validate.setEnabled(False)
        seq_actions.addWidget(self._btn_del)
        seq_actions.addWidget(self._btn_clear)
        seq_actions.addWidget(self._btn_retry_validate)
        seq_actions.addStretch(1)
        sequence_layout.addLayout(seq_actions)

        params_group = QGroupBox("参数与调度")
        params_layout = QVBoxLayout(params_group)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("计划名称"))
        self._plan_name = QLineEdit()
        self._plan_name.setPlaceholderText("可留空，留空时用调度说明")
        name_row.addWidget(self._plan_name, 1)
        params_layout.addLayout(name_row)

        # 联系人间隔（全局）。计划里仍按秒保存，界面可选秒 / 分 / 时。
        contact_gap_row = QHBoxLayout()
        contact_gap_row.addWidget(QLabel("联系人间隔"))
        self._contact_gap_spin = QSpinBox()
        self._contact_gap_spin.setRange(1, 86400)
        self._contact_gap_spin.setValue(int(DEFAULT_CONTACT_GAP_FLAT))
        self._contact_gap_unit = QComboBox()
        self._contact_gap_unit.addItems(["秒", "分", "时"])
        self._contact_gap_unit_prev = "秒"
        self._contact_gap_spin.setToolTip("发完一个联系人的全部消息后，再隔多久发下一个人")
        contact_gap_row.addWidget(self._contact_gap_spin, 1)
        contact_gap_row.addWidget(self._contact_gap_unit)
        contact_gap_row.addStretch(1)
        params_layout.addLayout(contact_gap_row)

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
        # 日期和时刻分开。合在一个带日历的框里时，时分秒会被挤掉，
        # 而且下限钉在「此刻」会让分、秒的每一次改动都被弹回。
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

        right = QVBoxLayout()
        right.addWidget(sequence_group, 3)
        right.addWidget(params_group, 1)
        top.addWidget(contacts_group, 1)
        top.addLayout(right, 1)
        root.addLayout(top, 1)

    def _bind_events(self) -> None:
        self._contacts_panel.selectedContactsChanged.connect(self.set_contacts)
        self._contacts_panel.contactsSnapshotChanged.connect(self._relay_contact_rows)
        self._contacts_panel.refreshStateChanged.connect(self.contactsRefreshStateChanged)
        self._asset_type_filter.currentTextChanged.connect(self._refresh_asset_picker)
        self._asset_search.textChanged.connect(self._refresh_asset_picker)
        self._btn_add_asset.clicked.connect(self._add_selected_asset_step)
        self._btn_del.clicked.connect(self._delete_selected_sequence_item)
        self._btn_clear.clicked.connect(self._clear_sequence)
        self._btn_retry_validate.clicked.connect(self._retry_selected_validation)
        self._sequence.currentRowChanged.connect(self._on_sequence_selection_changed)
        self._sequence.customContextMenuRequested.connect(self._on_sequence_context_menu)
        self._sequence.itemDoubleClicked.connect(self._preview_sequence_item)
        self._schedule_mode.currentIndexChanged.connect(self._on_schedule_mode_changed)
        self._cycle_mode.currentTextChanged.connect(self._on_cycle_mode_changed)
        self._btn_submit.clicked.connect(self._on_submit)
        self._scheduleTriggered.connect(self._on_schedule_triggered)

        self._contact_gap_spin.valueChanged.connect(self._update_preview)
        self._contact_gap_unit.currentTextChanged.connect(self._on_contact_gap_unit_changed)

        for widget in [
            self._delay_value,
        ]:
            widget.editingFinished.connect(self._update_preview)
        self._delay_unit.currentIndexChanged.connect(self._update_preview)
        self._delay_unit.currentTextChanged.connect(self._on_delay_unit_changed)
        self._run_date.dateChanged.connect(self._update_preview)
        self._run_clock.timeChanged.connect(self._update_preview)
        self._cycle_mode.currentIndexChanged.connect(self._update_preview)
        self._cycle_weekday.currentIndexChanged.connect(self._update_preview)
        self._cycle_time.timeChanged.connect(self._update_preview)
        self._sequence.model().rowsMoved.connect(self._on_sequence_rows_moved)
        self._sequence.model().rowsMoved.connect(lambda *_: self._refresh_sequence_widgets())
        self._sequence.itemChanged.connect(lambda *_: self._update_preview())
        self._on_cycle_mode_changed(self._cycle_mode.currentText())
        self._on_schedule_mode_changed(self._schedule_mode.currentIndex())

    def set_account(self, account_id: str) -> None:
        self._stop_pending_timers()
        self._account_id = account_id.strip() or "default"
        self._stop_all_validations()
        self._selected_contacts.clear()
        self._sequence.clear()
        self._update_submit_enabled()
        self._contacts_panel.set_account(self._account_id)
        self._update_preview()
        self._restore_pending_timers()

    def reset_for_account(self) -> None:
        self.set_account(self._account_id)

    def set_contacts(self, contacts: list[PlanContact]) -> None:
        selected: dict[str, PlanContact] = {}
        for item in contacts:
            conv_local = str(item.conv_local).strip()
            display_name = str(item.display_name).strip()
            if not conv_local or not display_name:
                continue
            selected[conv_local] = PlanContact(conv_local=conv_local, display_name=display_name)
        self._selected_contacts = selected
        self._update_preview()

    def drop_removed_assets(self, removed: list, removed_bundle_ids: list | None = None) -> None:
        """素材库删除后，把发送序列里还没发出去的对应步骤和已删除的素材包节点拿掉。"""
        keys: set[tuple[str, str]] = set()
        paths: set[str] = set()
        bundle_ids = {str(item).strip() for item in (removed_bundle_ids or []) if str(item).strip()}
        for row in removed or []:
            if not row:
                continue
            kind = canon_asset_type(str(row[0]))
            item_id = str(row[1]).strip() if len(row) > 1 else ""
            path = str(row[2]).strip() if len(row) > 2 else ""
            if kind == "bundle" and item_id:
                bundle_ids.add(item_id)
                continue
            if kind and item_id:
                keys.add((kind, item_id))
            if path:
                paths.add(path)
        rows_to_drop: list[int] = []
        for index in range(self._sequence.count()):
            data = self._sequence.item(index).data(Qt.ItemDataRole.UserRole)
            if sequence_step_removed(data if isinstance(data, dict) else {}, keys, paths, bundle_ids):
                rows_to_drop.append(index)
        if not rows_to_drop:
            return
        self._delete_sequence_rows(rows_to_drop)
        self._update_preview()
        self._update_submit_enabled()

    def set_voice_entries(self, entries: list[Any]) -> None:
        assets: list[AssetRowDTO] = []
        for item in entries:
            voice_id = str(getattr(item, "id", "")).strip()
            if not voice_id:
                continue
            name = (
                str(getattr(item, "note", "")).strip()
                or str(getattr(item, "label", "")).strip()
                or str(getattr(item, "file", "")).strip()
                or "未命名"
            )
            assets.append(
                AssetRowDTO(
                    asset_type="voice",
                    item_id=voice_id,
                    name=name,
                )
            )
        self.set_library_assets(assets)

    def set_library_assets(self, rows: list[AssetRowDTO]) -> None:
        cleaned: list[dict[str, Any]] = []
        for row in rows:
            cleaned.append(row.model_dump(mode="json"))
        self._asset_rows = cleaned
        self._refresh_asset_picker()
        self._sync_sequence_display_names()

    def build_plan(self) -> BroadcastPlan:
        contacts = [
            item.model_dump(mode="json")
            for item in sorted(self._selected_contacts.values(), key=lambda x: x.display_name)
        ]
        sequence: list[dict[str, Any]] = []
        step_gaps: list[float] = []
        index = 0
        count = self._sequence.count()
        while index < count:
            data = self._step_data(index)
            row_type = str(data.get("_row_type", ""))
            if row_type == "gap" and not str(data.get("group_id", "")).strip():
                step_gaps.append(max(3.0, float(data.get("gap", DEFAULT_STEP_GAP))))
                index += 1
                continue
            if row_type == "bundle_header":
                group_id = str(data.get("group_id", "")).strip()
                presentation = str(data.get("presentation", "atomic")).strip() or "atomic"
                items: list[dict[str, Any]] = []
                item_gaps: list[float] = []
                index += 1
                while index < count:
                    child = self._step_data(index)
                    if str(child.get("group_id", "")).strip() != group_id:
                        break
                    child_type = str(child.get("_row_type", ""))
                    if child_type == "gap":
                        item_gaps.append(max(3.0, float(child.get("gap", DEFAULT_STEP_GAP))))
                    elif child_type == "bundle_child" and presentation == "expanded":
                        items.append(self._public_step_payload(child))
                    index += 1
                sequence.append(
                    {
                        "type": "bundle",
                        "bundle_id": str(data.get("bundle_id", "")).strip(),
                        "bundle_name": str(data.get("bundle_name", "")).strip(),
                        "presentation": "expanded" if presentation == "expanded" else "atomic",
                        "items": items,
                        "item_gaps": item_gaps if presentation == "expanded" else [],
                    }
                )
                continue
            if row_type == "gap":
                index += 1
                continue
            sequence.append(self._public_step_payload(data))
            index += 1
        typed_name = self._plan_name.text().strip()
        schedule_mode, schedule_label = self._schedule_identity()
        payload = {
            "contacts": contacts,
            "sequence": sequence,
            "step_gaps": step_gaps,
            "contact_gap": self._contact_gap_seconds(),
            "name": typed_name or self._fallback_plan_name(),
            "schedule_mode": schedule_mode,
            "schedule_label": schedule_label,
        }
        return BroadcastPlan.model_validate(payload)

    def _schedule_identity(self) -> tuple[str, str]:
        mode = self._schedule_mode.currentText()
        if mode == "立即执行":
            return "immediate", "立即执行"
        if mode == "N 时间后":
            return "after", delay_label(int(self._delay_value.value()), self._delay_unit.currentText())
        if mode == "具体时刻":
            return "at", self._selected_run_at().strftime("%Y-%m-%d %H:%M:%S")
        return "cron", cron_expr_label(self._build_cycle_expr())

    def _public_step_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        hidden = {"_row_type", "group_id", "library_bundle_id"}
        return {key: value for key, value in data.items() if key not in hidden}

    def _fallback_plan_name(self) -> str:
        mode = self._schedule_mode.currentText()
        if mode == "立即执行":
            return f"（立即执行）{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        if mode == "N 时间后":
            delay = delay_label(int(self._delay_value.value()), self._delay_unit.currentText())
            target = datetime.fromtimestamp(datetime.now().timestamp() + self._delay_seconds())
            return f"（{delay}）{target.strftime('%Y-%m-%d %H:%M:%S')}"
        if mode == "具体时刻":
            return f"（具体时刻）{self._selected_run_at().strftime('%Y-%m-%d %H:%M:%S')}"
        return f"（循环任务）{cron_expr_label(self._build_cycle_expr())}"
    def _contact_gap_seconds(self) -> float:
        value = max(1, int(self._contact_gap_spin.value()))
        unit = self._contact_gap_unit.currentText()
        if unit == "分":
            return float(value * 60)
        if unit == "时":
            return float(value * 3600)
        return float(value)

    def _on_contact_gap_unit_changed(self, unit: str) -> None:
        old = self._contact_gap_unit_prev
        new = str(unit or "").strip() or "秒"
        seconds = self._gap_amount_to_seconds(int(self._contact_gap_spin.value()), old)
        self._contact_gap_unit_prev = new
        shown = self._seconds_to_gap_amount(seconds, new)
        lo, hi = {"秒": (1, 86400), "分": (1, 1440), "时": (1, 168)}.get(new, (1, 86400))
        self._contact_gap_spin.blockSignals(True)
        self._contact_gap_spin.setRange(lo, hi)
        self._contact_gap_spin.setValue(min(hi, max(lo, shown)))
        self._contact_gap_spin.blockSignals(False)
        self._update_preview()

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

    def _step_data(self, row: int) -> dict[str, Any]:
        item = self._sequence.item(row)
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            raise ValueError("sequence 项数据非法")
        return dict(data)

    def _refresh_asset_picker(self) -> None:
        selected_type = asset_type_from_filter(self._asset_type_filter.currentText())
        keyword = self._asset_search.text().strip().lower()
        rows = []
        for row in self._asset_rows:
            asset_type = str(row.get("asset_type", "")).strip()
            if selected_type and asset_type != selected_type:
                continue
            if keyword:
                name = str(row.get("name", "")).lower()
                item_id = str(row.get("item_id", "")).lower()
                if keyword not in name and keyword not in item_id:
                    continue
            rows.append(row)
        rows = newest_first(rows)
        self._asset_pick.clear()
        chosen: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for row in rows:
            asset_type = str(row.get("asset_type", "")).strip()
            item_id = str(row.get("item_id", "")).strip()
            key = (asset_type, item_id)
            previous = chosen.get(key)
            if previous is None:
                chosen[key] = row
                order.append(key)
                continue
            if not _asset_steps(previous) and _asset_steps(row):
                chosen[key] = row
        for key in order:
            row = chosen[key]
            asset_type, item_id = key
            type_label = asset_type_to_zh(asset_type)
            name = str(row.get("name", "")).strip()
            if not name or name == item_id:
                name = "未命名"
            self._asset_pick.addItem(f"{type_label} · {name}", row)

    def _add_selected_asset_step(self) -> None:
        data = self._asset_pick.currentData()
        if not isinstance(data, dict):
            QMessageBox.information(self, "暂无可用素材", "当前筛选条件下没有可用素材，请先到素材库准备素材。")
            return
        asset_type = str(data.get("asset_type", "")).strip()
        if asset_type == "voice":
            voice_id = str(data.get("item_id", "")).strip()
            payload = {
                "type": "voice",
                "voice_id": voice_id,
                "display_name": self._public_asset_name(str(data.get("name", "")), voice_id),
            }
        elif asset_type == "text":
            payload = {
                "type": "text",
                "template_id": str(data.get("item_id", "")).strip(),
                "variant_pick": "random",
                "text_kind": "text",
                "display_name": str(data.get("name", "")).strip(),
            }
        elif asset_type == "image":
            payload = {
                "type": "image",
                "path": str(data.get("path", "")).strip(),
                "display_name": self._public_asset_name(
                    str(data.get("name", "")),
                    str(data.get("item_id", "")).strip(),
                ),
            }
        elif asset_type in {"oa", "doc", "channel"}:
            template_id = str(data.get("item_id", "")).strip()
            payload = {
                "type": asset_type,
                "template_id": template_id,
                "display_name": self._public_asset_name(str(data.get("name", "")), template_id),
            }
        elif asset_type == "wxa":
            template_id = str(data.get("item_id", "")).strip()
            payload = {
                "type": "wxa",
                "template_id": template_id,
                "thumb_path": str(data.get("thumb_path", "")).strip(),
                "display_name": self._public_asset_name(str(data.get("name", "")), template_id),
            }
        elif asset_type == "video":
            payload = {
                "type": "video",
                "path": str(data.get("path", "")).strip(),
                "thumb_path": str(data.get("thumb_path", "")).strip(),
                "display_name": self._public_asset_name(
                    str(data.get("name", "")),
                    str(data.get("item_id", "")).strip(),
                ),
            }
        elif asset_type == "bundle":
            bundle_id = str(data.get("item_id", "")).strip()
            bundle_name = self._public_asset_name(str(data.get("name", "")), bundle_id, empty_label="未命名素材包")
            mode, ok = QInputDialog.getItem(
                self,
                "选择素材包加入方式",
                f"素材包：{bundle_name}",
                ["展开为多个步骤", "作为一个原子节点"],
                0,
                False,
            )
            if not ok:
                return
            group_id = uuid.uuid4().hex
            presentation = "expanded" if mode == "展开为多个步骤" else "atomic"
            header = {
                "_row_type": "bundle_header",
                "group_id": group_id,
                "type": "bundle",
                "bundle_id": bundle_id,
                "bundle_name": bundle_name,
                "presentation": presentation,
            }
            if presentation == "expanded":
                extra = data.get("extra", {}) if isinstance(data.get("extra"), dict) else {}
                steps = list(extra.get("steps", []))
                if not isinstance(steps, list) or not steps:
                    QMessageBox.warning(self, "素材包不可用", "该素材包没有可展开步骤。")
                    return
                from app.broadcast.asset_bundles import get_bundle

                bundle = get_bundle(bundle_id, account_id=self._account_id)
                gaps = list(bundle.step_gaps) if bundle is not None else []
                self._append_sequence_item(header)
                for index, row in enumerate(steps):
                    if not isinstance(row, dict):
                        continue
                    child = dict(row)
                    child["_row_type"] = "bundle_child"
                    child["group_id"] = group_id
                    self._append_raw_sequence_item(child, validate=True)
                    if index < len(steps) - 1:
                        gap = float(gaps[index]) if index < len(gaps) else DEFAULT_STEP_GAP
                        self._append_raw_sequence_item(
                            {"_row_type": "gap", "gap": gap, "group_id": group_id},
                            validate=False,
                        )
                return
            self._append_sequence_item(header)
            return
        else:
            QMessageBox.information(self, "暂不支持", "该素材类型暂不支持加入发送计划。")
            return
        # bundle 原子节点跳过 schema 预校验（BroadcastPlan.model_validate），
        # 因为 bundle 的真正校验由异步 validate_step_payload 完成（会展开子项逐一检查）。
        if asset_type != "bundle":
            try:
                BroadcastPlan.model_validate(
                    {
                        "contacts": ["_check_"],
                        "sequence": [payload],
                    }
                )
            except Exception as exc:
                logger.warning("素材预校验失败 asset_type={} payload={} err={}", asset_type, payload, exc)
                QMessageBox.warning(self, "素材不可用", "该素材当前不可用，请重新选择或稍后重试。")
                return
        self._append_sequence_item(payload)

    def _append_sequence_item(self, payload: dict[str, Any]) -> None:
        # 如果列表非空且末尾不是 gap 行，先插入间隔行
        if self._sequence.count() > 0:
            last = self._sequence.item(self._sequence.count() - 1)
            last_data = last.data(Qt.ItemDataRole.UserRole) if last else None
            if isinstance(last_data, dict) and str(last_data.get("_row_type", "")) != "gap":
                self._insert_gap_item()
        validate = str(payload.get("_row_type", "")) != "bundle_header"
        self._append_raw_sequence_item(payload, validate=validate)

    def _append_raw_sequence_item(self, payload: dict[str, Any], *, validate: bool) -> None:
        item = QListWidgetItem()
        item.setText("")
        item.setData(Qt.ItemDataRole.UserRole, dict(payload))
        row_type = str(payload.get("_row_type", ""))
        if row_type in {"gap", "bundle_header"}:
            item.setData(_VALIDATION_ROLE, "ok")
        else:
            item.setData(_VALIDATION_ROLE, "pending")
        item.setData(_VALIDATION_ERROR_ROLE, "")
        item.setData(_SEQUENCE_DESC_ROLE, self._sequence_text(payload))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        row = self._sequence.count()
        self._sequence.addItem(item)
        self._render_sequence_item_widget(item)
        self._sequence.setCurrentItem(item)
        if validate and row_type != "gap":
            self._schedule_validation(row, dict(payload))
        self._update_preview()
        self._update_submit_enabled()

    def _delete_selected_sequence_item(self) -> None:
        rows = self._selected_sequence_rows()
        if not rows:
            return
        self._delete_sequence_rows(rows)
        self._update_preview()
        self._update_submit_enabled()

    def _clear_sequence(self) -> None:
        self._stop_all_validations()
        self._sequence.clear()
        self._update_preview()
        self._update_submit_enabled()

    def _selected_sequence_rows(self) -> list[int]:
        # QListWidgetItem 没有 .row() 方法；行号要通过所属的 QListWidget 反查。
        rows = sorted({self._sequence.row(item) for item in self._sequence.selectedItems()})
        if rows:
            return rows
        row = self._sequence.currentRow()
        return [row] if row >= 0 else []

    def _delete_sequence_rows(self, rows: list[int]) -> None:
        grouped = self._rows_with_bundle_groups(rows)
        for row in sorted(grouped, reverse=True):
            self._stop_validation(row)
            self._sequence.takeItem(row)
        # 修正间隔行：清除相邻重复 gap 行及首尾 gap 行
        self._normalize_sequence_gaps()

    def _rows_with_bundle_groups(self, rows: list[int]) -> list[int]:
        groups: set[str] = set()
        for row in rows:
            if row < 0 or row >= self._sequence.count():
                continue
            data = self._sequence.item(row).data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and str(data.get("_row_type", "")) == "bundle_header":
                group_id = str(data.get("group_id", "")).strip()
                if group_id:
                    groups.add(group_id)
        if not groups:
            return rows
        extra: list[int] = []
        for index in range(self._sequence.count()):
            data = self._sequence.item(index).data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and str(data.get("group_id", "")).strip() in groups:
                extra.append(index)
        return sorted(set(rows) | set(extra))

    def _on_sequence_rows_moved(self, *_args) -> None:
        QTimer.singleShot(0, self._repack_and_preview)

    def _repack_and_preview(self) -> None:
        self._repack_bundle_groups()
        self._update_preview()

    def _repack_bundle_groups(self) -> None:
        """子步骤被拖出素材包时，整组重新贴回标题下面。"""
        rows: list[dict[str, Any]] = []
        for index in range(self._sequence.count()):
            data = self._sequence.item(index).data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                rows.append(dict(data))
        if not any(str(row.get("group_id", "")).strip() for row in rows):
            return
        groups: dict[str, dict[str, Any]] = {}
        top: list[Any] = []
        for data in rows:
            group_id = str(data.get("group_id", "")).strip()
            row_type = str(data.get("_row_type", ""))
            if row_type == "bundle_header" and group_id:
                bucket = groups.setdefault(group_id, {"header": None, "rest": []})
                bucket["header"] = data
                if not any(item == ("group", group_id) for item in top):
                    top.append(("group", group_id))
                continue
            if group_id:
                bucket = groups.setdefault(group_id, {"header": None, "rest": []})
                bucket["rest"].append(data)
                if bucket["header"] is None and not any(item == ("group", group_id) for item in top):
                    top.append(("group", group_id))
                continue
            top.append(("row", data))
        rebuilt: list[dict[str, Any]] = []
        for kind, payload in top:
            if kind == "row":
                rebuilt.append(payload)
                continue
            bucket = groups.get(str(payload), {})
            header = bucket.get("header")
            if isinstance(header, dict):
                rebuilt.append(header)
            for child in bucket.get("rest", []):
                rebuilt.append(child)
        if rebuilt == rows:
            return
        self._sequence.blockSignals(True)
        self._sequence.clear()
        for data in rebuilt:
            self._append_raw_sequence_item(data, validate=str(data.get("_row_type", "")) not in {"gap", "bundle_header"})
        self._sequence.blockSignals(False)
        self._normalize_sequence_gaps()

    def _insert_gap_item(self, gap: float = DEFAULT_STEP_GAP) -> None:
        """在序列列表末尾插入间隔设置行。"""
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, {"_row_type": "gap", "gap": gap})
        item.setData(_VALIDATION_ROLE, "ok")  # gap 行不需要校验
        item.setData(_VALIDATION_ERROR_ROLE, "")
        item.setData(_SEQUENCE_DESC_ROLE, "")
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._sequence.addItem(item)
        widget = self._make_gap_widget(item, gap)
        self._sequence.setItemWidget(item, widget)

    def _make_gap_widget(self, item: QListWidgetItem, gap: float = DEFAULT_STEP_GAP) -> QWidget:
        """创建间隔行内嵌 widget：标签 + QSpinBox。"""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(28 if str((item.data(Qt.ItemDataRole.UserRole) or {}).get("group_id", "")) else 8, 2, 8, 2)
        label = QLabel("⏱ 间隔")
        label.setStyleSheet("color: #888; font-style: italic;")
        spin = QSpinBox()
        spin.setRange(3, 3600)
        spin.setValue(max(3, int(gap)))
        spin.setSuffix(" 秒")
        layout.addWidget(label)
        layout.addWidget(spin, 1)
        layout.addStretch(1)
        spin.valueChanged.connect(lambda v, it=item: self._on_gap_spin_changed(it, v))
        return w

    def _on_gap_spin_changed(self, item: QListWidgetItem, value: int) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            data["gap"] = float(value)
            item.setData(Qt.ItemDataRole.UserRole, data)
            bundle_id = str(data.get("library_bundle_id", "")).strip()
            group_id = str(data.get("group_id", "")).strip()
            if bundle_id and group_id:
                self._persist_atomic_bundle_gaps(bundle_id, group_id)
        self._update_preview()

    def _normalize_sequence_gaps(self) -> None:
        """移除首尾 gap 行及连续重复 gap 行。"""
        changed = True
        while changed:
            changed = False
            count = self._sequence.count()
            if count == 0:
                break
            first = self._sequence.item(0)
            if first:
                d = first.data(Qt.ItemDataRole.UserRole)
                if isinstance(d, dict) and str(d.get("_row_type", "")) == "gap":
                    self._sequence.takeItem(0)
                    changed = True
                    continue
            count = self._sequence.count()
            if count > 0:
                last = self._sequence.item(count - 1)
                if last:
                    d = last.data(Qt.ItemDataRole.UserRole)
                    if isinstance(d, dict) and str(d.get("_row_type", "")) == "gap":
                        self._sequence.takeItem(count - 1)
                        changed = True
                        continue
            for i in range(self._sequence.count() - 1):
                d1 = self._sequence.item(i).data(Qt.ItemDataRole.UserRole)
                d2 = self._sequence.item(i + 1).data(Qt.ItemDataRole.UserRole)
                if (isinstance(d1, dict) and str(d1.get("_row_type", "")) == "gap"
                        and isinstance(d2, dict) and str(d2.get("_row_type", "")) == "gap"):
                    self._sequence.takeItem(i + 1)
                    changed = True
                    break

    def _on_sequence_context_menu(self, pos) -> None:
        row = self._sequence.indexAt(pos).row()
        if row < 0:
            return
        rows = self._selected_sequence_rows()
        if not rows:
            return
        menu = QMenu(self)
        add_copy_action(menu, self._sequence)
        action = menu.addAction(f"删除选中步骤（{len(rows)} 条）")
        chosen = menu.exec(self._sequence.viewport().mapToGlobal(pos))
        if chosen != action:
            return
        self._delete_sequence_rows(rows)
        self._update_preview()
        self._update_submit_enabled()

    def _on_schedule_mode_changed(self, index: int) -> None:
        self._schedule_stack.setCurrentIndex(max(0, min(index, self._schedule_stack.count() - 1)))
        self._btn_submit.setText("开始执行" if index == 0 else "提交调度")
        if self._schedule_mode.currentText() == "具体时刻":
            self._sync_run_at_floor()
        self._update_preview()

    def _selected_run_at(self) -> datetime:
        date = self._run_date.date()
        clock = self._run_clock.time()
        return datetime(date.year(), date.month(), date.day(), clock.hour(), clock.minute(), clock.second())

    def _set_run_at(self, when: datetime) -> None:
        self._run_date.setDate(QDate(when.year, when.month, when.day))
        self._run_clock.setTime(QTime(when.hour, when.minute, when.second))

    def _sync_run_at_floor(self) -> None:
        """日期不能早于今天。若组合后的时刻已经过去，拉回当前时间。

        不把下限钉在当前这一秒上，否则改分、改秒时控件会把输入弹回去。
        """
        today = QDate.currentDate()
        self._run_date.setMinimumDate(today)
        if self._run_date.date() < today:
            self._run_date.setDate(today)
        now = datetime.now().replace(microsecond=0)
        if self._selected_run_at() < now:
            self._set_run_at(now)

    def _on_cycle_mode_changed(self, text: str) -> None:
        self._cycle_weekday.setVisible(text == "每周")
        self._update_preview()

    def _on_submit(self) -> None:
        if not self._all_steps_validated():
            failed = self._failed_validation_rows()
            if failed:
                logger.warning("素材校验异常: {}", failed)
                QMessageBox.warning(
                    self,
                    "素材校验异常",
                    "检测到部分素材异常，请删除异常素材或点击“重试校验”后再执行。",
                )
            else:
                QMessageBox.information(self, "校验中", "素材仍在后台校验，请稍候。")
            return
        try:
            plan = self.build_plan()
        except Exception as exc:
            logger.warning("构建计划失败: {}", exc)
            QMessageBox.warning(self, "计划不可用", "当前计划配置不完整，请检查联系人、素材和参数。")
            return

        spec = self._build_schedule_spec()
        after_label = ""
        if isinstance(spec, AfterSpec):
            after_label = delay_label(int(self._delay_value.value()), self._delay_unit.currentText())
        self.submit_prepared_plan(plan, spec, after_label=after_label)

    def refresh_contacts(self) -> None:
        """给群公告页的「刷新群聊」用，走同一套联系人刷新。"""
        self._contacts_panel.refresh_contacts(force_refresh=True, show_error=True)

    def _relay_contact_rows(self, rows: list) -> None:
        self.contactsSnapshotChanged.emit(list(rows))

    def submit_prepared_plan(self, plan: BroadcastPlan, spec: ScheduleSpec, after_label: str = "") -> None:
        """提交一份已经组好的计划。立即执行直接开跑，其余模式进入待执行。"""
        if isinstance(spec, ImmediateSpec):
            self.runPlanRequested.emit(plan)
            return

        from app.broadcast.pending_plans import PendingPlanStore

        plan_dict = plan.model_dump(mode="json")
        store = PendingPlanStore(account_id=self._account_id)

        if isinstance(spec, AfterSpec):
            trigger_dt = datetime.now().timestamp() + spec.delay_seconds
            trigger_iso = datetime.fromtimestamp(trigger_dt).isoformat(timespec="seconds")
            label = after_label or delay_label(int(spec.delay_seconds), "秒")
            entry = store.add(plan_dict, mode="after", trigger_at=trigger_iso, label=label)
            delay_ms = max(1, int(spec.delay_seconds * 1000))
            self._arm_pending_timer(entry.id, delay_ms)
            self.pendingPlansChanged.emit()
            when = trigger_iso.replace("T", " ")
            QMessageBox.information(self, "调度已设置", f"将在 {label}（{when}）自动执行。")
            return

        if isinstance(spec, AtSpec):
            target = datetime.fromisoformat(spec.run_at).replace(microsecond=0)
            now = datetime.now().replace(microsecond=0)
            if target < now:
                target = now
            trigger_iso = target.isoformat(timespec="seconds")
            label = trigger_iso.replace("T", " ")
            delta = (target - now).total_seconds()
            entry = store.add(plan_dict, mode="at", trigger_at=trigger_iso, label=label)
            delay_ms = 0 if delta <= 0 else max(1, int(delta * 1000))
            self._arm_pending_timer(entry.id, delay_ms)
            self.pendingPlansChanged.emit()
            QMessageBox.information(self, "调度已设置", f"将在 {label} 自动执行。")
            return

        if isinstance(spec, CronSpec):
            scheduler = self._resolve_scheduler()
            if scheduler is None:
                QMessageBox.critical(
                    self, "调度器不可用",
                    "循环调度需要后台调度器，但初始化失败。请检查 apscheduler 依赖是否正确安装。",
                )
                return
            payload = {"plan": plan_dict}
            try:
                sched_entry = scheduler.submit(spec, payload=payload)
            except Exception as exc:
                logger.error("提交调度失败: {}", exc)
                QMessageBox.critical(self, "提交调度失败", "任务提交失败，请稍后重试。")
                return
            label = cron_expr_label(spec.expr)
            trigger_iso = next_cron_fire(spec.expr, datetime.now()).isoformat(timespec="seconds")
            store.add(
                plan_dict,
                mode="cron",
                trigger_at=trigger_iso,
                label=label,
                schedule_id=sched_entry.id,
            )
            self.scheduledPlanPending.emit(sched_entry.id)
            self.pendingPlansChanged.emit()
            QMessageBox.information(self, "调度成功", f"循环任务已加入待执行：{label}")
            return

        QMessageBox.warning(self, "未知调度模式", "不支持的调度模式。")

    def _restore_pending_timers(self) -> None:
        """启动/切账号时恢复所有待执行计划的 QTimer。"""
        from datetime import datetime
        from app.broadcast.pending_plans import PendingPlanStore
        store = PendingPlanStore(account_id=self._account_id)
        now = datetime.now()
        for entry in store.list():
            if entry.mode == "cron":
                continue  # 循环由调度器负责，到点后仍留在待执行列表
            try:
                target = datetime.fromisoformat(entry.trigger_at)
            except Exception:
                store.remove(entry.id)
                continue
            delta = (target - now).total_seconds()
            delay_ms = 0 if delta <= 0 else max(1, int(delta * 1000))
            self._arm_pending_timer(entry.id, delay_ms)
        self._sync_cron_pending_from_scheduler()
        self.pendingPlansChanged.emit()

    def _stop_pending_timers(self) -> None:
        for timer in list(self._pending_timers.values()):
            timer.stop()
            timer.deleteLater()
        self._pending_timers.clear()

    def _arm_pending_timer(self, entry_id: str, delay_ms: int) -> None:
        old = self._pending_timers.pop(entry_id, None)
        if old is not None:
            old.stop()
            old.deleteLater()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda eid=entry_id: self._fire_pending_plan(eid))
        self._pending_timers[entry_id] = timer
        timer.start(max(0, int(delay_ms)))

    def reschedule_pending(self, entry_id: str) -> None:
        """触发时间被改写后，按新时间重新计时。"""
        from datetime import datetime
        from app.broadcast.pending_plans import PendingPlanStore

        store = PendingPlanStore(account_id=self._account_id)
        entry = store.get(entry_id)
        if entry is None:
            self.cancel_pending(entry_id)
            return
        try:
            target = datetime.fromisoformat(entry.trigger_at)
        except Exception:
            store.remove(entry_id)
            self.cancel_pending(entry_id)
            self.pendingPlansChanged.emit()
            return
        delta = (target - datetime.now()).total_seconds()
        delay_ms = 0 if delta <= 0 else max(1, int(delta * 1000))
        self._arm_pending_timer(entry_id, delay_ms)

    def cancel_pending(self, entry_id: str) -> None:
        timer = self._pending_timers.pop(entry_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def cancel_cron(self, schedule_id: str) -> None:
        """待执行列表里删掉循环任务时，同时取消后台循环。"""
        key = str(schedule_id or "").strip()
        if not key:
            return
        scheduler = self._resolve_scheduler()
        if scheduler is None:
            logger.warning("取消循环失败：调度器不可用 id={}", key)
            return
        scheduler.cancel(key)

    def _sync_cron_pending_from_scheduler(self) -> None:
        """启动时把调度器里已有的循环任务补进待执行列表。"""
        from app.broadcast.pending_plans import PendingPlanStore

        scheduler = self._resolve_scheduler()
        if scheduler is None:
            return
        store = PendingPlanStore(account_id=self._account_id)
        for entry in scheduler.list_entries():
            if entry.trigger != "cron" or entry.status != "active" or not entry.cron_expr:
                continue
            if store.find_by_schedule_id(entry.id) is not None:
                continue
            plan = entry.payload.get("plan") if isinstance(entry.payload, dict) else None
            if not isinstance(plan, dict):
                continue
            try:
                label = cron_expr_label(entry.cron_expr)
                trigger_iso = next_cron_fire(entry.cron_expr, datetime.now()).isoformat(timespec="seconds")
            except Exception as exc:
                logger.warning("循环任务无法列入待执行 id={} err={}", entry.id, exc)
                continue
            store.add(plan, mode="cron", trigger_at=trigger_iso, label=label, schedule_id=entry.id)

    def _fire_pending_plan(self, entry_id: str) -> None:
        """QTimer 到期：从持久化中取出计划并触发执行。"""
        self.cancel_pending(entry_id)
        from app.broadcast.pending_plans import PendingPlanStore
        store = PendingPlanStore(account_id=self._account_id)
        entry = store.get(entry_id)
        if entry is None:
            return  # 已被用户删除
        try:
            plan = BroadcastPlan.model_validate(entry.plan)
        except Exception as exc:
            logger.warning("待执行计划无效 id={} err={}", entry_id, exc)
            store.remove(entry_id)
            self.pendingPlansChanged.emit()
            return
        store.remove(entry_id)
        self.pendingPlansChanged.emit()
        self.runPlanRequested.emit(plan)

    def _on_delay_unit_changed(self, unit: str) -> None:
        """根据延迟单位动态调整 SpinBox 范围。"""
        if unit == "秒":
            self._delay_value.setRange(1, 86400)
        elif unit == "分钟":
            self._delay_value.setRange(1, 1440)
        else:  # 小时
            self._delay_value.setRange(1, 24)

    def _delay_seconds(self) -> float:
        base = float(self._delay_value.value())
        unit = self._delay_unit.currentText()
        if unit == "小时":
            return base * 3600.0
        if unit == "分钟":
            return base * 60.0
        return base  # 秒

    def _build_schedule_spec(self) -> ScheduleSpec:
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

    def _build_cycle_expr(self) -> str:
        hhmm = self._cycle_time.time().toString("HH:mm")
        if self._cycle_mode.currentText() == "每天":
            return f"daily@{hhmm}"
        index = self._cycle_weekday.currentIndex() + 1
        return f"weekly@{index}/{hhmm}"

    def _on_schedule_triggered(self, entry_obj: object) -> None:
        if not isinstance(entry_obj, ScheduleEntry):
            return
        payload = dict(entry_obj.payload)
        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, dict):
            return
        try:
            plan = BroadcastPlan.model_validate(raw_plan)
        except Exception as exc:
            logger.warning("调度任务无效: {}", exc)
            QMessageBox.warning(self, "调度任务无效", "该定时任务配置已失效，请重新创建任务。")
            return
        self.scheduleDialogAboutToShow.emit()
        dialog = ScheduleConfirmDialog(
            self,
            title="定时任务到点确认",
            message="任务已到设定时间，是否立即开始执行？",
            seconds=60,
        )
        if dialog.exec():
            self.runPlanRequested.emit(plan)

    def _resolve_scheduler(self) -> BroadcastScheduler | None:
        if self._scheduler_provider is None:
            logger.warning("scheduler_provider 未设置")
            return None
        try:
            return self._scheduler_provider()
        except Exception as exc:
            logger.error("调度器初始化失败: {}", exc)
            return None

    def enqueue_schedule_entry(self, entry: ScheduleEntry) -> None:
        if entry.trigger == "cron" and entry.cron_expr:
            self._touch_cron_pending(entry)
        self._scheduleTriggered.emit(entry)

    def _touch_cron_pending(self, entry: ScheduleEntry) -> None:
        """循环到点后仍留在列表里，只把下一次触发时刻往后推。"""
        from app.broadcast.pending_plans import PendingPlanStore

        store = PendingPlanStore(account_id=self._account_id)
        pending = store.find_by_schedule_id(entry.id)
        if pending is None:
            return
        try:
            trigger_iso = next_cron_fire(entry.cron_expr, datetime.now()).isoformat(timespec="seconds")
        except Exception as exc:
            logger.warning("更新循环下一次时间失败 id={} err={}", entry.id, exc)
            return
        store.update_trigger(pending.id, trigger_iso)
        self.pendingPlansChanged.emit()

    def _schedule_validation(self, row: int, payload: dict[str, Any]) -> None:
        self._stop_validation(row)
        item = self._sequence.item(row)
        if item is None:
            return
        item.setData(_VALIDATION_ROLE, "pending")
        item.setData(_VALIDATION_ERROR_ROLE, "")
        item.setData(_SEQUENCE_DESC_ROLE, self._sequence_text(payload))
        self._render_sequence_item_widget(item)
        thread = QThread(self)
        worker = _ValidateWorker(row, payload, self._account_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_validation_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit)

        def _on_thread_finished(r=row, t=thread) -> None:
            pair = self._validate_jobs.get(r)
            if pair is not None and pair[0] is t:
                self._cleanup_validation_job(r)

        thread.finished.connect(_on_thread_finished)
        # 必须同时持有 thread 和 worker：只存 thread 时 Python 会 GC 掉 worker，
        # started 槽永远调不到 run()，行就会永久停在「校验中」。
        self._validate_jobs[row] = (thread, worker)
        thread.start()
        self._update_submit_enabled()

    def _on_validation_finished(self, row: int, ok: bool, err: str) -> None:
        item = self._sequence.item(row)
        if item is None:
            self._update_submit_enabled()
            return
        status = "ok" if ok else "fail"
        item.setData(_VALIDATION_ROLE, status)
        item.setData(_VALIDATION_ERROR_ROLE, err if not ok else "")
        payload = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(payload, dict):
            item.setData(_SEQUENCE_DESC_ROLE, self._sequence_text(payload))
        self._render_sequence_item_widget(item)
        if not ok and self._sequence.currentRow() == row:
            self._btn_retry_validate.setEnabled(True)
        self._update_submit_enabled()

    def _cleanup_validation_job(self, row: int) -> None:
        pair = self._validate_jobs.pop(row, None)
        if pair is None:
            return
        thread, worker = pair
        worker.deleteLater()
        thread.deleteLater()

    def _stop_validation(self, row: int) -> None:
        pair = self._validate_jobs.get(row)
        if pair is None:
            return
        thread, _worker = pair
        if thread.isRunning():
            thread.quit()
            thread.wait(500)
        self._cleanup_validation_job(row)

    def _stop_all_validations(self) -> None:
        rows = list(self._validate_jobs.keys())
        for row in rows:
            self._stop_validation(row)

    def _validation_status(self, row: int) -> str:
        item = self._sequence.item(row)
        if item is None:
            return "ok"
        return str(item.data(_VALIDATION_ROLE) or "pending").strip() or "pending"

    def _failed_validation_rows(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for row in range(self._sequence.count()):
            if self._validation_status(row) != "fail":
                continue
            item = self._sequence.item(row)
            err = ""
            if item is not None:
                err = str(item.data(_VALIDATION_ERROR_ROLE) or "校验失败").strip()
            out.append((row, err or "校验失败"))
        return out

    def _all_steps_validated(self) -> bool:
        if self._sequence.count() < 1:
            return False
        has_real_step = False
        for row in range(self._sequence.count()):
            data = self._sequence.item(row).data(Qt.ItemDataRole.UserRole)
            row_type = str(data.get("_row_type", "")) if isinstance(data, dict) else ""
            if row_type == "gap":
                continue
            has_real_step = True
            if row_type == "bundle_header":
                continue
            if self._validation_status(row) != "ok":
                return False
        return has_real_step

    def _update_submit_enabled(self) -> None:
        count = self._sequence.count()
        if count < 1:
            self._btn_submit.setEnabled(False)
            return
        ready = self._all_steps_validated()
        self._btn_submit.setEnabled(ready)

    def _on_sequence_selection_changed(self, _row: int) -> None:
        row = self._sequence.currentRow()
        self._btn_retry_validate.setEnabled(row >= 0 and self._validation_status(row) == "fail")

    def _retry_selected_validation(self) -> None:
        row = self._sequence.currentRow()
        if row < 0:
            return
        item = self._sequence.item(row)
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        self._schedule_validation(row, dict(payload))

    def _validation_status_text(self, status: str) -> tuple[str, str]:
        if status == "ok":
            return ("✅ 通过", "#16a34a")
        if status == "fail":
            return ("❌ 失败", "#dc2626")
        return ("⏳ 校验中", "#d97706")

    def _refresh_sequence_widgets(self) -> None:
        for row in range(self._sequence.count()):
            item = self._sequence.item(row)
            if item is None:
                continue
            self._render_sequence_item_widget(item)

    def _render_sequence_item_widget(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        # Gap 行：渲染为间隔设置 widget
        if isinstance(data, dict) and str(data.get("_row_type", "")) == "gap":
            gap = float(data.get("gap", DEFAULT_STEP_GAP))
            widget = self._make_gap_widget(item, gap)
            self._sequence.setItemWidget(item, widget)
            return
        desc = str(item.data(_SEQUENCE_DESC_ROLE) or item.text()).strip()
        status = str(item.data(_VALIDATION_ROLE) or "pending").strip() or "pending"
        status_text, color = self._validation_status_text(status)
        err = str(item.data(_VALIDATION_ERROR_ROLE) or "").strip()
        if status == "fail" and err:
            status_text = f"❌ {err}"

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        nested = isinstance(data, dict) and str(data.get("_row_type", "")) == "bundle_child"
        row_layout.setContentsMargins(28 if nested else 6, 0, 6, 0)
        row_layout.setSpacing(12)
        desc_label = QLabel(desc)
        status_label = QLabel(status_text)
        status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        if err:
            status_label.setToolTip(err)
        row_layout.addWidget(desc_label, 1)
        row_layout.addWidget(status_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        for child in row_widget.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._sequence.setItemWidget(item, row_widget)

    def _real_asset_name(self, name: str, item_id: str = "") -> str:
        from app.broadcast.asset_preview_source import usable_asset_name

        return usable_asset_name(name, item_id)

    def _lookup_named_asset(self, item_id: str, *kinds: str) -> str:
        key = str(item_id or "").strip()
        if not key:
            return ""
        for kind in kinds:
            found = self._lookup_sequence_name(kind, key)
            shown = self._real_asset_name(found, key)
            if shown:
                return shown
        return ""

    def _public_asset_name(self, name: str, item_id: str = "", *, empty_label: str = "未命名") -> str:
        text = str(name or "").strip()
        key = str(item_id or "").strip()
        if text and text != key:
            return text
        return empty_label

    def _sync_sequence_display_names(self) -> None:
        """已保存素材改名后，素材序列里已经列出来的行跟着改。"""
        for row in range(self._sequence.count()):
            item = self._sequence.item(row)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
                continue
            updated = self._step_with_current_name(dict(data))
            if updated == data:
                continue
            item.setData(Qt.ItemDataRole.UserRole, updated)
            item.setData(_SEQUENCE_DESC_ROLE, self._sequence_text(updated))
            self._render_sequence_item_widget(item)

    def _step_with_current_name(self, data: dict[str, Any]) -> dict[str, Any]:
        step_type = str(data.get("type", "")).strip()
        if step_type == "voice":
            name = self._lookup_sequence_name("voice", str(data.get("voice_id", "")))
            if name:
                data["display_name"] = name
        elif step_type == "text" and str(data.get("template_id", "")).strip():
            name = self._lookup_sequence_name("text", str(data.get("template_id", "")))
            if name:
                data["display_name"] = name
        elif step_type in {"oa", "doc", "channel"}:
            name = self._lookup_sequence_name(step_type, str(data.get("template_id", "")))
            if name:
                data["display_name"] = name
        elif step_type == "wxa":
            name = self._lookup_sequence_name("wxa", str(data.get("template_id", "")))
            if name:
                data["display_name"] = name
        elif step_type in {"image", "video"}:
            name = self._lookup_media_name(step_type, str(data.get("path", "")))
            if name:
                data["display_name"] = name
        elif step_type == "bundle":
            name = self._lookup_sequence_name("bundle", str(data.get("bundle_id", "")))
            if name:
                data["bundle_name"] = name
        return data

    def _lookup_media_name(self, asset_type: str, path: str) -> str:
        target = str(path or "").strip()
        kind = str(asset_type or "").strip().lower()
        if not target or not kind:
            return ""
        for row in self._asset_rows:
            if str(row.get("asset_type", "")).strip().lower() != kind:
                continue
            if str(row.get("path", "")).strip() != target:
                continue
            return self._public_asset_name(
                str(row.get("name", "")),
                str(row.get("item_id", "")).strip(),
                empty_label="",
            )
        return ""

    def _media_step_name(self, payload: dict[str, Any]) -> str:
        shown = self._real_asset_name(str(payload.get("display_name", "")))
        if shown:
            return shown
        return self._lookup_media_name(str(payload.get("type", "")), str(payload.get("path", ""))) or "未命名"

    def _lookup_sequence_name(self, asset_type: str, item_id: str) -> str:
        kind = str(asset_type or "").strip().lower()
        key = str(item_id or "").strip()
        if not kind or not key:
            return ""
        for row in self._asset_rows:
            row_kind = str(row.get("asset_type", "")).strip().lower()
            if row_kind != kind or str(row.get("item_id", "")).strip() != key:
                continue
            return self._public_asset_name(str(row.get("name", "")), key, empty_label="")
        return ""

    def _sequence_text(self, payload: dict[str, Any]) -> str:
        step_type = str(payload.get("type", "unknown"))
        type_label = asset_type_to_zh(step_type)
        if step_type == "voice":
            voice_id = str(payload.get("voice_id", "")).strip()
            shown = self._public_asset_name(str(payload.get("display_name", "")), voice_id, empty_label="")
            shown = shown or self._lookup_sequence_name("voice", voice_id) or "未命名"
            return f"{type_label}：{shown}"
        if step_type in {"text", "at", "notice"}:
            template_id = str(payload.get("template_id", "")).strip()
            shown = self._public_asset_name(str(payload.get("display_name", "")), template_id, empty_label="")
            if not shown:
                shown = self._lookup_sequence_name("text", template_id)
            if not shown:
                shown = str(payload.get("body", "")).strip() or "未命名"
            label = "文字" if step_type == "text" else type_label
            return f"{label}：{shown}"
        if step_type == "image":
            shown = self._media_step_name(payload)
            return f"{type_label}：{shown}"
        if step_type in {"oa", "doc", "channel"}:
            template_id = str(payload.get("template_id", "")).strip()
            shown = self._real_asset_name(str(payload.get("display_name", "")), template_id)
            shown = shown or self._lookup_named_asset(template_id, step_type, "doc", "channel", "oa")
            return f"{type_label}：{shown or '未命名'}"
        if step_type == "wxa":
            template_id = str(payload.get("template_id", "")).strip()
            shown = self._real_asset_name(str(payload.get("display_name", "")), template_id)
            shown = shown or self._lookup_sequence_name("wxa", template_id) or "未命名"
            return f"{type_label}：{shown}"
        if step_type == "video":
            shown = self._media_step_name(payload)
            return f"{type_label}：{shown}"
        if step_type == "link":
            template_id = str(payload.get("template_id", "")).strip()
            shown = str(payload.get("url", "")).strip()
            if not shown:
                shown = self._public_asset_name(str(payload.get("display_name", "")), template_id, empty_label="")
                shown = shown or self._lookup_sequence_name("text", template_id) or "未命名"
            return f"文字：{shown}"
        if step_type == "bundle":
            bundle_id = str(payload.get("bundle_id", "")).strip()
            shown = self._public_asset_name(
                str(payload.get("bundle_name", "")),
                bundle_id,
                empty_label="",
            )
            shown = shown or self._lookup_sequence_name("bundle", bundle_id) or "未命名素材包"
            if payload.get("presentation") == "expanded":
                return f"素材包：{shown}（展开）"
            if payload.get("presentation") == "atomic":
                return f"素材包：{shown}（原子）"
            return f"素材包：{shown}"
        return f"{type_label}：未命名"

    def _toggle_atomic_bundle(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        group_id = str(data.get("group_id", "")).strip()
        bundle_id = str(data.get("bundle_id", "")).strip()
        header_row = self._sequence.row(item)
        if header_row < 0 or not group_id:
            return
        has_child = False
        for index in range(self._sequence.count()):
            row = self._sequence.item(index).data(Qt.ItemDataRole.UserRole)
            if (
                isinstance(row, dict)
                and str(row.get("group_id", "")).strip() == group_id
                and str(row.get("_row_type", "")) == "bundle_child"
            ):
                has_child = True
                break
        if has_child:
            self._remove_group_children(group_id)
            return
        steps = self._bundle_steps_for(bundle_id)
        if not steps:
            QMessageBox.information(self, "素材包", "这个素材包里没有可展开的素材。")
            return
        from app.broadcast.asset_bundles import get_bundle

        bundle = get_bundle(bundle_id, account_id=self._account_id)
        gaps = list(bundle.step_gaps) if bundle is not None else []
        insert_at = header_row + 1
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            child = dict(step)
            child["_row_type"] = "bundle_child"
            child["group_id"] = group_id
            child["library_bundle_id"] = bundle_id
            self._insert_raw_sequence_item(insert_at, child, validate=False)
            insert_at += 1
            if index < len(steps) - 1:
                gap = float(gaps[index]) if index < len(gaps) else DEFAULT_STEP_GAP
                self._insert_raw_sequence_item(
                    insert_at,
                    {
                        "_row_type": "gap",
                        "gap": gap,
                        "group_id": group_id,
                        "library_bundle_id": bundle_id,
                    },
                    validate=False,
                )
                insert_at += 1

    def _remove_group_children(self, group_id: str) -> None:
        for index in range(self._sequence.count() - 1, -1, -1):
            data = self._sequence.item(index).data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict) or str(data.get("group_id", "")).strip() != group_id:
                continue
            if str(data.get("_row_type", "")) == "bundle_header":
                continue
            self._sequence.takeItem(index)

    def _bundle_steps_for(self, bundle_id: str) -> list[dict[str, Any]]:
        for row in self._asset_rows:
            if str(row.get("asset_type", "")).strip() != "bundle":
                continue
            if str(row.get("item_id", "")).strip() != bundle_id:
                continue
            steps = _asset_steps(row)
            return [dict(step) for step in steps if isinstance(step, dict)]
        return []

    def _insert_raw_sequence_item(self, row: int, payload: dict[str, Any], *, validate: bool) -> None:
        item = QListWidgetItem()
        item.setText("")
        item.setData(Qt.ItemDataRole.UserRole, dict(payload))
        row_type = str(payload.get("_row_type", ""))
        item.setData(_VALIDATION_ROLE, "ok" if row_type in {"gap", "bundle_header"} or not validate else "pending")
        item.setData(_VALIDATION_ERROR_ROLE, "")
        item.setData(_SEQUENCE_DESC_ROLE, self._sequence_text(payload))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._sequence.insertItem(row, item)
        self._render_sequence_item_widget(item)
        if validate and row_type not in {"gap", "bundle_header"}:
            self._schedule_validation(row, dict(payload))

    def _persist_atomic_bundle_gaps(self, bundle_id: str, group_id: str) -> None:
        from app.broadcast.asset_bundles import get_bundle, save_bundle

        bundle = get_bundle(bundle_id, account_id=self._account_id)
        if bundle is None:
            return
        gaps: list[float] = []
        for index in range(self._sequence.count()):
            data = self._sequence.item(index).data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            if str(data.get("group_id", "")).strip() != group_id:
                continue
            if str(data.get("_row_type", "")) != "gap":
                continue
            gaps.append(max(3.0, float(data.get("gap", DEFAULT_STEP_GAP))))
        bundle.step_gaps = gaps
        save_bundle(bundle, account_id=self._account_id)

    def _preview_sequence_item(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
            return
        if str(data.get("_row_type", "")) == "bundle_header":
            if str(data.get("presentation", "")) == "atomic":
                self._toggle_atomic_bundle(item)
                return
            self._preview_sequence_bundle(str(data.get("bundle_id", "")).strip())
            return
        step_type = str(data.get("type", "")).strip().lower()
        if step_type == "bundle":
            self._preview_sequence_bundle(str(data.get("bundle_id", "")).strip())
            return
        from gui.widgets.pending_plan_detail import open_message_preview

        open_message_preview(
            self,
            data,
            account_id=self._account_id,
            preview_service_provider=self._preview_service_provider,
            name=str(data.get("display_name") or ""),
        )

    def _sequence_asset_row(self, data: dict[str, Any]) -> dict[str, Any] | None:
        step_type = str(data.get("type", "")).strip().lower()
        if step_type == "voice":
            return self._find_asset_row("voice", str(data.get("voice_id", "")).strip())
        if step_type == "text":
            return self._find_asset_row("text", str(data.get("template_id", "")).strip())
        if step_type == "oa":
            return self._find_asset_row("oa", str(data.get("template_id", "")).strip())
        if step_type == "wxa":
            return self._find_asset_row("wxa", str(data.get("template_id", "")).strip())
        if step_type in {"image", "video"}:
            path = str(data.get("path", "")).strip()
            for row in self._asset_rows:
                if str(row.get("asset_type", "")).strip().lower() != step_type:
                    continue
                if str(row.get("path", "")).strip() == path and path:
                    return dict(row)
            return None
        return None

    def _find_asset_row(self, asset_type: str, item_id: str) -> dict[str, Any] | None:
        kind = asset_type.strip().lower()
        key = item_id.strip()
        if not kind or not key:
            return None
        for row in self._asset_rows:
            row_kind = str(row.get("asset_type", "")).strip().lower()
            if row_kind == kind and str(row.get("item_id", "")).strip() == key:
                return dict(row)
        return None

    def _preview_sequence_bundle(self, bundle_id: str) -> None:
        from app.broadcast.asset_bundles import get_bundle
        from gui.tabs.library_bundle_tab import BundleDetailDialog

        if not bundle_id:
            return
        bundle = get_bundle(bundle_id, account_id=self._account_id)
        if bundle is None:
            QMessageBox.information(self, "素材包不存在", "这个素材包已经不在素材库里。")
            return
        asset_map: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._asset_rows:
            if str(row.get("asset_type", "")).strip().lower() == "bundle":
                continue
            asset_type = str(row.get("asset_type", "")).strip().lower()
            item_id = str(row.get("item_id", "")).strip()
            if asset_type and item_id:
                asset_map[(asset_type, item_id)] = dict(row)
        dialog = BundleDetailDialog(
            bundle,
            asset_map,
            self,
            preview_service_provider=self._preview_service_provider,
            account_id=self._account_id,
            readonly=True,
        )
        dialog.exec()

    def _open_asset_preview(self, row: dict[str, Any]) -> None:
        name = str(row.get("name", "")).strip() or "未命名"
        provider = self._preview_service_provider
        if provider is None:
            QMessageBox.information(self, "素材详情", self._sequence_text({"type": row.get("asset_type", ""), "display_name": name}))
            return
        from gui.widgets.asset_preview import AssetPreviewPanel

        dialog = QDialog(self)
        dialog.setWindowTitle(f"素材详情 · {name}")
        dialog.resize(520, 560)
        layout = QVBoxLayout(dialog)
        panel = AssetPreviewPanel(preview_service_provider=provider)
        panel.set_asset(row)
        layout.addWidget(panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _update_preview(self) -> None:
        return

