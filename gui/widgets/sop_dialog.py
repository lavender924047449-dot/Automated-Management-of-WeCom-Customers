from __future__ import annotations

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout


class SopDialog(QDialog):
    """SOP 保存对话框：命名 + 标签绑定。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("另存为 SOP")
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self._name = QLineEdit()
        self._name.setPlaceholderText("请输入 SOP 名称")
        self._labels = QLineEdit()
        self._labels.setPlaceholderText("可选：绑定标签，多个用逗号分隔")
        hint = QLabel("示例：新客,高意向,复购")
        hint.setStyleSheet("color: #64748b;")

        row = QHBoxLayout()
        self._btn_ok = QPushButton("保存")
        self._btn_cancel = QPushButton("取消")
        row.addStretch(1)
        row.addWidget(self._btn_ok)
        row.addWidget(self._btn_cancel)

        root.addWidget(QLabel("名称"))
        root.addWidget(self._name)
        root.addWidget(QLabel("绑定标签"))
        root.addWidget(self._labels)
        root.addWidget(hint)
        root.addLayout(row)

        self._btn_ok.clicked.connect(self.accept)
        self._btn_cancel.clicked.connect(self.reject)

    def result_data(self) -> tuple[str, list[str]]:
        name = self._name.text().strip()
        labels = [x.strip() for x in self._labels.text().replace("、", ",").split(",") if x.strip()]
        return name, labels

