from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QMessageBox

from app.updater.version_check import VersionCheckResult
from gui.main_window import MainWindow


@pytest.fixture
def window(qtbot, monkeypatch):
    monkeypatch.setattr("gui.main_window.MainWindow._post_startup_tasks", lambda self: None)
    monkeypatch.setattr("gui.main_window.AppTrayIcon.is_available", staticmethod(lambda: False))
    monkeypatch.setattr(
        "gui.tabs.contacts_tab.ContactsTab.refresh_contacts",
        lambda self, *, force_refresh, show_error=True: None,
    )
    w = MainWindow()
    qtbot.addWidget(w)
    return w


def test_help_menu_contains_step8_actions(window: MainWindow):
    menus = [a.menu() for a in window.menuBar().actions() if a.menu() is not None]
    help_menu = next((m for m in menus if m.title() == "帮助"), None)
    assert help_menu is not None
    labels = [a.text() for a in help_menu.actions() if a.text()]
    assert "使用手册" in labels
    assert "检查更新" in labels
    assert "关于" in labels


def test_business_tabs_disabled_until_account_detected(window: MainWindow):
    """一机一号：探测结果出来前所有业务 tab 应禁用。"""
    for idx in range(window._tabs.count()):
        assert window._tabs.isTabEnabled(idx) is False


def test_detection_success_enables_all_tabs(window: MainWindow):
    """AccountBar.detectionStateChanged(True) → 业务 tab 全部激活。"""
    window._set_business_tabs_enabled(True)
    for idx in range(window._tabs.count()):
        assert window._tabs.isTabEnabled(idx) is True


def test_open_user_manual_opens_local_markdown(window: MainWindow, monkeypatch, tmp_path: Path):
    manual = tmp_path / "USER_MANUAL.md"
    manual.write_text("# manual", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr("gui.main_window.resolve_path", lambda _p: manual)
    monkeypatch.setattr("gui.main_window.QDesktopServices.openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    window._open_user_manual()
    assert opened
    assert Path(opened[0]).resolve() == manual.resolve()


def test_check_update_latest_shows_info_when_manual(window: MainWindow, monkeypatch):
    monkeypatch.setattr(
        "gui.main_window.check_latest_version",
        lambda: VersionCheckResult(
            current_version="0.1.0",
            latest_version="0.1.0",
            has_update=False,
            releases_url="https://github.com/lavender924047449-dot/Automated-Management-of-WeCom-Customers/releases",
            error="",
        ),
    )
    called = {"n": 0}
    monkeypatch.setattr(
        "gui.main_window.QMessageBox.information",
        lambda *_a, **_k: called.__setitem__("n", called["n"] + 1) or 0,
    )
    window._check_update_prompt(silent=False)
    assert called["n"] == 1


def _fake_message_box(monkeypatch, buttons: list[str]):
    class _FakeMessageBox:
        Icon = QMessageBox.Icon
        ButtonRole = QMessageBox.ButtonRole
        StandardButton = QMessageBox.StandardButton

        def __init__(self, *_args, **_kwargs) -> None:
            self._clicked = None

        def setIcon(self, _icon) -> None:
            return None

        def setWindowTitle(self, _title: str) -> None:
            return None

        def setText(self, _text: str) -> None:
            return None

        def addButton(self, text, *_args, **_kwargs):
            buttons.append(str(text))
            return text

        def exec(self) -> int:
            return 0

        def clickedButton(self):
            return None

    monkeypatch.setattr("gui.main_window.QMessageBox", _FakeMessageBox)


def test_check_update_offers_apply_for_portable_install(window: MainWindow, monkeypatch, tmp_path: Path):
    home = tmp_path / "install"
    (home / "程序").mkdir(parents=True)
    (home / "python").mkdir()
    (home / "python" / "pythonw.exe").write_bytes(b"py")
    monkeypatch.setenv("TESTVOICE_HOME", str(home))
    monkeypatch.setattr(
        "gui.main_window.check_latest_version",
        lambda: VersionCheckResult(
            current_version="0.1.0",
            latest_version="0.2.0",
            has_update=True,
            releases_url="https://github.com/org/Test-Voice/releases/tag/v0.2.0",
            asset_name="program-update.zip",
            asset_url="https://github.com/org/Test-Voice/releases/download/v0.2.0/program-update.zip",
        ),
    )
    buttons: list[str] = []
    _fake_message_box(monkeypatch, buttons)
    window._check_update_prompt(silent=False)
    assert "更新并重启" in buttons
    assert "打开发布页" not in buttons


def test_check_update_dev_install_opens_release_page(window: MainWindow, monkeypatch):
    monkeypatch.delenv("TESTVOICE_HOME", raising=False)
    monkeypatch.setattr(
        "gui.main_window.check_latest_version",
        lambda: VersionCheckResult(
            current_version="0.1.0",
            latest_version="0.2.0",
            has_update=True,
            releases_url="https://github.com/org/Test-Voice/releases/tag/v0.2.0",
            asset_url="https://github.com/org/Test-Voice/releases/download/v0.2.0/program-update.zip",
        ),
    )
    buttons: list[str] = []
    _fake_message_box(monkeypatch, buttons)
    window._check_update_prompt(silent=False)
    assert "打开发布页" in buttons
    assert "更新并重启" not in buttons


def test_about_dialog_contains_versions(window: MainWindow, monkeypatch):
    captured = {"text": ""}

    class _FakeMessageBox:
        Icon = QMessageBox.Icon
        ButtonRole = QMessageBox.ButtonRole
        StandardButton = QMessageBox.StandardButton

        def __init__(self, *_args, **_kwargs) -> None:
            self._clicked = None

        def setIcon(self, _icon) -> None:
            return None

        def setWindowTitle(self, _title: str) -> None:
            return None

        def setText(self, text: str) -> None:
            captured["text"] = text

        def addButton(self, *_args, **_kwargs):
            return object()

        def exec(self) -> int:
            return 0

        def clickedButton(self):
            return self._clicked

    monkeypatch.setattr("gui.main_window.QMessageBox", _FakeMessageBox)
    monkeypatch.setattr("gui.main_window.get_local_app_version", lambda: "0.1.0")
    window._show_about_dialog()
    assert "版本号：0.1.0" in captured["text"]
    assert "企微适配版本：" in captured["text"]
    assert "项目地址：" in captured["text"]
