"""执行结果页的发送列表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from PySide6.QtCore import QDate, QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass
class BoardNode:
    title: str
    kind: str = "message"
    expanded: bool = True
    action: str = ""
    status: str = ""
    payload: dict = field(default_factory=dict)
    children: list["BoardNode"] = field(default_factory=list)


class _ActivationTree(QTreeWidget):
    """Emit the actual row under a viewport double click.

    QTreeWidget.itemDoubleClicked is not emitted reliably by every Qt/Windows
    style combination when the tree also contains index widgets (our action
    buttons).  Handling the viewport event keeps preview activation stable.
    """

    rowDoubleClicked = Signal(object)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._dbl_guard = False
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is self.viewport() and event.type() == QEvent.Type.MouseButtonDblClick:
            point = event.position().toPoint() if hasattr(event, "position") else event.pos()
            self._emit_double_click(self.itemAt(point))
            return True
        return super().eventFilter(watched, event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        point = event.position().toPoint() if hasattr(event, "position") else event.pos()
        item = self.itemAt(point)
        if item is None:
            item = self.itemAt(self.viewport().mapFrom(self, point))
        if item is not None:
            self._emit_double_click(item)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _emit_double_click(self, item: QTreeWidgetItem | None) -> None:
        if item is None or self._dbl_guard:
            return
        self._dbl_guard = True
        self.rowDoubleClicked.emit(item)
        QTimer.singleShot(0, self._release_double_click)

    def _release_double_click(self) -> None:
        self._dbl_guard = False


class SendBoard(QWidget):
    """日期、发送按钮、循环小框，以及左右两块发送列表。"""

    dateChanged = Signal(QDate)
    stopClicked = Signal()
    restartClicked = Signal()
    retryFailedClicked = Signal()
    briefToggled = Signal(bool)
    rowAction = Signal(str, object)
    nodeActivated = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._brief = False
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("日期："))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy年M月d日")
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.dateChanged.connect(self.dateChanged.emit)
        top.addWidget(self.date_edit)

        self.stop_button = QPushButton("立即停止")
        self.restart_button = QPushButton("重新发送任务")
        self.retry_button = QPushButton("重试所有失败项")
        self.brief_button = QPushButton("简略视图")
        self.brief_button.setCheckable(True)
        for button in (self.stop_button, self.retry_button):
            button.setEnabled(False)
        self.restart_button.setEnabled(True)
        self.stop_button.clicked.connect(self.stopClicked.emit)
        self.restart_button.clicked.connect(self.restartClicked.emit)
        self.retry_button.clicked.connect(self.retryFailedClicked.emit)
        self.brief_button.toggled.connect(self._on_brief)
        top.addWidget(self.stop_button)
        top.addWidget(self.restart_button)
        top.addWidget(self.retry_button)
        top.addWidget(self.brief_button)
        top.addStretch(1)

        self.cron_box = _ActivationTree()
        self.cron_box.setHeaderHidden(True)
        self.cron_box.setFixedWidth(180)
        self.cron_box.setMaximumHeight(72)
        self.cron_box.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cron_box.rowDoubleClicked.connect(self._emit_item)
        top.addWidget(self.cron_box)
        root.addLayout(top)

        root.addWidget(QLabel("发送列表"))
        columns = QHBoxLayout()
        self.executing = self._make_tree("正在执行计划")
        self.executing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.pending = self._make_tree("当日待执行计划")
        columns.addWidget(self._wrap("正在执行计划", self.executing), 1)
        columns.addWidget(self._wrap("当日待执行计划", self.pending), 1)
        root.addLayout(columns, 1)

    def _make_tree(self, name: str) -> QTreeWidget:
        tree = _ActivationTree()
        tree.setObjectName(name)
        tree.setColumnCount(3)
        tree.setHeaderLabels(["内容", "状态", "操作"])
        tree.setHeaderHidden(False)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.setExpandsOnDoubleClick(False)
        header = tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        tree.rowDoubleClicked.connect(self._emit_item)
        return tree

    def _wrap(self, title: str, tree: QTreeWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title))
        layout.addWidget(tree, 1)
        return box

    def selected_date(self) -> date:
        chosen = self.date_edit.date()
        return date(chosen.year(), chosen.month(), chosen.day())

    def set_running(self, running: bool, *, paused: bool, can_retry: bool, can_restart: bool = False) -> None:
        self.stop_button.setEnabled(running or paused)
        self.stop_button.setText("接续开始" if paused and not running else "立即停止")
        # 点下去再选具体任务。没有可发任务时由点击处理提示，不把按钮变灰。
        self.restart_button.setEnabled(True)
        self.retry_button.setEnabled(running and can_retry)

    def set_lists(
        self,
        *,
        executing: list[BoardNode],
        pending: list[BoardNode],
        cron: list[BoardNode],
    ) -> None:
        self._fill(self.executing, executing)
        self._fill(self.pending, pending)
        self._fill(self.cron_box, cron)
        if self._brief:
            self._collapse_plans()

    def _on_brief(self, checked: bool) -> None:
        self._brief = checked
        self.brief_button.setText("恢复展开" if checked else "简略视图")
        if checked:
            self._collapse_plans()
        else:
            self._expand_default(self.executing)
            self._expand_default(self.pending)
        self.briefToggled.emit(checked)

    def _collapse_plans(self) -> None:
        for tree in (self.executing, self.pending):
            tree.collapseAll()

    def _expand_default(self, tree: QTreeWidget) -> None:
        def walk(item: QTreeWidgetItem) -> None:
            payload = item.data(0, Qt.ItemDataRole.UserRole)
            kind = str(payload.get("kind", "")) if isinstance(payload, dict) else ""
            item.setExpanded(kind != "bundle_atomic")
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(tree.topLevelItemCount()):
            walk(tree.topLevelItem(index))

    def _item(self, node: BoardNode, columns: int) -> QTreeWidgetItem:
        if columns >= 3:
            item = QTreeWidgetItem([node.title, node.status, ""])
        else:
            item = QTreeWidgetItem([node.title])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        payload = dict(node.payload)
        payload.setdefault("kind", node.kind)
        payload["action"] = node.action
        item.setData(0, Qt.ItemDataRole.UserRole, payload)
        self._paint(item, node, columns)
        if node.action and columns >= 3:
            button = QPushButton("重新发送" if node.action == "retry" else "接续开始")
            button.clicked.connect(lambda _checked=False, data=dict(payload): self.rowAction.emit(data.get("action", ""), data))
            self._pending_buttons.append((item, button))
        for child in node.children:
            item.addChild(self._item(child, columns))
        return item

    def _paint(self, item: QTreeWidgetItem, node: BoardNode, columns: int) -> None:
        bold_kinds = {"plan", "pending", "cron", "contact", "bundle", "bundle_atomic"}
        if node.kind in bold_kinds:
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        if node.kind in {"plan", "pending"}:
            band = QBrush(QColor("#f3f4f6"))
            for column in range(max(1, columns)):
                item.setBackground(column, band)
        if node.kind == "gap":
            font = QFont(item.font(0))
            font.setItalic(True)
            font.setBold(False)
            item.setFont(0, font)
            item.setForeground(0, QBrush(QColor("#6b7280")))
        if columns < 2 or not node.status:
            return
        color, emphasize = _status_style(node.status)
        if not color:
            return
        item.setForeground(1, QBrush(QColor(color)))
        if emphasize:
            font = item.font(1)
            font.setBold(True)
            item.setFont(1, font)

    def _fill(self, tree: QTreeWidget, nodes: list[BoardNode]) -> None:
        self._pending_buttons: list[tuple[QTreeWidgetItem, QPushButton]] = []
        columns = tree.columnCount()
        tree.clear()
        for node in nodes:
            tree.addTopLevelItem(self._item(node, columns))
        action_column = 2 if columns >= 3 else 1
        for item, button in self._pending_buttons:
            tree.setItemWidget(item, action_column, button)
        if not self._brief and tree is not self.cron_box:
            self._expand_default(tree)

    def _emit_item(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        if item is None:
            return
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("kind", ""))
        if kind in {"message", "pending", "cron"}:
            self.nodeActivated.emit(dict(payload))
            return
        if item.childCount() > 0:
            item.setExpanded(not item.isExpanded())

def _status_style(status: str) -> tuple[str, bool]:
    text = status.strip()
    if "发送失败" in text:
        return "#b91c1c", True
    if "已暂停" in text:
        return "#c2410c", True
    if text == "发送成功":
        return "#15803d", False
    if text in {"30%", "70%"} or "30%" in text or "70%" in text:
        return "#1d4ed8", False
    if text == "未开始":
        return "#6b7280", False
    return "", False
