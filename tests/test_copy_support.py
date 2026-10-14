from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QMenu, QTableWidget, QTableWidgetItem

from gui.copy_support import add_copy_action, copy_text_from_widget, install_copy_support


def test_ctrl_c_copies_selected_cells_only(qtbot) -> None:
    install_copy_support(QApplication.instance())
    table = QTableWidget(1, 2)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
    table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
    qtbot.addWidget(table)
    table.setItem(0, 0, QTableWidgetItem("文字"))
    table.setItem(0, 1, QTableWidgetItem("你好"))
    table.show()
    table.setCurrentCell(0, 1)
    table.setFocus()
    qtbot.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "你好"


def test_right_click_menu_copies_the_clicked_cell(qtbot) -> None:
    install_copy_support(QApplication.instance())
    table = QTableWidget(1, 2)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
    qtbot.addWidget(table)
    table.setItem(0, 0, QTableWidgetItem("文字"))
    table.setItem(0, 1, QTableWidgetItem("你好"))
    table.resize(420, 180)
    table.show()
    table.setCurrentCell(0, 0)

    def accept_copy() -> None:
        for widget in QApplication.topLevelWidgets():
            if not isinstance(widget, QMenu) or not widget.isVisible():
                continue
            for action in widget.actions():
                if action.text() == "复制":
                    action.trigger()
            widget.close()

    QTimer.singleShot(0, accept_copy)
    index = table.model().index(0, 1)
    pos = table.visualRect(index).center()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, table.viewport().mapToGlobal(pos))
    QApplication.sendEvent(table.viewport(), event)
    assert QApplication.clipboard().text() == "你好"
    assert (table.currentRow(), table.currentColumn()) == (0, 1)


def test_existing_menu_copy_action_uses_selected_cells(qtbot) -> None:
    table = QTableWidget(1, 2)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
    qtbot.addWidget(table)
    table.setItem(0, 0, QTableWidgetItem("文字"))
    table.setItem(0, 1, QTableWidgetItem("你好"))
    table.setCurrentCell(0, 0)
    menu = QMenu()
    action = add_copy_action(menu, table)
    action.trigger()
    assert QApplication.clipboard().text() == "文字"
    assert menu.actions()[0].text() == "复制"


def test_ctrl_c_keeps_line_edit_selection(qtbot) -> None:
    install_copy_support(QApplication.instance())
    edit = QLineEdit("abcdef")
    qtbot.addWidget(edit)
    edit.show()
    edit.setSelection(0, 3)
    edit.setFocus()
    qtbot.keyClick(edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "abc"


def test_ctrl_c_copies_readonly_text_and_label(qtbot) -> None:
    install_copy_support(QApplication.instance())
    edit = QLineEdit("只读正文")
    edit.setReadOnly(True)
    qtbot.addWidget(edit)
    edit.show()
    edit.deselect()
    edit.setFocus()
    qtbot.keyClick(edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "只读正文"


def test_label_text_can_be_copied(qtbot) -> None:
    install_copy_support(QApplication.instance())
    label = QLabel("联系人备注")
    qtbot.addWidget(label)
    label.show()
    assert copy_text_from_widget(label) == "联系人备注"
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
