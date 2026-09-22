from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.broadcast.schedule_spec import AfterSpec, AtSpec, CronSpec, ImmediateSpec
from app.broadcast.scheduler import BroadcastScheduler, parse_cron_expr


class _FakeBackend:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.started = False
        self.stopped = False

    def add_job(self, func, *args, **kwargs):
        job_id = kwargs["id"]
        trigger = kwargs.get("trigger", "")
        data = {"func": func, "trigger": trigger, "kwargs": dict(kwargs)}
        self.jobs[job_id] = data
        return data

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:
        self.stopped = True

    def fire(self, job_id: str) -> None:
        row = self.jobs[job_id]
        row["func"](job_id)


def _fixed_now() -> datetime:
    return datetime(2026, 9, 16, 19, 20, 0)


def test_parse_cron_expr_daily_and_weekly():
    assert parse_cron_expr("daily@09:30") == {"hour": 9, "minute": 30}
    assert parse_cron_expr("weekly@wed/18:05") == {
        "hour": 18,
        "minute": 5,
        "day_of_week": "wed",
    }


def test_schedule_triggers_and_timing(tmp_path: Path):
    backend = _FakeBackend()
    scheduler = BroadcastScheduler(
        schedules_path=tmp_path / "schedules.json",
        reports_dir=tmp_path / "reports",
        scheduler_backend=backend,
        auto_start=False,
        now_fn=_fixed_now,
    )
    i = scheduler.schedule_immediate(payload={"plan_id": "p1"})
    a = scheduler.schedule_after(120, payload={"plan_id": "p2"})
    c = scheduler.schedule_cron("daily@19:30", payload={"plan_id": "p3"})

    i_job = backend.jobs[i.id]
    assert i_job["trigger"] == "date"
    assert i_job["kwargs"]["run_date"] == _fixed_now()

    a_job = backend.jobs[a.id]
    assert a_job["trigger"] == "date"
    assert a_job["kwargs"]["run_date"] == _fixed_now() + timedelta(seconds=120)

    c_job = backend.jobs[c.id]
    assert c_job["trigger"] == "cron"
    assert c_job["kwargs"]["hour"] == 19
    assert c_job["kwargs"]["minute"] == 30


def test_schedule_spec_submit_and_roundtrip(tmp_path: Path):
    backend = _FakeBackend()
    scheduler = BroadcastScheduler(
        schedules_path=tmp_path / "schedules.json",
        reports_dir=tmp_path / "reports",
        scheduler_backend=backend,
        auto_start=False,
        now_fn=_fixed_now,
    )

    spec_cases = [
        ImmediateSpec(),
        AfterSpec(delay_seconds=30),
        AtSpec(run_at=_fixed_now().isoformat(timespec="seconds")),
        CronSpec(expr="daily@09:30"),
    ]
    for spec in spec_cases:
        dumped = spec.model_dump(mode="json")
        if dumped["mode"] == "immediate":
            rebuilt = ImmediateSpec.model_validate(dumped)
        elif dumped["mode"] == "after":
            rebuilt = AfterSpec.model_validate(dumped)
        elif dumped["mode"] == "at":
            rebuilt = AtSpec.model_validate(dumped)
        else:
            rebuilt = CronSpec.model_validate(dumped)
        scheduler.submit(rebuilt, payload={"source": dumped["mode"]})

    triggers = {row["trigger"] for row in backend.jobs.values()}
    assert triggers == {"date", "cron"}


def test_schedule_persistence_and_restore(tmp_path: Path):
    first_backend = _FakeBackend()
    scheduler = BroadcastScheduler(
        schedules_path=tmp_path / "schedules.json",
        reports_dir=tmp_path / "reports",
        scheduler_backend=first_backend,
        auto_start=False,
        now_fn=_fixed_now,
    )
    row = scheduler.schedule_after(300, payload={"plan_id": "persist"})
    assert row.id in first_backend.jobs

    second_backend = _FakeBackend()
    BroadcastScheduler(
        schedules_path=tmp_path / "schedules.json",
        reports_dir=tmp_path / "reports",
        scheduler_backend=second_backend,
        auto_start=False,
        now_fn=_fixed_now,
    )
    assert row.id in second_backend.jobs


def test_missed_schedule_skipped_and_reported(tmp_path: Path):
    schedules_path = tmp_path / "schedules.json"
    reports_dir = tmp_path / "reports"
    past_due = (_fixed_now() - timedelta(minutes=2)).isoformat(timespec="seconds")
    payload = [
        {
            "id": "job-old",
            "trigger": "at",
            "status": "active",
            "created_at": "2026-09-16T19:00:00",
            "payload": {"plan_id": "old"},
            "run_at": past_due,
            "delay_seconds": 0.0,
            "cron_expr": "",
            "last_run_at": "",
            "last_error": "",
        }
    ]
    schedules_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    backend = _FakeBackend()
    scheduler = BroadcastScheduler(
        schedules_path=schedules_path,
        reports_dir=reports_dir,
        scheduler_backend=backend,
        auto_start=False,
        now_fn=_fixed_now,
    )
    rows = scheduler.list_entries()
    assert rows[0].status == "missed"
    assert backend.jobs == {}
    missed_files = list(reports_dir.glob("missed_*.json"))
    assert len(missed_files) == 1
