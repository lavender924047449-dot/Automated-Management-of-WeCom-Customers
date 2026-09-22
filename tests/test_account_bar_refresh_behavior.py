"""AccountBar 一机一号模式的关键行为覆盖。

历史（2026-09-22 之前）该文件测的是多账号下拉 / init 引导 / 被动 probe / hydrate 等，
这些能力已按「一机一号 + 无 Frida 账号探测」方案全部下线，测试也随之精简。
"""

from __future__ import annotations

from subprocess import CompletedProcess

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QMessageBox

import gui.widgets.account_bar as account_bar_mod
from app.broadcast.account import get_current_account_id
from gui.widgets.account_bar import AccountBar


def test_get_current_account_id_can_disable_frida_fallback(monkeypatch) -> None:
    """account facade 支持完全禁用 Frida fallback；AccountBar 依赖此语义。"""
    monkeypatch.setattr(
        "app.broadcast.account.read_vid_from_wework_prefs",
        lambda **_kwargs: "",
    )
    called = {"probe": 0}

    def fake_probe(**_kwargs):
        called["probe"] += 1
        return {"account_id": "1688855042791155", "display_name": ""}

    monkeypatch.setattr("app.broadcast.account.probe_account_identity", fake_probe)

    result = get_current_account_id(
        run_fn=lambda *_args, **_kwargs: CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        use_env=False,
        allow_frida_fallback=False,
    )
    assert result == "default"
    assert called["probe"] == 0


def test_startup_probe_success_applies_account_and_enables_tabs(qtbot, monkeypatch) -> None:
    """启动探测成功 → accountChanged 发出 vid + detectionStateChanged=True。"""
    monkeypatch.setattr(account_bar_mod, "switch_account", lambda account_id, **_kwargs: account_id)

    bar = AccountBar()
    qtbot.addWidget(bar)
    assert bar.current_account() == ""

    changed: list[str] = []
    states: list[bool] = []
    bar.accountChanged.connect(changed.append)
    bar.detectionStateChanged.connect(states.append)

    bar._probe_purpose = "startup"  # noqa: SLF001
    bar._on_probe_finished("1688855042791155", "")  # noqa: SLF001

    assert bar.current_account() == "1688855042791155"
    assert changed == ["1688855042791155"]
    assert states == [True]


def test_startup_probe_failure_leaves_tabs_disabled(qtbot, monkeypatch) -> None:
    """启动探测失败：不 emit accountChanged 为有效 vid，广播 detectionStateChanged=False。"""
    bar = AccountBar()
    qtbot.addWidget(bar)

    changed: list[str] = []
    states: list[bool] = []
    bar.accountChanged.connect(changed.append)
    bar.detectionStateChanged.connect(states.append)

    bar._probe_purpose = "startup"  # noqa: SLF001
    bar._on_probe_finished("", "未检测到已登录的企微账号。")  # noqa: SLF001

    assert bar.current_account() == ""
    # startup 失败前当前没账号，不应 emit 空 accountChanged
    assert changed == []
    assert states == [False]


def test_refresh_probe_declined_does_not_switch(qtbot, monkeypatch) -> None:
    """刷新探到不同 vid，用户点 No → 保持原账号。"""
    monkeypatch.setattr(account_bar_mod, "switch_account", lambda account_id, **_kwargs: account_id)
    monkeypatch.setattr(
        account_bar_mod.QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.No,
    )

    bar = AccountBar()
    qtbot.addWidget(bar)
    # 先假装有一个当前账号
    bar._probe_purpose = "startup"  # noqa: SLF001
    bar._on_probe_finished("acct_a", "")  # noqa: SLF001
    assert bar.current_account() == "acct_a"

    changed_before = []
    bar.accountChanged.connect(changed_before.append)

    bar._probe_purpose = "refresh"  # noqa: SLF001
    bar._on_probe_finished("acct_b", "")  # noqa: SLF001
    assert bar.current_account() == "acct_a"
    assert changed_before == []


def test_refresh_probe_accepted_switches(qtbot, monkeypatch) -> None:
    """刷新探到不同 vid，用户点 Yes → 切到新账号，emit accountChanged。"""
    monkeypatch.setattr(account_bar_mod, "switch_account", lambda account_id, **_kwargs: account_id)
    monkeypatch.setattr(
        account_bar_mod.QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )

    bar = AccountBar()
    qtbot.addWidget(bar)
    bar._probe_purpose = "startup"  # noqa: SLF001
    bar._on_probe_finished("acct_a", "")  # noqa: SLF001

    changed: list[str] = []
    bar.accountChanged.connect(changed.append)

    bar._probe_purpose = "refresh"  # noqa: SLF001
    bar._on_probe_finished("acct_b", "")  # noqa: SLF001

    assert bar.current_account() == "acct_b"
    assert changed == ["acct_b"]


def test_refresh_probe_same_vid_is_noop(qtbot, monkeypatch) -> None:
    """刷新探到同一 vid → 不重复 emit accountChanged。"""
    monkeypatch.setattr(account_bar_mod, "switch_account", lambda account_id, **_kwargs: account_id)
    monkeypatch.setattr(
        account_bar_mod.QMessageBox,
        "information",
        lambda *_a, **_k: None,
    )

    bar = AccountBar()
    qtbot.addWidget(bar)
    bar._probe_purpose = "startup"  # noqa: SLF001
    bar._on_probe_finished("acct_a", "")  # noqa: SLF001

    changed: list[str] = []
    bar.accountChanged.connect(changed.append)

    bar._probe_purpose = "refresh"  # noqa: SLF001
    bar._on_probe_finished("acct_a", "")  # noqa: SLF001
    assert bar.current_account() == "acct_a"
    assert changed == []
