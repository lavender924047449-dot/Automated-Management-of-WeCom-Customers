"""素材列表按录入时间从新到旧排列。"""

from __future__ import annotations

from typing import Any


def entered_at_of(row: dict[str, Any]) -> str:
    extra = row.get("extra")
    if isinstance(extra, dict):
        stamp = str(extra.get("entered_at") or "").strip()
        if stamp:
            return stamp
    return str(row.get("updated_at") or "").strip()


def newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=entered_at_of, reverse=True)
