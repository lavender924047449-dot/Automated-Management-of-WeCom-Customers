"""待执行计划持久化存储。

每条记录包含：plan JSON、调度模式、触发时间、创建时间、唯一 ID。
存储文件按账号隔离，使用加密 JSON 存储。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.broadcast.paths import data_root_for
from app.security import get_secure_store

_SECURE_STORE = get_secure_store()


@dataclass
class PendingPlan:
    id: str
    plan: dict[str, Any]
    mode: str  # "after" | "at" | "cron"
    trigger_at: str  # ISO datetime string
    created_at: str
    label: str = ""  # 用户可读描述
    schedule_id: str = ""  # 循环任务对应的调度器 id，一次性计划为空

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan": self.plan,
            "mode": self.mode,
            "trigger_at": self.trigger_at,
            "created_at": self.created_at,
            "label": self.label,
            "schedule_id": self.schedule_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingPlan":
        return cls(
            id=str(data.get("id", "")).strip(),
            plan=dict(data.get("plan", {})),
            mode=str(data.get("mode", "")).strip(),
            trigger_at=str(data.get("trigger_at", "")).strip(),
            created_at=str(data.get("created_at", "")).strip(),
            label=str(data.get("label", "")).strip(),
            schedule_id=str(data.get("schedule_id", "")).strip(),
        )


class PendingPlanStore:
    """按账号持久化待执行计划列表。"""

    def __init__(self, *, account_id: str) -> None:
        self.account_id = account_id.strip() or "default"
        self.path = data_root_for(self.account_id) / "pending_plans.json"
        self._entries: dict[str, PendingPlan] = {}
        self._load()

    def _load(self) -> None:
        self._entries = {}
        if not self.path.exists():
            return
        raw = _SECURE_STORE.load_json(self.path, default=[])
        if not isinstance(raw, list):
            return
        for row in raw:
            if not isinstance(row, dict):
                continue
            entry = PendingPlan.from_dict(row)
            if entry.id:
                self._entries[entry.id] = entry

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [e.to_dict() for e in self.list()]
        _SECURE_STORE.save_json(self.path, rows, encrypt=True)

    def list(self) -> list[PendingPlan]:
        items = list(self._entries.values())
        return sorted(items, key=lambda x: x.trigger_at)

    def add(
        self,
        plan: dict[str, Any],
        *,
        mode: str,
        trigger_at: str,
        label: str = "",
        schedule_id: str = "",
    ) -> PendingPlan:
        entry = PendingPlan(
            id=str(uuid.uuid4()),
            plan=dict(plan),
            mode=mode,
            trigger_at=trigger_at,
            created_at=datetime.now().isoformat(timespec="seconds"),
            label=label,
            schedule_id=str(schedule_id or "").strip(),
        )
        self._entries[entry.id] = entry
        self._save()
        return entry

    def remove(self, entry_id: str) -> bool:
        key = entry_id.strip()
        if key not in self._entries:
            return False
        del self._entries[key]
        self._save()
        return True

    def get(self, entry_id: str) -> PendingPlan | None:
        return self._entries.get(entry_id.strip())

    def update_plan(self, entry_id: str, plan: dict[str, Any]) -> bool:
        key = entry_id.strip()
        entry = self._entries.get(key)
        if entry is None:
            return False
        entry.plan = dict(plan)
        self._save()
        return True

    def update_trigger(self, entry_id: str, trigger_at: str, *, label: str | None = None) -> bool:
        key = entry_id.strip()
        entry = self._entries.get(key)
        if entry is None:
            return False
        entry.trigger_at = trigger_at.strip()
        if label is not None:
            entry.label = label.strip()
        self._save()
        return True

    def find_by_schedule_id(self, schedule_id: str) -> PendingPlan | None:
        key = str(schedule_id or "").strip()
        if not key:
            return None
        for entry in self._entries.values():
            if entry.schedule_id == key:
                return entry
        return None


def delay_label(value: int, unit: str) -> str:
    """把用户选的延迟写成「5秒后」「5分钟后」「5小时后」。"""
    names = {"秒": "秒", "分钟": "分钟", "小时": "小时"}
    text = names.get(str(unit or "").strip(), "秒")
    return f"{int(value)}{text}后"


def format_trigger_clock(trigger_at: str) -> str:
    text = str(trigger_at or "").strip().replace("T", " ")
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:19]


def pending_list_text(entry: PendingPlan) -> str:
    """待执行列表一行文案。循环只显示「每天/每周」；相对延迟旁边保留具体时刻。"""
    label = entry.label.strip()
    if entry.mode == "cron":
        return label or "循环"
    when = format_trigger_clock(entry.trigger_at)
    if not label or label == when:
        return when or label or entry.mode
    if when:
        return f"{label} · {when}"
    return label


__all__ = [
    "PendingPlan",
    "PendingPlanStore",
    "delay_label",
    "format_trigger_clock",
    "pending_list_text",
]
