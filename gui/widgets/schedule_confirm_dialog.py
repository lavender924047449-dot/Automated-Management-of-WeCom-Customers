from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class ScheduleConfirmDialog(QDialog):
    """定时任务到点确认框：60s 倒计时。"""

    def __init__(self, parent=None, *, title: str, message: str, seconds: int = 60) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._left = max(1, int(seconds))
        self._build_ui(message)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._refresh_countdown_text()

    def _build_ui(self, message: str) -> None:
        root = QVBoxLayout(self)
        self._message = QLabel(message)
        self._countdown = QLabel("")
        self._countdown.setStyleSheet("color: #dc2626; font-weight: 600;")

        row = QHBoxLayout()
        self._btn_ok = QPushButton("立即开始")
        self._btn_cancel = QPushButton("取消")
        row.addStretch(1)
        row.addWidget(self._btn_ok)
        row.addWidget(self._btn_cancel)

        root.addWidget(self._message)
        root.addWidget(self._countdown)
        root.addLayout(row)

        self._btn_ok.clicked.connect(self.accept)
        self._btn_cancel.clicked.connect(self.reject)

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self.reject()
            return
        self._refresh_countdown_text()

    def _refresh_countdown_text(self) -> None:
        self._countdown.setText(f"{self._left}s 后自动取消")

