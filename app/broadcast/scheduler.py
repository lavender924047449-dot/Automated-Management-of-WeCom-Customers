"""Step 15: 广播任务调度器（APScheduler + 本地持久化）。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from app.broadcast.paths import account_reports_dir, data_root_for
from app.broadcast.schedule_spec import AfterSpec, AtSpec, CronSpec, ImmediateSpec, ScheduleSpec
from app.security import get_secure_store

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:  # pragma: no cover - 依赖缺失时降级为运行期报错
    BackgroundScheduler = None  # type: ignore[assignment]


TriggerKind = Literal["immediate", "after", "at", "cron"]
ScheduleStatus = Literal["active", "completed", "missed", "cancelled", "failed"]
ExecuteCallback = Callable[["ScheduleEntry"], None]

MISSED_GRACE_TIME_SECONDS = 0
_SECURE_STORE = get_secure_store()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_weekday_token(value: str) -> str:
    token = value.strip().lower()
    mapping = {
        "1": "mon",
        "2": "tue",
        "3": "wed",
        "4": "thu",
        "5": "fri",
        "6": "sat",
        "7": "sun",
    }
    if token in mapping:
        return mapping[token]
    if token in mapping.values():
        return token
    raise ValueError(f"weekly 触发器 weekday 非法: {value}")


def parse_cron_expr(expr: str) -> dict[str, Any]:
    text = str(expr or "").strip().lower()
    if text.startswith("daily@"):
        hm = text.removeprefix("daily@")
        return _parse_hh_mm(hm)
    if text.startswith("weekly@"):
        body = text.removeprefix("weekly@")
        if "/" not in body:
            raise ValueError("weekly 触发器格式应为 weekly@DOW/HH:MM")
        dow, hm = body.split("/", 1)
        payload = _parse_hh_mm(hm)
        payload["day_of_week"] = _parse_weekday_token(dow)
        return payload
    raise ValueError(f"不支持的 cron 表达式: {expr}")


_WEEKDAY_LABEL = {
    "mon": "一",
    "tue": "二",
    "wed": "三",
    "thu": "四",
    "fri": "五",
    "sat": "六",
    "sun": "日",
}


def cron_expr_label(expr: str) -> str:
    """把 daily@/weekly@ 表达式写成「每天 09:00」「每周三 09:00」。"""
    parsed = parse_cron_expr(expr)
    hhmm = f"{int(parsed['hour']):02d}:{int(parsed['minute']):02d}"
    weekday = str(parsed.get("day_of_week", "")).strip()
    if not weekday:
        return f"每天 {hhmm}"
    short = _WEEKDAY_LABEL.get(weekday, weekday)
    return f"每周{short} {hhmm}"


def next_cron_fire(expr: str, now: datetime) -> datetime:
    """下一次循环触发时刻。若当前时刻刚好落在触发点上，返回再下一轮。"""
    parsed = parse_cron_expr(expr)
    hour = int(parsed["hour"])
    minute = int(parsed["minute"])
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    weekday = str(parsed.get("day_of_week", "")).strip()
    if not weekday:
        if base <= now:
            base += timedelta(days=1)
        return base
    target = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}[weekday]
    days = (target - base.weekday()) % 7
    base += timedelta(days=days)
    if base <= now:
        base += timedelta(days=7)
    return base


def _parse_hh_mm(value: str) -> dict[str, int]:
    body = value.strip()
    if ":" not in body:
        raise ValueError(f"时间格式非法（需 HH:MM）: {value}")
    h, m = body.split(":", 1)
    if not h.isdigit() or not m.isdigit():
        raise ValueError(f"时间格式非法（需 HH:MM）: {value}")
    hh = int(h)
    mm = int(m)
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError(f"时间越界（需 00:00~23:59）: {value}")
    return {"hour": hh, "minute": mm}


@dataclass
class ScheduleEntry:
    id: str
    trigger: TriggerKind
    status: ScheduleStatus
    created_at: str
    payload: dict[str, Any]
    run_at: str = ""
    delay_seconds: float = 0.0
    cron_expr: str = ""
    last_run_at: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trigger": self.trigger,
            "status": self.status,
            "created_at": self.created_at,
            "payload": self.payload,
            "run_at": self.run_at,
            "delay_seconds": self.delay_seconds,
            "cron_expr": self.cron_expr,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ScheduleEntry":
        return cls(
            id=str(row.get("id", "")).strip(),
            trigger=str(row.get("trigger", "immediate")).strip(),  # type: ignore[arg-type]
            status=str(row.get("status", "active")).strip(),  # type: ignore[arg-type]
            created_at=str(row.get("created_at", "")).strip() or _now_iso(),
            payload=dict(row.get("payload", {}) or {}),
            run_at=str(row.get("run_at", "")).strip(),
            delay_seconds=float(row.get("delay_seconds", 0.0) or 0.0),
            cron_expr=str(row.get("cron_expr", "")).strip(),
            last_run_at=str(row.get("last_run_at", "")).strip(),
            last_error=str(row.get("last_error", "")).strip(),
        )

    def is_one_shot(self) -> bool:
        return self.trigger in {"immediate", "after", "at"}


class _SchedulerBackend(Protocol):
    def add_job(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def remove_job(self, job_id: str) -> None:
        ...

    def start(self) -> None:
        ...

    def shutdown(self, wait: bool = True) -> None:
        ...


class BroadcastScheduler:
    """计划持久化 + APScheduler 调度封装。"""

    def __init__(
        self,
        *,
        account_id: str | None = None,
        execute_cb: ExecuteCallback | None = None,
        schedules_path: str | Path | None = None,
        reports_dir: str | Path | None = None,
        scheduler_backend: _SchedulerBackend | None = None,
        auto_start: bool = True,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.execute_cb = execute_cb or (lambda _entry: None)
        self.now_fn = now_fn or datetime.now
        root = data_root_for(account_id)
        self.schedules_path = Path(schedules_path) if schedules_path else (root / "schedules.json")
        self.reports_dir = Path(reports_dir) if reports_dir else account_reports_dir(account_id)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, ScheduleEntry] = {}
        self._scheduler = scheduler_backend or self._build_scheduler_backend()
        self._load_entries()
        self._restore_active_jobs()
        if auto_start:
            self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        self._scheduler.shutdown(wait=wait)

    def list_entries(self) -> list[ScheduleEntry]:
        items = list(self._entries.values())
        return sorted(items, key=lambda x: x.created_at, reverse=True)

    def schedule_immediate(self, *, payload: dict[str, Any]) -> ScheduleEntry:
        return self.submit(ImmediateSpec(), payload=payload)

    def schedule_after(self, delay_seconds: float, *, payload: dict[str, Any]) -> ScheduleEntry:
        return self.submit(AfterSpec(delay_seconds=delay_seconds), payload=payload)

    def schedule_at(self, when: datetime, *, payload: dict[str, Any]) -> ScheduleEntry:
        return self.submit(
            AtSpec(run_at=when.isoformat(timespec="seconds")),
            payload=payload,
        )

    def schedule_cron(self, cron_expr: str, *, payload: dict[str, Any]) -> ScheduleEntry:
        return self.submit(CronSpec(expr=cron_expr), payload=payload)

    def submit(self, spec: ScheduleSpec, *, payload: dict[str, Any]) -> ScheduleEntry:
        if isinstance(spec, ImmediateSpec):
            return self._create_one_shot("immediate", payload=payload, run_at=self.now_fn())
        if isinstance(spec, AfterSpec):
            sec = float(spec.delay_seconds)
            return self._create_one_shot(
                "after",
                payload=payload,
                run_at=self.now_fn() + timedelta(seconds=sec),
                delay_seconds=sec,
            )
        if isinstance(spec, AtSpec):
            run_at = _parse_iso(spec.run_at)
            if run_at is None:
                raise ValueError(f"run_at 非法: {spec.run_at}")
            return self._create_one_shot("at", payload=payload, run_at=run_at)
        if isinstance(spec, CronSpec):
            # 先做 parse 校验，避免持久化脏数据。
            parse_cron_expr(spec.expr)
            entry = ScheduleEntry(
                id=str(uuid.uuid4()),
                trigger="cron",
                status="active",
                created_at=_now_iso(),
                payload=dict(payload),
                cron_expr=spec.expr,
            )
            self._entries[entry.id] = entry
            self._save_entries()
            self._register_job(entry)
            return entry
        raise TypeError(f"不支持的 ScheduleSpec: {type(spec).__name__}")

    def cancel(self, schedule_id: str) -> bool:
        key = str(schedule_id).strip()
        if not key or key not in self._entries:
            return False
        entry = self._entries[key]
        entry.status = "cancelled"
        self._entries[key] = entry
        self._save_entries()
        try:
            self._scheduler.remove_job(key)
        except Exception:
            pass
        return True

    def _create_one_shot(
        self,
        trigger: Literal["immediate", "after", "at"],
        *,
        payload: dict[str, Any],
        run_at: datetime,
        delay_seconds: float = 0.0,
    ) -> ScheduleEntry:
        entry = ScheduleEntry(
            id=str(uuid.uuid4()),
            trigger=trigger,
            status="active",
            created_at=_now_iso(),
            payload=dict(payload),
            run_at=run_at.isoformat(timespec="seconds"),
            delay_seconds=float(delay_seconds),
        )
        self._entries[entry.id] = entry
        self._save_entries()
        self._register_job(entry)
        return entry

    def _build_scheduler_backend(self) -> _SchedulerBackend:
        if BackgroundScheduler is None:
            raise RuntimeError("缺少 APScheduler 依赖，请安装 `apscheduler`")
        # APScheduler 要求 misfire_grace_time >= 1；Step 15 的 0 语义由 restore 逻辑实现：
        # 开机时只要发现一次性任务的 run_at 已过，立刻写 missed 报告并不补发。
        return BackgroundScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 1,
            }
        )

    def _load_entries(self) -> None:
        self._entries = {}
        if not self.schedules_path.exists():
            return
        raw = _SECURE_STORE.load_json(self.schedules_path, default=[])
        if not isinstance(raw, list):
            return
        for row in raw:
            if not isinstance(row, dict):
                continue
            entry = ScheduleEntry.from_dict(row)
            if not entry.id:
                continue
            self._entries[entry.id] = entry

    def _save_entries(self) -> Path:
        self.schedules_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [x.to_dict() for x in self.list_entries()]
        _SECURE_STORE.save_json(self.schedules_path, rows, encrypt=True)
        return self.schedules_path

    def _restore_active_jobs(self) -> None:
        now = self.now_fn()
        changed = False
        for entry in self.list_entries():
            if entry.status != "active":
                continue
            if entry.is_one_shot():
                due = _parse_iso(entry.run_at)
                if due is None:
                    entry.status = "cancelled"
                    entry.last_error = "invalid run_at"
                    self._entries[entry.id] = entry
                    changed = True
                    continue
                if due <= now:
                    entry.status = "missed"
                    entry.last_error = "missed while app offline"
                    self._entries[entry.id] = entry
                    self._write_missed_report(entry, reason="startup_missed")
                    changed = True
                    continue
            self._register_job(entry)
        if changed:
            self._save_entries()

    def _register_job(self, entry: ScheduleEntry) -> None:
        if entry.status != "active":
            return
        if entry.is_one_shot():
            run_at = _parse_iso(entry.run_at)
            if run_at is None:
                raise ValueError(f"schedule run_at 非法: {entry.run_at}")
            self._scheduler.add_job(
                self._fire_job,
                trigger="date",
                run_date=run_at,
                args=[entry.id],
                id=entry.id,
                replace_existing=True,
            )
            return
        if entry.trigger == "cron":
            spec = parse_cron_expr(entry.cron_expr)
            self._scheduler.add_job(
                self._fire_job,
                trigger="cron",
                args=[entry.id],
                id=entry.id,
                replace_existing=True,
                **spec,
            )
            return
        raise ValueError(f"不支持的 trigger: {entry.trigger}")

    def _fire_job(self, schedule_id: str) -> None:
        entry = self._entries.get(schedule_id)
        if entry is None or entry.status != "active":
            return
        now_text = _now_iso()
        try:
            self.execute_cb(entry)
            entry.last_run_at = now_text
            if entry.is_one_shot():
                entry.status = "completed"
        except Exception as exc:  # pragma: no cover - 回调异常在上层集成中验证
            entry.last_run_at = now_text
            entry.last_error = str(exc)
            if entry.is_one_shot():
                entry.status = "failed"
        self._entries[entry.id] = entry
        self._save_entries()

    def _write_missed_report(self, entry: ScheduleEntry, *, reason: str) -> Path:
        ts = self.now_fn().strftime("%Y%m%d_%H%M%S")
        target = self.reports_dir / f"missed_{ts}.json"
        payload = {
            "ts": _now_iso(),
            "reason": reason,
            "missed_grace_time": MISSED_GRACE_TIME_SECONDS,
            "schedule_id": entry.id,
            "trigger": entry.trigger,
            "status": entry.status,
            "run_at": entry.run_at,
            "delay_seconds": entry.delay_seconds,
            "cron_expr": entry.cron_expr,
            "payload": entry.payload,
            "last_error": entry.last_error,
        }
        _SECURE_STORE.save_json(target, payload, encrypt=True)
        return target


__all__ = [
    "MISSED_GRACE_TIME_SECONDS",
    "BroadcastScheduler",
    "ScheduleEntry",
    "parse_cron_expr",
    "cron_expr_label",
    "next_cron_fire",
]
