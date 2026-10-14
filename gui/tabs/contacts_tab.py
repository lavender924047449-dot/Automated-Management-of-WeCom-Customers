from __future__ import annotations

from loguru import logger
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.contacts import refresh_contacts as refresh_contacts_facade
from app.broadcast.dto import ContactRowDTO, PlanContact
from app.broadcast.labels import LocalLabelStore
from gui.i18n import CHOICE_BUTTON_STYLE
from gui.widgets.label_multiselect import LabelMultiSelect


class _RefreshWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, *, account_id: str, refresh: bool, include_cached_friends: bool = False) -> None:
        super().__init__()
        self.account_id = account_id
        self.refresh = refresh
        self.include_cached_friends = include_cached_friends

    def run(self) -> None:
        try:
            rows = refresh_contacts_facade(
                self.account_id,
                refresh=self.refresh,
                include_cached_friends=self.include_cached_friends,
            )
            self.finished.emit(rows)
        except Exception as exc:  # pragma: no cover - GUI thread fallback
            self.failed.emit(str(exc))


class ContactsTab(QWidget):
    """联系人页：支持分类、搜索、标签批量选择。"""

    selectedContactsChanged = Signal(list)
    contactsSnapshotChanged = Signal(list)
    refreshStateChanged = Signal(bool)

    _TAB_ALL = "全部"
    _TAB_CUSTOMER = "外部客户"
    _TAB_GROUP = "外部群"
    _TAB_INTERNAL = "内部同事"

    def __init__(self) -> None:
        super().__init__()
        self._account_id = "default"
        self._rows: list[ContactRowDTO] = []
        self._selected_conv: set[str] = set()
        self._selected_contacts: list[PlanContact] = []
        self._thread: QThread | None = None
        self._worker: _RefreshWorker | None = None
        self._show_refresh_error = True
        self._store = LocalLabelStore.load(account_id=self._account_id)
        self._building = False
        self._active_filter_labels: set[str] = set()
        # None：完整选择页。有值：只显示确认时勾中的人，取消勾选也不会把人移出这批。
        self._confirmed_conv: set[str] | None = None
        self._tables: dict[str, QTableWidget] = {}
        self._select_all_buttons: list[QPushButton] = []
        self._pending_retry_success_toast = False
        self._build_ui()
        self._rebuild()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索：备注/标签")
        self._btn_refresh = QPushButton("刷新联系人")
        top.addWidget(QLabel("搜索"))
        top.addWidget(self._search, 1)
        top.addWidget(self._btn_refresh)
        root.addLayout(top)

        self._labels = LabelMultiSelect()
        root.addWidget(self._labels)

        self._progress = QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._progress.setTextVisible(False)
        self._progress.hide()
        root.addWidget(self._progress)

        self._tabs = QTabWidget()
        for title in [self._TAB_ALL, self._TAB_CUSTOMER, self._TAB_GROUP, self._TAB_INTERNAL]:
            panel = QWidget()
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            panel_layout.setSpacing(0)

            header_row = QHBoxLayout()
            header_row.setContentsMargins(0, 0, 0, 2)
            btn_select_all = QPushButton("全选")
            btn_select_all.setCheckable(True)
            btn_select_all.setToolTip("全选 / 取消全选当前列表中的可见联系人")
            btn_select_all.setStyleSheet(CHOICE_BUTTON_STYLE)
            btn_select_all.setFixedSize(52, 28)
            btn_select_all.toggled.connect(self._on_select_all_toggled)
            header_row.addWidget(btn_select_all, 0, Qt.AlignmentFlag.AlignLeft)
            header_row.addStretch(1)
            panel_layout.addLayout(header_row)

            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(["", "备注名", "标签", "类型"])
            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(0, 32)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            table.verticalHeader().setVisible(False)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
            table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
            table.setEditTriggers(
                QTableWidget.EditTrigger.DoubleClicked
                | QTableWidget.EditTrigger.SelectedClicked
                | QTableWidget.EditTrigger.EditKeyPressed
            )
            table.horizontalHeader().setStretchLastSection(True)
            table.itemChanged.connect(self._on_item_changed)
            table.cellDoubleClicked.connect(self._toggle_row_selection)
            panel_layout.addWidget(table, 1)

            self._tabs.addTab(panel, title)
            self._tables[title] = table
            self._select_all_buttons.append(btn_select_all)
        root.addWidget(self._tabs, 1)

        action_row = QHBoxLayout()
        self._btn_confirm = QPushButton("确认联系人")
        self._btn_reselect = QPushButton("重选联系人")
        self._checked_count = QLabel("已勾选：0人")
        action_row.addWidget(self._btn_confirm)
        action_row.addWidget(self._btn_reselect)
        action_row.addStretch(1)
        action_row.addWidget(self._checked_count)
        root.addLayout(action_row)

        self._search.textChanged.connect(self._rebuild)
        self._btn_refresh.clicked.connect(lambda: self.refresh_contacts(force_refresh=True, show_error=True))
        self._btn_confirm.clicked.connect(self._on_confirm_contacts)
        self._btn_reselect.clicked.connect(self._on_reselect_contacts)
        self._labels.labelToggled.connect(self._on_label_filter_toggled)
        self._tabs.currentChanged.connect(lambda _idx: self._rebuild())

    def set_account(self, account_id: str) -> None:
        self._account_id = account_id.strip() or "default"
        self._store = LocalLabelStore.load(account_id=self._account_id)
        self._selected_conv.clear()
        self._selected_contacts = []
        self._confirmed_conv = None
        self._rows = []
        self._active_filter_labels.clear()
        self._labels.reset_checks()
        for btn in self._select_all_buttons:
            btn.setChecked(False)
        self._rebuild()
        # 账号切换：立即强制刷新一次（Frida 拉 conv 列表），失败要看得见
        self.refresh_contacts(force_refresh=True, show_error=True)

    def refresh_contacts(self, *, force_refresh: bool, show_error: bool = True) -> None:
        if self._thread is not None:
            return
        self.refreshStateChanged.emit(True)
        self._show_refresh_error = bool(show_error)
        self._progress.show()
        self._progress.setRange(0, 0)
        self._thread = QThread(self)
        self._worker = _RefreshWorker(
            account_id=self._account_id,
            refresh=force_refresh,
            include_cached_friends=True,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_refresh_done)
        self._worker.failed.connect(self._on_refresh_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_worker)
        self._thread.start()

    def selected_contact_names(self) -> list[str]:
        return [item.display_name for item in self._selected_contacts]

    def _on_refresh_done(self, rows: list[ContactRowDTO]) -> None:
        self._rows = list(rows)
        self._rebuild()
        if self._pending_retry_success_toast:
            self._pending_retry_success_toast = False
            QMessageBox.information(self, "联系人刷新成功", "您的账号已成功入库")
        # 注：空结果时 refresh_contacts_facade 已经会 RuntimeError 带上 Frida 侧诊断日志
        # 走 _on_refresh_failed 弹窗，这里不再重复提示。

    def _on_refresh_failed(self, err: str) -> None:
        logger.warning("联系人刷新失败 account_id={} err={}", self._account_id, err)
        if self._show_refresh_error:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("联系人刷新失败")
            box.setText("联系人刷新失败，请您登录企微后刷新重试")
            retry_btn = box.addButton("重试", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("好的", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is retry_btn:
                self._pending_retry_success_toast = True
                self.refresh_contacts(force_refresh=True, show_error=True)

    def _cleanup_worker(self) -> None:
        self._progress.hide()
        self.refreshStateChanged.emit(False)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._show_refresh_error = True

    def _on_confirm_contacts(self) -> None:
        if not self._selected_conv:
            QMessageBox.information(self, "请先勾选", "请先勾选联系人。")
            return
        self._confirmed_conv = set(self._selected_conv)
        self._rebuild()

    def _on_reselect_contacts(self) -> None:
        self._confirmed_conv = None
        self._selected_conv.clear()
        self._active_filter_labels.clear()
        self._labels.reset_checks()
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._tabs.blockSignals(True)
        self._tabs.setCurrentIndex(0)
        self._tabs.blockSignals(False)
        self._rebuild()

    def _on_label_filter_toggled(self, label: str, checked: bool) -> None:
        if checked:
            self._active_filter_labels.add(label)
        else:
            self._active_filter_labels.discard(label)
        self._rebuild()

    def _on_select_all_toggled(self, checked: bool) -> None:
        tab_name = self._current_tab_name()
        for row in self._visible_rows(tab_name):
            if checked:
                self._selected_conv.add(row.conv_local)
            else:
                self._selected_conv.discard(row.conv_local)
        self._rebuild()

    def _rebuild(self) -> None:
        self._building = True
        try:
            keyword = self._search.text().strip().lower()
            labels = sorted({x for row in self._rows for x in row.labels})
            self._labels.set_labels(labels, checked=self._active_filter_labels)
            for tab_name, table in self._tables.items():
                visible_rows = self._visible_rows(tab_name, keyword=keyword)
                table.setRowCount(len(visible_rows))
                for i, row in enumerate(visible_rows):
                    self._fill_table_row(table, i, row)
            self._refresh_selected_names()
            self._sync_select_all_button()
            self.contactsSnapshotChanged.emit(list(self._rows))
        finally:
            self._building = False

    def _current_tab_name(self) -> str:
        return self._tabs.tabText(self._tabs.currentIndex())

    def _visible_rows(self, tab_name: str, *, keyword: str | None = None) -> list[ContactRowDTO]:
        query = self._search.text().strip().lower() if keyword is None else keyword
        # 搜索词与已选标签取交集：有搜索词时必须命中，有标签时每个已选标签都要有。
        return [
            row
            for row in self._rows
            if self._match_confirmed(row)
            and self._match_tab(tab_name, row)
            and self._match_search(row, query)
            and self._match_label_filter(row)
        ]

    def _sync_select_all_button(self) -> None:
        visible = self._visible_rows(self._current_tab_name())
        all_selected = bool(visible) and all(row.conv_local in self._selected_conv for row in visible)
        for btn in self._select_all_buttons:
            btn.blockSignals(True)
            try:
                btn.setEnabled(bool(visible))
                btn.setChecked(all_selected)
            finally:
                btn.blockSignals(False)

    def _fill_table_row(self, table: QTableWidget, index: int, row: ContactRowDTO) -> None:
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        check.setCheckState(
            Qt.CheckState.Checked if row.conv_local in self._selected_conv else Qt.CheckState.Unchecked
        )
        check.setData(Qt.ItemDataRole.UserRole, row.conv_local)
        table.setItem(index, 0, check)

        shown_name = row.display_name.strip() or row.peer_name.strip()
        name_item = QTableWidgetItem(shown_name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name_item.setData(Qt.ItemDataRole.UserRole, row.conv_local)
        table.setItem(index, 1, name_item)
        label_item = QTableWidgetItem("、".join(row.labels))
        label_item.setData(Qt.ItemDataRole.UserRole, row.conv_local)
        table.setItem(index, 2, label_item)
        kind_item = QTableWidgetItem(row.category)
        kind_item.setFlags(kind_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(index, 3, kind_item)

    def _toggle_row_selection(self, row: int, _column: int) -> None:
        table = self.sender()
        if not isinstance(table, QTableWidget):
            return
        item = table.item(row, 0)
        if item is None:
            return
        target = (
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        item.setCheckState(target)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._building:
            return
        if item.column() == 0:
            conv_local = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not conv_local:
                return
            if item.checkState() == Qt.CheckState.Checked:
                self._selected_conv.add(conv_local)
            else:
                self._selected_conv.discard(conv_local)
            self._refresh_selected_names()
            self._sync_check_state(conv_local, item.checkState())
            self._sync_select_all_button()
            return
        if item.column() == 2:
            conv_local = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not conv_local:
                return
            labels = [x.strip() for x in item.text().replace("、", ",").split(",") if x.strip()]
            self._store.set_labels(conv_local, labels)
            self._store.save()
            self._update_row_labels(conv_local, labels)
            self._rebuild()

    def _sync_check_state(self, conv_local: str, state: Qt.CheckState) -> None:
        self._building = True
        try:
            for table in self._tables.values():
                for row in range(table.rowCount()):
                    item = table.item(row, 0)
                    if item is None:
                        continue
                    if str(item.data(Qt.ItemDataRole.UserRole) or "") == conv_local:
                        item.setCheckState(state)
        finally:
            self._building = False

    def _refresh_selected_names(self) -> None:
        rows = [row for row in self._rows if row.conv_local in self._selected_conv]
        self._selected_contacts = [
            PlanContact(conv_local=row.conv_local, display_name=row.display_name)
            for row in rows
        ]
        self._checked_count.setText(f"已勾选：{len(self._selected_conv)}人")
        self.selectedContactsChanged.emit(list(self._selected_contacts))

    def _update_row_labels(self, conv_local: str, labels: list[str]) -> None:
        for row in self._rows:
            if row.conv_local == conv_local:
                row.labels = list(labels)

    def _match_confirmed(self, row: ContactRowDTO) -> bool:
        if self._confirmed_conv is None:
            return True
        return row.conv_local in self._confirmed_conv

    def _match_tab(self, tab_name: str, row: ContactRowDTO) -> bool:
        if tab_name == self._TAB_ALL:
            return True
        return row.category == tab_name

    def _match_label_filter(self, row: ContactRowDTO) -> bool:
        """已选标签取交集：联系人必须同时带有每一个已选标签。未选时不限制。"""
        if not self._active_filter_labels:
            return True
        row_labels = set(row.labels)
        return all(label in row_labels for label in self._active_filter_labels)

    @staticmethod
    def _match_search(row: ContactRowDTO, keyword: str) -> bool:
        if not keyword:
            return True
        corpus = f"{row.display_name} {row.peer_name} {' '.join(row.labels)}".lower()
        return keyword in corpus
