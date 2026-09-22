from __future__ import annotations

import json
from typing import Any, Callable

from loguru import logger
from PySide6.QtCore import QDateTime, QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.asset_validator import validate_step_payload
from app.broadcast.dto import AssetRowDTO, ContactRowDTO, PlanContact
from app.broadcast.plan import DEFAULT_CONTACT_GAP, DEFAULT_MSG_GAP, BroadcastPlan
from app.broadcast.schedule_spec import AfterSpec, AtSpec, CronSpec, ImmediateSpec, ScheduleSpec
from app.broadcast.scheduler import BroadcastScheduler, ScheduleEntry
from app.broadcast.sop_store import SopTemplate, list_sops, resolve_contacts_by_labels, save_sop
from gui.i18n import ASSET_TYPE_FILTER_OPTIONS, asset_type_from_filter, asset_type_to_zh
from gui.widgets.contact_confirm_panel import ContactConfirmDialog
from gui.widgets.label_multiselect import LabelMultiSelect
from gui.widgets.schedule_confirm_dialog import ScheduleConfirmDialog
from gui.widgets.sop_dialog import SopDialog

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
    runStepRequested = Signal(object, list)
    scheduledPlanPending = Signal(str)
    _scheduleTriggered = Signal(object)
    scheduleDialogAboutToShow = Signal()

    def __init__(
        self,
        *,
        scheduler_provider: Callable[[], BroadcastScheduler] | None = None,
    ) -> None:
        super().__init__()
        self._account_id = "default"
        self._building = False
        self._contacts_all: list[dict[str, Any]] = []
        self._selected_contacts: dict[str, PlanContact] = {}
        self._asset_rows: list[dict[str, Any]] = []
        self._sops: list[SopTemplate] = []
        self._scheduler_provider = scheduler_provider
        self._validate_jobs: dict[int, tuple[QThread, _ValidateWorker]] = {}
        self._build_ui()
        self._bind_events()
        self._reload_sops()
        self._refresh_contact_list()
        self._refresh_asset_picker()
        self._update_preview()
        self._update_submit_enabled()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        top = QHBoxLayout()

        contacts_group = QGroupBox("联系人")
        contacts_layout = QVBoxLayout(contacts_group)
        self._contacts_search = QLineEdit()
        self._contacts_search.setPlaceholderText("搜索：备注/微信名/标签")
        self._contact_labels = LabelMultiSelect()
        self._contacts_list = QListWidget()
        contacts_layout.addWidget(self._contacts_search)
        contacts_layout.addWidget(self._contact_labels)
        contacts_layout.addWidget(self._contacts_list, 1)

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

        quick_row = QHBoxLayout()
        self._inline_text = QLineEdit()
        self._inline_text.setPlaceholderText("临时文字（可选，不走素材库）")
        self._btn_add_inline_text = QPushButton("添加文字")
        quick_row.addWidget(self._inline_text, 1)
        quick_row.addWidget(self._btn_add_inline_text)
        sequence_layout.addLayout(quick_row)

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
        self._btn_retry_step = QPushButton("重试此步骤")
        self._btn_retry_validate.setEnabled(False)
        self._btn_retry_step.setEnabled(False)
        seq_actions.addWidget(self._btn_del)
        seq_actions.addWidget(self._btn_clear)
        seq_actions.addWidget(self._btn_retry_validate)
        seq_actions.addWidget(self._btn_retry_step)
        seq_actions.addStretch(1)
        sequence_layout.addLayout(seq_actions)

        params_group = QGroupBox("参数与调度")
        params_layout = QVBoxLayout(params_group)

        # UI 层默认全部归 0：消息/联系人间隔、语音扰动都关闭；用户按需再调。
        # 注意：这里只改 UI 首次呈现，`app/broadcast/plan.py::DEFAULT_MSG_GAP` /
        # `DEFAULT_CONTACT_GAP` 仍作为 runner 层的安全兜底常量，不动。
        gap_grid = QGridLayout()
        self._msg_gap_min_slider = QSlider(Qt.Orientation.Horizontal)
        self._msg_gap_min_slider.setRange(0, 600)
        self._msg_gap_min_slider.setValue(0)
        self._msg_gap_max_slider = QSlider(Qt.Orientation.Horizontal)
        self._msg_gap_max_slider.setRange(0, 600)
        self._msg_gap_max_slider.setValue(0)
        self._msg_gap_min = QSpinBox()
        self._msg_gap_min.setRange(0, 600)
        self._msg_gap_min.setValue(0)
        self._msg_gap_max = QSpinBox()
        self._msg_gap_max.setRange(0, 600)
        self._msg_gap_max.setValue(0)

        self._contact_gap_min_slider = QSlider(Qt.Orientation.Horizontal)
        self._contact_gap_min_slider.setRange(0, 3600)
        self._contact_gap_min_slider.setValue(0)
        self._contact_gap_max_slider = QSlider(Qt.Orientation.Horizontal)
        self._contact_gap_max_slider.setRange(0, 3600)
        self._contact_gap_max_slider.setValue(0)
        self._contact_gap_min = QSpinBox()
        self._contact_gap_min.setRange(0, 3600)
        self._contact_gap_min.setValue(0)
        self._contact_gap_max = QSpinBox()
        self._contact_gap_max.setRange(0, 3600)
        self._contact_gap_max.setValue(0)
        self._perturb = QCheckBox("语音扰动开关")
        self._perturb.setChecked(False)

        gap_grid.addWidget(QLabel("消息间隔最小(秒)"), 0, 0)
        gap_grid.addWidget(self._msg_gap_min_slider, 0, 1)
        gap_grid.addWidget(self._msg_gap_min, 0, 2)
        gap_grid.addWidget(QLabel("消息间隔最大(秒)"), 1, 0)
        gap_grid.addWidget(self._msg_gap_max_slider, 1, 1)
        gap_grid.addWidget(self._msg_gap_max, 1, 2)
        gap_grid.addWidget(QLabel("联系人间隔最小(秒)"), 2, 0)
        gap_grid.addWidget(self._contact_gap_min_slider, 2, 1)
        gap_grid.addWidget(self._contact_gap_min, 2, 2)
        gap_grid.addWidget(QLabel("联系人间隔最大(秒)"), 3, 0)
        gap_grid.addWidget(self._contact_gap_max_slider, 3, 1)
        gap_grid.addWidget(self._contact_gap_max, 3, 2)
        params_layout.addLayout(gap_grid)
        params_layout.addWidget(self._perturb)

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
        self._delay_unit.addItems(["分钟", "小时"])
        delay_row.addWidget(self._delay_value)
        delay_row.addWidget(self._delay_unit)
        delay_row.addStretch(1)
        self._schedule_stack.addWidget(delay_page)

        at_page = QWidget()
        at_row = QHBoxLayout(at_page)
        at_row.setContentsMargins(0, 0, 0, 0)
        self._run_at = QDateTimeEdit()
        self._run_at.setCalendarPopup(True)
        self._run_at.setDateTime(QDateTime.currentDateTime().addSecs(5 * 60))
        at_row.addWidget(self._run_at)
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

        sop_row = QHBoxLayout()
        self._btn_save_sop = QPushButton("另存为 SOP")
        self._sop_pick = QComboBox()
        self._btn_use_sop = QPushButton("使用 SOP")
        sop_row.addWidget(self._btn_save_sop)
        sop_row.addWidget(self._sop_pick, 1)
        sop_row.addWidget(self._btn_use_sop)
        params_layout.addLayout(sop_row)

        self._btn_submit = QPushButton("开始执行")
        params_layout.addWidget(self._btn_submit)
        params_layout.addStretch(1)

        top.addWidget(contacts_group, 2)
        top.addWidget(sequence_group, 3)
        top.addWidget(params_group, 3)
        root.addLayout(top, 1)

        self._preview_group = QGroupBox("JSON 预览（调试）")
        self._preview_group.setCheckable(True)
        self._preview_group.setChecked(False)
        preview_layout = QVBoxLayout(self._preview_group)
        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        preview_layout.addWidget(self._preview)
        root.addWidget(self._preview_group, 1)

    def _bind_events(self) -> None:
        self._contacts_search.textChanged.connect(self._refresh_contact_list)
        self._contacts_list.itemChanged.connect(self._on_contact_item_changed)
        self._contact_labels.labelToggled.connect(self._on_label_toggled)
        self._asset_type_filter.currentTextChanged.connect(self._refresh_asset_picker)
        self._asset_search.textChanged.connect(self._refresh_asset_picker)
        self._btn_add_asset.clicked.connect(self._add_selected_asset_step)
        self._btn_add_inline_text.clicked.connect(self._add_inline_text_step)
        self._btn_del.clicked.connect(self._delete_selected_sequence_item)
        self._btn_clear.clicked.connect(self._clear_sequence)
        self._btn_retry_validate.clicked.connect(self._retry_selected_validation)
        self._btn_retry_step.clicked.connect(self._retry_selected_step)
        self._sequence.currentRowChanged.connect(self._on_sequence_selection_changed)
        self._sequence.customContextMenuRequested.connect(self._on_sequence_context_menu)
        self._schedule_mode.currentIndexChanged.connect(self._on_schedule_mode_changed)
        self._cycle_mode.currentTextChanged.connect(self._on_cycle_mode_changed)
        self._btn_submit.clicked.connect(self._on_submit)
        self._btn_save_sop.clicked.connect(self._save_as_sop)
        self._btn_use_sop.clicked.connect(self._use_sop)
        self._scheduleTriggered.connect(self._on_schedule_triggered)
        self._preview_group.toggled.connect(self._update_preview)

        self._sync_slider_spin(self._msg_gap_min_slider, self._msg_gap_min)
        self._sync_slider_spin(self._msg_gap_max_slider, self._msg_gap_max)
        self._sync_slider_spin(self._contact_gap_min_slider, self._contact_gap_min)
        self._sync_slider_spin(self._contact_gap_max_slider, self._contact_gap_max)

        for widget in [
            self._msg_gap_min,
            self._msg_gap_max,
            self._contact_gap_min,
            self._contact_gap_max,
            self._delay_value,
        ]:
            widget.editingFinished.connect(self._update_preview)
        self._delay_unit.currentIndexChanged.connect(self._update_preview)
        self._run_at.dateTimeChanged.connect(self._update_preview)
        self._cycle_mode.currentIndexChanged.connect(self._update_preview)
        self._cycle_weekday.currentIndexChanged.connect(self._update_preview)
        self._cycle_time.timeChanged.connect(self._update_preview)
        self._perturb.toggled.connect(self._update_preview)
        self._sequence.model().rowsMoved.connect(lambda *_: self._update_preview())
        self._sequence.model().rowsMoved.connect(lambda *_: self._refresh_sequence_widgets())
        self._sequence.itemChanged.connect(lambda *_: self._update_preview())
        self._on_cycle_mode_changed(self._cycle_mode.currentText())
        self._on_schedule_mode_changed(self._schedule_mode.currentIndex())

    def _sync_slider_spin(self, slider: QSlider, spin: QSpinBox) -> None:
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        spin.valueChanged.connect(self._normalize_gaps)
        spin.valueChanged.connect(self._update_preview)

    def _normalize_gaps(self) -> None:
        if self._msg_gap_min.value() > self._msg_gap_max.value():
            self._msg_gap_max.setValue(self._msg_gap_min.value())
        if self._contact_gap_min.value() > self._contact_gap_max.value():
            self._contact_gap_max.setValue(self._contact_gap_min.value())

    def set_account(self, account_id: str) -> None:
        self._account_id = account_id.strip() or "default"
        self._stop_all_validations()
        self._selected_contacts.clear()
        self._sequence.clear()
        self._reload_sops()
        self._update_submit_enabled()
        self._refresh_contact_list()
        self._update_preview()

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
        self._refresh_contact_list()
        self._update_preview()

    def set_contact_rows(self, rows: list[ContactRowDTO]) -> None:
        self._contacts_all = [
            {
                "display_name": row.display_name,
                "conv_local": row.conv_local,
                "labels": list(row.labels),
                "peer_name": row.peer_name,
                "category": row.category,
            }
            for row in rows
        ]
        self._refresh_contact_list()

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
                or voice_id
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

    def build_plan(self) -> BroadcastPlan:
        contacts = [
            item.model_dump(mode="json")
            for item in sorted(self._selected_contacts.values(), key=lambda x: x.display_name)
        ]
        sequence = [self._step_data(i) for i in range(self._sequence.count())]
        payload = {
            "contacts": contacts,
            "sequence": sequence,
            "msg_gap_seconds": [self._msg_gap_min.value(), self._msg_gap_max.value()],
            "contact_gap_seconds": [self._contact_gap_min.value(), self._contact_gap_max.value()],
            "perturb": self._perturb.isChecked(),
        }
        return BroadcastPlan.model_validate(payload)

    def _step_data(self, row: int) -> dict[str, Any]:
        item = self._sequence.item(row)
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            raise ValueError("sequence 项数据非法")
        return dict(data)

    def _refresh_contact_list(self) -> None:
        keyword = self._contacts_search.text().strip().lower()
        labels = sorted({x for row in self._contacts_all for x in row.get("labels", [])})
        self._contact_labels.set_labels(labels)
        self._building = True
        try:
            self._contacts_list.clear()
            for row in self._contacts_all:
                display_name = str(row.get("display_name", "")).strip()
                conv_local = str(row.get("conv_local", "")).strip()
                if not display_name or not conv_local:
                    continue
                if keyword:
                    labels_text = " ".join(str(x) for x in row.get("labels", []))
                    corpus = f"{display_name} {row.get('peer_name', '')} {labels_text}".lower()
                    if keyword not in corpus:
                        continue
                item = QListWidgetItem(display_name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if conv_local in self._selected_contacts else Qt.CheckState.Unchecked
                )
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    {"conv_local": conv_local, "display_name": display_name},
                )
                self._contacts_list.addItem(item)
        finally:
            self._building = False

    def _on_contact_item_changed(self, item: QListWidgetItem) -> None:
        if self._building:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        conv_local = str(data.get("conv_local", "")).strip()
        display_name = str(data.get("display_name", item.text())).strip()
        if not conv_local or not display_name:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_contacts[conv_local] = PlanContact(
                conv_local=conv_local,
                display_name=display_name,
            )
        else:
            self._selected_contacts.pop(conv_local, None)
        self._update_preview()

    def _on_label_toggled(self, label: str, checked: bool) -> None:
        tag = label.strip()
        if not tag:
            return
        touched: set[str] = set()
        for row in self._contacts_all:
            labels = [str(x).strip() for x in row.get("labels", []) if str(x).strip()]
            if tag not in labels:
                continue
            display_name = str(row.get("display_name", "")).strip()
            conv_local = str(row.get("conv_local", "")).strip()
            if not display_name or not conv_local:
                continue
            touched.add(conv_local)
            if checked:
                self._selected_contacts[conv_local] = PlanContact(
                    conv_local=conv_local,
                    display_name=display_name,
                )
            else:
                self._selected_contacts.pop(conv_local, None)
        if touched:
            self._refresh_contact_list()
            self._update_preview()

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
        self._asset_pick.clear()
        for row in rows:
            type_label = asset_type_to_zh(str(row.get("asset_type", "")))
            text = f"{type_label} · {row.get('name')} ({row.get('item_id')})"
            self._asset_pick.addItem(text, row)

    def _add_selected_asset_step(self) -> None:
        data = self._asset_pick.currentData()
        if not isinstance(data, dict):
            QMessageBox.information(self, "暂无可用素材", "当前筛选条件下没有可用素材，请先到素材库准备素材。")
            return
        asset_type = str(data.get("asset_type", "")).strip()
        if asset_type == "voice":
            payload = {"type": "voice", "voice_id": str(data.get("item_id", "")).strip()}
        elif asset_type == "text":
            payload = {
                "type": "text",
                "template_id": str(data.get("item_id", "")).strip(),
                "variant_pick": "random",
                "text_kind": "text",
            }
        elif asset_type == "link":
            # 「小程序链接」= TextTemplate(kind="link")，发送侧按 text 通道走。
            # 与 asset_type=="text" 生成的 step 结构完全一致，让 runner 无需感知。
            payload = {
                "type": "text",
                "template_id": str(data.get("item_id", "")).strip(),
                "variant_pick": "random",
                "text_kind": "link",
            }
        elif asset_type == "image":
            payload = {"type": "image", "path": str(data.get("path", "")).strip()}
        elif asset_type == "oa":
            payload = {"type": "oa", "template_id": str(data.get("item_id", "")).strip()}
        elif asset_type == "video":
            payload = {
                "type": "video",
                "path": str(data.get("path", "")).strip(),
                "thumb_path": str(data.get("thumb_path", "")).strip(),
            }
        elif asset_type == "bundle":
            bundle_id = str(data.get("item_id", "")).strip()
            bundle_name = str(data.get("name", "")).strip() or bundle_id
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
            if mode == "展开为多个步骤":
                extra = data.get("extra", {}) if isinstance(data.get("extra"), dict) else {}
                steps = list(extra.get("steps", []))
                if not isinstance(steps, list) or not steps:
                    QMessageBox.warning(self, "素材包不可用", "该素材包没有可展开步骤。")
                    return
                for row in steps:
                    if isinstance(row, dict):
                        self._append_sequence_item(row)
                return
            payload = {"type": "bundle", "bundle_id": bundle_id}
        else:
            QMessageBox.information(self, "暂不支持", "该素材类型暂不支持加入发送计划。")
            return
        try:
            # 走 schema 预校验，避免把无效 step 放进序列。
            BroadcastPlan.model_validate(
                {
                    "contacts": ["_check_"],
                    "sequence": [payload],
                    "msg_gap_seconds": list(DEFAULT_MSG_GAP),
                    "contact_gap_seconds": list(DEFAULT_CONTACT_GAP),
                    "perturb": True,
                }
            )
        except Exception as exc:
            logger.warning("素材预校验失败 asset_type={} payload={} err={}", asset_type, payload, exc)
            QMessageBox.warning(self, "素材不可用", "该素材当前不可用，请重新选择或稍后重试。")
            return
        self._append_sequence_item(payload)

    def _add_inline_text_step(self) -> None:
        body = self._inline_text.text().strip()
        if not body:
            return
        self._append_sequence_item({"type": "text", "body": body, "text_kind": "text"})
        self._inline_text.clear()

    def _append_sequence_item(self, payload: dict[str, Any]) -> None:
        item = QListWidgetItem()
        item.setText("")
        item.setData(Qt.ItemDataRole.UserRole, dict(payload))
        item.setData(_VALIDATION_ROLE, "pending")
        item.setData(_VALIDATION_ERROR_ROLE, "")
        item.setData(_SEQUENCE_DESC_ROLE, self._sequence_text(payload))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        row = self._sequence.count()
        self._sequence.addItem(item)
        self._render_sequence_item_widget(item)
        self._sequence.setCurrentItem(item)
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
        for row in sorted(rows, reverse=True):
            self._stop_validation(row)
            self._sequence.takeItem(row)

    def _on_sequence_context_menu(self, pos) -> None:
        row = self._sequence.indexAt(pos).row()
        if row < 0:
            return
        rows = self._selected_sequence_rows()
        if not rows:
            return
        menu = QMenu(self)
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
        self._update_preview()

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
        if isinstance(spec, ImmediateSpec):
            self.runPlanRequested.emit(plan)
            return

        scheduler = self._resolve_scheduler()
        if scheduler is None:
            QMessageBox.critical(self, "调度器不可用", "当前调度器未初始化。")
            return
        payload = {"plan": plan.model_dump(mode="json")}
        try:
            entry = scheduler.submit(spec, payload=payload)
        except Exception as exc:
            logger.error("提交调度失败: {}", exc)
            QMessageBox.critical(self, "提交调度失败", "任务提交失败，请稍后重试。")
            return
        self.scheduledPlanPending.emit(entry.id)
        QMessageBox.information(self, "调度成功", "任务已提交，可在执行结果页查看。")

    def _delay_seconds(self) -> float:
        base = float(self._delay_value.value())
        return base * (3600.0 if self._delay_unit.currentText() == "小时" else 60.0)

    def _build_schedule_spec(self) -> ScheduleSpec:
        mode = self._schedule_mode.currentText()
        if mode == "立即执行":
            return ImmediateSpec()
        if mode == "N 时间后":
            return AfterSpec(delay_seconds=self._delay_seconds())
        if mode == "具体时刻":
            return AtSpec(run_at=self._run_at.dateTime().toPython().isoformat(timespec="seconds"))
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

    def _save_as_sop(self) -> None:
        sequence = [self._step_data(i) for i in range(self._sequence.count())]
        if not sequence:
            QMessageBox.information(self, "提示", "请先添加至少一个素材步骤。")
            return
        dialog = SopDialog(self)
        if not dialog.exec():
            return
        name, labels = dialog.result_data()
        if not name:
            return
        sop = SopTemplate(
            name=name,
            labels=labels,
            sequence=sequence,
            msg_gap_seconds=(float(self._msg_gap_min.value()), float(self._msg_gap_max.value())),
            contact_gap_seconds=(float(self._contact_gap_min.value()), float(self._contact_gap_max.value())),
            perturb=self._perturb.isChecked(),
        )
        save_sop(sop, account_id=self._account_id)
        self._reload_sops()
        QMessageBox.information(self, "保存成功", f"SOP 已保存：{name}")

    def _use_sop(self) -> None:
        sop = self._current_sop()
        if sop is None:
            QMessageBox.information(self, "提示", "请选择一个 SOP。")
            return
        self._stop_all_validations()
        self._sequence.clear()
        for row in sop.sequence:
            self._append_sequence_item(row)
        self._msg_gap_min.setValue(int(sop.msg_gap_seconds[0]))
        self._msg_gap_max.setValue(int(sop.msg_gap_seconds[1]))
        self._contact_gap_min.setValue(int(sop.contact_gap_seconds[0]))
        self._contact_gap_max.setValue(int(sop.contact_gap_seconds[1]))
        self._perturb.setChecked(sop.perturb)
        if sop.labels:
            matches = resolve_contacts_by_labels(
                sop.labels,
                self._contacts_all,
                account_id=self._account_id,
            )
            if not matches:
                QMessageBox.warning(self, "未命中联系人", f"SOP 标签未命中联系人：{'、'.join(sop.labels)}")
            else:
                options: list[str] = []
                text_to_contact: dict[str, PlanContact] = {}
                for contact in matches:
                    text = contact.display_name
                    if text in text_to_contact:
                        text = f"{contact.display_name} ({contact.conv_local})"
                    options.append(text)
                    text_to_contact[text] = contact
                dlg = ContactConfirmDialog(
                    self,
                    title=f"SOP 命中 {len(matches)} 人，请确认",
                    contacts=options,
                )
                if dlg.exec():
                    selected_names = dlg.selected_contacts()
                    selected: dict[str, PlanContact] = {}
                    for text in selected_names:
                        chosen = text_to_contact.get(text)
                        if chosen is None:
                            continue
                        selected[chosen.conv_local] = chosen
                    self._selected_contacts = selected
                    self._refresh_contact_list()
        self._update_preview()

    def _current_sop(self) -> SopTemplate | None:
        data = self._sop_pick.currentData()
        if not isinstance(data, dict):
            return None
        sop_id = str(data.get("id", "")).strip()
        for row in self._sops:
            if row.id == sop_id:
                return row
        return None

    def _reload_sops(self) -> None:
        self._sops = list_sops(self._account_id)
        self._sop_pick.clear()
        for row in self._sops:
            text = f"{row.name}（标签: {'、'.join(row.labels) if row.labels else '无'}）"
            self._sop_pick.addItem(text, {"id": row.id})

    def _resolve_scheduler(self) -> BroadcastScheduler | None:
        if self._scheduler_provider is None:
            return None
        try:
            return self._scheduler_provider()
        except Exception:
            return None

    def enqueue_schedule_entry(self, entry: ScheduleEntry) -> None:
        self._scheduleTriggered.emit(entry)

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
        for row in range(self._sequence.count()):
            if self._validation_status(row) != "ok":
                return False
        return True

    def _update_submit_enabled(self) -> None:
        count = self._sequence.count()
        if count < 1:
            self._btn_submit.setEnabled(False)
            self._btn_retry_step.setEnabled(False)
            return
        ready = self._all_steps_validated()
        self._btn_submit.setEnabled(ready)
        self._btn_retry_step.setEnabled(self._sequence.currentRow() >= 0)

    def _on_sequence_selection_changed(self, _row: int) -> None:
        row = self._sequence.currentRow()
        self._btn_retry_validate.setEnabled(row >= 0 and self._validation_status(row) == "fail")
        self._btn_retry_step.setEnabled(row >= 0)

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

    def _retry_selected_step(self) -> None:
        row = self._sequence.currentRow()
        if row < 0:
            return
        try:
            plan = self.build_plan()
        except Exception as exc:
            logger.warning("重试步骤时计划构建失败: {}", exc)
            QMessageBox.warning(self, "计划不可用", "当前计划配置不完整，请先修正后再重试。")
            return
        seq_size = len(plan.sequence)
        if seq_size < 1 or row >= seq_size:
            return
        indexes = [c_idx * seq_size + row for c_idx in range(len(plan.contacts))]
        self.runStepRequested.emit(plan, indexes)

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
        desc = str(item.data(_SEQUENCE_DESC_ROLE) or item.text()).strip()
        status = str(item.data(_VALIDATION_ROLE) or "pending").strip() or "pending"
        status_text, color = self._validation_status_text(status)

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(6, 0, 6, 0)
        row_layout.setSpacing(12)
        desc_label = QLabel(desc)
        status_label = QLabel(status_text)
        status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        row_layout.addWidget(desc_label, 1)
        row_layout.addWidget(status_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._sequence.setItemWidget(item, row_widget)

    def _sequence_text(self, payload: dict[str, Any]) -> str:
        step_type = str(payload.get("type", "unknown"))
        type_label = asset_type_to_zh(step_type)
        if step_type == "voice":
            return f"{type_label}：{payload.get('voice_id', '')}"
        if step_type == "text":
            text_kind = str(payload.get("text_kind", "text")).strip()
            if payload.get("template_id"):
                label = "小程序链接" if text_kind == "link" else "文字"
                return f"{label}：{payload.get('template_id')}"
            return f"文字：{payload.get('body', '')}"
        if step_type == "image":
            return f"{type_label}：{payload.get('path', '')}"
        if step_type == "oa":
            return f"{type_label}：{payload.get('template_id', '')}"
        if step_type == "video":
            return f"{type_label}：{payload.get('path', '')}"
        if step_type == "link":
            return f"小程序链接：{payload.get('url', '') or payload.get('template_id', '')}"
        if step_type == "bundle":
            return f"素材包：{payload.get('bundle_id', '')}"
        return f"{type_label}：{json.dumps(payload, ensure_ascii=False)}"

    def _update_preview(self) -> None:
        if not self._preview_group.isChecked():
            self._preview.setPlainText("（已折叠，展开后显示）")
            return
        try:
            plan = self.build_plan()
            payload = plan.model_dump(mode="json")
            schedule = self._schedule_preview_payload()
            text = json.dumps({"plan": payload, "schedule": schedule}, ensure_ascii=False, indent=2)
        except Exception as exc:
            text = f"Plan 当前不可用: {exc}"
        self._preview.setPlainText(text)

    def _schedule_preview_payload(self) -> dict[str, Any]:
        return self._build_schedule_spec().model_dump(mode="json")

