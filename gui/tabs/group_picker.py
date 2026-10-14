"""群公告页左侧：只列出外部群。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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
    QVBoxLayout,
    QWidget,
)

from app.broadcast.dto import ContactRowDTO, PlanContact
from gui.i18n import CHOICE_BUTTON_STYLE

_EXTERNAL_GROUP = "外部群"


class GroupPicker(QWidget):
    """搜索、勾选、确认外部群。数据由联系人刷新结果喂入。"""

    selectedGroupsChanged = Signal(list)
    refreshRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ContactRowDTO] = []
        self._signature: tuple[tuple[str, str], ...] | None = None
        self._selected_conv: set[str] = set()
        self._confirmed_conv: set[str] | None = None
        self._building = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索：群聊名称")
        self._btn_refresh = QPushButton("刷新群聊")
        top.addWidget(QLabel("搜索"))
        top.addWidget(self._search, 1)
        top.addWidget(self._btn_refresh)
        root.addLayout(top)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.hide()
        root.addWidget(self._progress)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 2)
        self._btn_select_all = QPushButton("全选")
        self._btn_select_all.setCheckable(True)
        self._btn_select_all.setToolTip("全选 / 取消全选当前列表中的可见群聊")
        self._btn_select_all.setStyleSheet(CHOICE_BUTTON_STYLE)
        self._btn_select_all.setFixedSize(52, 28)
        header_row.addWidget(self._btn_select_all, 0, Qt.AlignmentFlag.AlignLeft)
        header_row.addStretch(1)
        root.addLayout(header_row)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["", "群聊名称"])
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 32)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self._table, 1)

        action_row = QHBoxLayout()
        self._btn_confirm = QPushButton("确认群聊")
        self._btn_reselect = QPushButton("重选群聊")
        self._checked_count = QLabel("已勾选：0个")
        action_row.addWidget(self._btn_confirm)
        action_row.addWidget(self._btn_reselect)
        action_row.addStretch(1)
        action_row.addWidget(self._checked_count)
        root.addLayout(action_row)

        self._search.textChanged.connect(self._rebuild)
        self._btn_refresh.clicked.connect(self.refreshRequested)
        self._btn_confirm.clicked.connect(self._on_confirm)
        self._btn_reselect.clicked.connect(self._on_reselect)
        self._btn_select_all.toggled.connect(self._on_select_all_toggled)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellDoubleClicked.connect(self._toggle_row_selection)

    def set_refreshing(self, refreshing: bool) -> None:
        if refreshing:
            self._progress.show()
            return
        self._progress.hide()

    def set_rows(self, rows: object) -> None:
        items = list(rows) if isinstance(rows, list) else []
        groups = [row for row in items if _is_external_group(row)]
        signature = tuple((row.conv_local, row.display_name) for row in groups)
        if signature == self._signature:
            return
        self._signature = signature
        self._rows = groups
        alive = {row.conv_local for row in groups}
        self._selected_conv &= alive
        if self._confirmed_conv is not None:
            self._confirmed_conv &= alive
        self._rebuild()

    def selected_groups(self) -> list[PlanContact]:
        chosen: list[PlanContact] = []
        for row in self._rows:
            if row.conv_local not in self._selected_conv:
                continue
            name = row.display_name.strip()
            if not name:
                continue
            chosen.append(PlanContact(conv_local=row.conv_local, display_name=name))
        return chosen

    def _on_confirm(self) -> None:
        if not self._selected_conv:
            QMessageBox.information(self, "请先勾选", "请先勾选群聊。")
            return
        self._confirmed_conv = set(self._selected_conv)
        self._rebuild()

    def _on_reselect(self) -> None:
        self._confirmed_conv = None
        self._selected_conv.clear()
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._rebuild()

    def _on_select_all_toggled(self, checked: bool) -> None:
        for row in self._visible_rows():
            if checked:
                self._selected_conv.add(row.conv_local)
            else:
                self._selected_conv.discard(row.conv_local)
        self._rebuild()

    def _rebuild(self) -> None:
        self._building = True
        try:
            visible = self._visible_rows()
            self._table.setRowCount(len(visible))
            for index, row in enumerate(visible):
                self._fill_row(index, row)
            self._refresh_selected()
            self._sync_select_all_button(visible)
        finally:
            self._building = False

    def _visible_rows(self) -> list[ContactRowDTO]:
        keyword = self._search.text().strip().lower()
        return [row for row in self._rows if self._match_confirmed(row) and self._match_search(row, keyword)]

    def _match_confirmed(self, row: ContactRowDTO) -> bool:
        if self._confirmed_conv is None:
            return True
        return row.conv_local in self._confirmed_conv

    @staticmethod
    def _match_search(row: ContactRowDTO, keyword: str) -> bool:
        if not keyword:
            return True
        corpus = f"{row.display_name} {row.peer_name}".lower()
        return keyword in corpus

    def _fill_row(self, index: int, row: ContactRowDTO) -> None:
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        check.setCheckState(
            Qt.CheckState.Checked if row.conv_local in self._selected_conv else Qt.CheckState.Unchecked
        )
        check.setData(Qt.ItemDataRole.UserRole, row.conv_local)
        self._table.setItem(index, 0, check)

        name_item = QTableWidgetItem(row.display_name.strip())
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name_item.setData(Qt.ItemDataRole.UserRole, row.conv_local)
        self._table.setItem(index, 1, name_item)

    def _sync_select_all_button(self, visible: list[ContactRowDTO]) -> None:
        all_selected = bool(visible) and all(row.conv_local in self._selected_conv for row in visible)
        self._btn_select_all.blockSignals(True)
        try:
            self._btn_select_all.setEnabled(bool(visible))
            self._btn_select_all.setChecked(all_selected)
        finally:
            self._btn_select_all.blockSignals(False)

    def _toggle_row_selection(self, row: int, _column: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        target = (
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        item.setCheckState(target)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._building or item.column() != 0:
            return
        conv_local = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not conv_local:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_conv.add(conv_local)
        else:
            self._selected_conv.discard(conv_local)
        self._refresh_selected()
        self._sync_select_all_button(self._visible_rows())

    def _refresh_selected(self) -> None:
        self._checked_count.setText(f"已勾选：{len(self._selected_conv)}个")
        self.selectedGroupsChanged.emit(self.selected_groups())


def _is_external_group(row: object) -> bool:
    if not isinstance(row, ContactRowDTO):
        return False
    return row.category == _EXTERNAL_GROUP and bool(row.conv_local.strip()) and bool(row.display_name.strip())
