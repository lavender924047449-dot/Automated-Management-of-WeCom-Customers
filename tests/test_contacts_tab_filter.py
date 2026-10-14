from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from app.broadcast.dto import ContactRowDTO
from gui.tabs.contacts_tab import ContactsTab


def _row(conv_local: str, name: str, labels: list[str]) -> ContactRowDTO:
    return ContactRowDTO(
        conv_local=conv_local,
        display_name=name,
        peer_name=name,
        category="外部客户",
        labels=labels,
    )


def _visible_ids(tab: ContactsTab) -> list[str]:
    return [row.conv_local for row in tab._visible_rows("全部")]  # noqa: SLF001


def test_search_and_selected_labels_use_intersection(qtbot) -> None:
    tab = ContactsTab()
    qtbot.addWidget(tab)
    tab._rows = [  # noqa: SLF001
        _row("1", "张三", ["新客", "VIP"]),
        _row("2", "李四", ["新客"]),
        _row("3", "张伟", ["VIP"]),
        _row("4", "王五", ["老客"]),
    ]

    tab._search.setText("张")  # noqa: SLF001
    tab._active_filter_labels = {"新客", "VIP"}  # noqa: SLF001
    assert _visible_ids(tab) == ["1"]

    tab._search.clear()  # noqa: SLF001
    tab._active_filter_labels = {"新客", "VIP"}  # noqa: SLF001
    assert _visible_ids(tab) == ["1"]

    tab._search.setText("张")  # noqa: SLF001
    tab._active_filter_labels = set()  # noqa: SLF001
    assert _visible_ids(tab) == ["1", "3"]

    tab._search.setText("张")  # noqa: SLF001
    tab._active_filter_labels = {"新客"}  # noqa: SLF001
    assert _visible_ids(tab) == ["1"]
