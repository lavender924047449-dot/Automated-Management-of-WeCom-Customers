from __future__ import annotations

from app.broadcast.context import AppContext
from app.broadcast.scheduler import ScheduleEntry


def test_app_context_rebind_switches_paths():
    ctx = AppContext("acct_a")
    first_reports = ctx.report_store.reports_dir
    first_voice_index = ctx.voice_index_path()

    changed = ctx.rebind("acct_b")
    assert changed == "acct_b"

    second_reports = ctx.report_store.reports_dir
    second_voice_index = ctx.voice_index_path()
    assert first_reports != second_reports
    assert first_voice_index != second_voice_index
    assert "acct_b" in str(second_reports)


def test_app_context_schedule_listener_dispatch():
    captured: list[str] = []
    ctx = AppContext("acct_x")

    def on_entry(entry) -> None:
        captured.append(entry.id)

    ctx.add_schedule_listener(on_entry)
    entry = ScheduleEntry(
        id="sched_1",
        trigger="immediate",
        status="active",
        created_at="2026-09-17T00:00:00",
        payload={},
    )
    ctx._dispatch_schedule_entry(entry)  # noqa: SLF001 - 单测直接触发回调
    assert captured == [entry.id]
    ctx.shutdown()
