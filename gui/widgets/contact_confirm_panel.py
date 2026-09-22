from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout


class ContactConfirmDialog(QDialog):
    """SOP 命中联系人确认面板（可勾选）。"""

    def __init__(self, parent=None, *, title: str, contacts: list[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._build_ui(contacts)

    def _build_ui(self, contacts: list[str]) -> None:
        root = QVBoxLayout(self)
        self._list = QListWidget()
        for name in contacts:
            text = str(name).strip()
            if not text:
                continue
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self._list.addItem(item)
        root.addWidget(self._list, 1)

        row = QHBoxLayout()
        self._btn_ok = QPushButton("确认")
        self._btn_cancel = QPushButton("取消")
        row.addStretch(1)
        row.addWidget(self._btn_ok)
        row.addWidget(self._btn_cancel)
        root.addLayout(row)

        self._btn_ok.clicked.connect(self.accept)
        self._btn_cancel.clicked.connect(self.reject)

    def selected_contacts(self) -> list[str]:
        out: list[str] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                text = item.text().strip()
                if text:
                    out.append(text)
        return out

