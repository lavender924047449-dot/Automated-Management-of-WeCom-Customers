"""让界面上的文字可以用 Ctrl+C 或右键「复制」复制。

输入框里已经选中文字时，仍由控件自己复制。表格只复制当前选中的格子，不把整行带上。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QContextMenuEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QTableWidget,
    QTextEdit,
    QTreeWidget,
    QWidget,
)

_SELECTABLE = Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
_EDITORS = (QLineEdit, QTextEdit, QPlainTextEdit)


def install_copy_support(app: QApplication) -> None:
    filt = _CopyFilter(app)
    app.installEventFilter(filt)
    app.setProperty("_copy_support_filter", filt)


def add_copy_action(menu: QMenu, widget: QWidget) -> QAction:
    """在已有右键菜单顶部加入「复制」。点选后复制该控件里当前选中的文字。"""
    action = QAction("复制", menu)
    menu.insertAction(menu.actions()[0] if menu.actions() else None, action)

    def _copy() -> None:
        text = text_for_clipboard(widget)
        if text:
            QApplication.clipboard().setText(text)

    action.triggered.connect(_copy)
    return action


def prepare_context_selection(view: QAbstractItemView, index) -> None:
    """右键落在未选中的格子上时，只选中这个格子，保留已经选好的多格。"""
    if not index.isValid():
        return
    model = view.selectionModel()
    if model is None:
        view.setCurrentIndex(index)
        return
    if model.isSelected(index):
        view.setCurrentIndex(index)
        return
    from PySide6.QtCore import QItemSelectionModel

    view.setCurrentIndex(index)
    model.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)


def copy_text_from_widget(widget: QWidget | None) -> str | None:
    """返回要写入剪贴板的文字。

    ``None`` 表示这次按键交给控件自己处理（例如输入框里已经选中了一段字）。
    """
    if widget is None:
        return ""
    if isinstance(widget, _EDITORS):
        return _readonly_editor_text(widget)
    view = _item_view_of(widget)
    if view is not None and widget in {view, view.viewport()}:
        widget = view
    elif not isinstance(widget, (QTableWidget, QListWidget, QTreeWidget, QComboBox, QLabel)):
        return ""
    if isinstance(widget, QTableWidget):
        return _table_text(widget)
    if isinstance(widget, QListWidget):
        return _list_text(widget)
    if isinstance(widget, QTreeWidget):
        return _tree_text(widget)
    if isinstance(widget, QComboBox):
        return widget.currentText().strip()
    if isinstance(widget, QLabel):
        if widget.selectedText().strip():
            return None
        return _label_text(widget)
    return ""


def text_for_clipboard(widget: QWidget | None) -> str:
    """右键「复制」要用的文字。有选区时复制选区，否则复制当前格子或整段只读文字。"""
    if widget is None:
        return ""
    selected = _explicit_selection(widget)
    if selected:
        return selected
    text = copy_text_from_widget(widget)
    return "" if text is None else text


def _explicit_selection(widget: QWidget) -> str:
    if isinstance(widget, QLineEdit):
        return widget.selectedText()
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return widget.textCursor().selectedText().replace("\u2029", "\n")
    if isinstance(widget, QLabel):
        return widget.selectedText().strip()
    return ""


def _readonly_editor_text(widget: QLineEdit | QTextEdit | QPlainTextEdit) -> str | None:
    if isinstance(widget, QLineEdit):
        if widget.selectedText() or not widget.isReadOnly():
            return None
        return widget.text().strip()
    if widget.textCursor().hasSelection() or not widget.isReadOnly():
        return None
    return widget.toPlainText().strip()


def _label_text(label: QLabel) -> str:
    return label.text().replace("\u200b", "").strip()


def _table_text(table: QTableWidget) -> str:
    indexes = [index for index in table.selectedIndexes() if index.isValid()]
    if not indexes:
        current = table.currentIndex()
        if current.isValid():
            indexes = [current]
    indexes.sort(key=lambda index: (index.row(), index.column()))
    lines: list[str] = []
    row_no: int | None = None
    cells: list[str] = []
    for index in indexes:
        if row_no is None:
            row_no = index.row()
        elif index.row() != row_no:
            lines.append("\t".join(cells))
            cells = []
            row_no = index.row()
        item = table.item(index.row(), index.column())
        cells.append(item.text().strip() if item is not None else "")
    if cells:
        lines.append("\t".join(cells))
    return "\n".join(line for line in lines if line.strip())


def _list_text(widget: QListWidget) -> str:
    items = list(widget.selectedItems())
    if not items and widget.currentItem() is not None:
        items = [widget.currentItem()]
    items.sort(key=widget.row)
    lines: list[str] = []
    for item in items:
        if item is None:
            continue
        text = item.text().strip()
        if not text:
            hosted = widget.itemWidget(item)
            if hosted is not None:
                parts = [_label_text(label) for label in hosted.findChildren(QLabel)]
                text = " ".join(part for part in parts if part)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _tree_text(widget: QTreeWidget) -> str:
    indexes = [index for index in widget.selectedIndexes() if index.isValid()]
    if indexes:
        indexes.sort(key=lambda index: (index.row(), index.column()))
        lines: list[str] = []
        row_key: tuple[int, int] | None = None
        cells: list[str] = []
        for index in indexes:
            key = (id(index.internalPointer()), index.row())
            if row_key is None:
                row_key = key
            elif key != row_key:
                lines.append("\t".join(cells))
                cells = []
                row_key = key
            cells.append(str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip())
        if cells:
            lines.append("\t".join(cells))
        return "\n".join(line for line in lines if line.strip())
    items = list(widget.selectedItems())
    if not items:
        current = widget.currentItem()
        if current is not None:
            items = [current]
    lines = []
    columns = max(1, widget.columnCount())
    for item in items:
        cells = [item.text(column).strip() for column in range(columns)]
        text = "\t".join(cell for cell in cells if cell)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _item_view_of(widget: QWidget | None) -> QAbstractItemView | None:
    if isinstance(widget, QAbstractItemView):
        return widget
    if widget is None:
        return None
    parent = widget.parentWidget()
    if isinstance(parent, QAbstractItemView):
        return parent
    if parent is not None:
        grand = parent.parentWidget()
        if isinstance(grand, QAbstractItemView) and parent is grand.viewport():
            return grand
    return None


class _CopyFilter(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if isinstance(watched, QLabel) and event.type() in {QEvent.Type.Show, QEvent.Type.Polish}:
            self._make_label_selectable(watched)
        if event.type() == QEvent.Type.ContextMenu and isinstance(event, QContextMenuEvent):
            return self._on_context_menu(watched, event)
        if event.type() != QEvent.Type.KeyPress or not event.matches(QKeySequence.StandardKey.Copy):
            return False
        target = watched if isinstance(watched, QWidget) else QApplication.focusWidget()
        text = copy_text_from_widget(target)
        if text is None or not text:
            return False
        QApplication.clipboard().setText(text)
        return True

    def _on_context_menu(self, watched: QObject, event: QContextMenuEvent) -> bool:
        if not isinstance(watched, QWidget) or isinstance(watched, _EDITORS):
            return False
        if isinstance(watched, QHeaderView):
            return self._popup_header(watched, event)
        view = _item_view_of(watched)
        if view is not None and watched in {view, view.viewport()}:
            if watched is view.viewport():
                pos = event.pos()
            else:
                pos = view.viewport().mapFromGlobal(event.globalPos())
            prepare_context_selection(view, view.indexAt(pos))
            if view.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu:
                return False
            return self._popup_copy(watched, event, text_for_clipboard(view))
        if isinstance(watched, (QLabel, QComboBox)):
            return self._popup_copy(watched, event, text_for_clipboard(watched))
        return False

    @staticmethod
    def _popup_header(header: QHeaderView, event: QContextMenuEvent) -> bool:
        logical = header.logicalIndexAt(event.pos())
        model = header.model()
        if logical < 0 or model is None:
            return False
        text = str(model.headerData(logical, header.orientation(), Qt.ItemDataRole.DisplayRole) or "").strip()
        return _CopyFilter._popup_copy(header, event, text)

    @staticmethod
    def _popup_copy(widget: QWidget, event: QContextMenuEvent, text: str) -> bool:
        if not text:
            return False
        menu = QMenu(widget)
        action = menu.addAction("复制")
        chosen = menu.exec(event.globalPos())
        if chosen == action:
            QApplication.clipboard().setText(text)
        return True

    @staticmethod
    def _make_label_selectable(label: QLabel) -> None:
        if label.property("_copy_ready"):
            return
        label.setTextInteractionFlags(label.textInteractionFlags() | _SELECTABLE)
        if label.focusPolicy() == Qt.FocusPolicy.NoFocus:
            label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        label.setProperty("_copy_ready", True)
