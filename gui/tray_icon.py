from __future__ import annotations

from typing import Callable

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMainWindow, QMenu, QStyle, QSystemTrayIcon

from gui.i18n import APP_TITLE


class AppTrayIcon:
    """系统托盘封装：显示主窗口 / 完全退出。"""

    def __init__(self, window: QMainWindow, on_quit: Callable[[], None]) -> None:
        self._window = window
        self._on_quit = on_quit
        icon = window.windowIcon()
        if icon.isNull():
            icon = window.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self._tray = QSystemTrayIcon(QIcon(icon), window)
        self._menu = QMenu(window)
        self._act_show = QAction("显示主窗口", window)
        self._act_quit = QAction("完全退出", window)
        self._menu.addAction(self._act_show)
        self._menu.addSeparator()
        self._menu.addAction(self._act_quit)
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._act_show.triggered.connect(self.show_main_window)
        self._act_quit.triggered.connect(self._on_quit)

    @staticmethod
    def is_available() -> bool:
        return bool(QSystemTrayIcon.isSystemTrayAvailable())

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def show_main_window(self) -> None:
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    def notify_minimized(self) -> None:
        self._tray.showMessage(
            APP_TITLE,
            "已最小化到托盘，定时任务将在后台执行",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_main_window()
