from __future__ import annotations

from pathlib import Path

from app.broadcast.pending_plans import PendingPlan, PendingPlanStore, delay_label, pending_list_text


def _use_tmp_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.broadcast.paths.resolve_path", lambda p: tmp_path / Path(p))


def test_delay_and_list_text_keep_the_unit_the_user_chose() -> None:
    assert delay_label(5, "秒") == "5秒后"
    assert delay_label(5, "分钟") == "5分钟后"
    assert delay_label(5, "小时") == "5小时后"

    after = PendingPlan(
        id="a",
        plan={},
        mode="after",
        trigger_at="2026-09-30T21:46:04",
        created_at="2026-09-30T16:46:04",
        label="5小时后",
    )
    assert pending_list_text(after) == "5小时后 · 2026-09-30 21:46:04"

    cron = PendingPlan(
        id="c",
        plan={},
        mode="cron",
        trigger_at="2026-10-01T09:00:00",
        created_at="2026-09-30T16:46:04",
        label="每天 09:00",
        schedule_id="sched-1",
    )
    assert pending_list_text(cron) == "每天 09:00"

    edited = PendingPlan(
        id="t",
        plan={},
        mode="at",
        trigger_at="2026-10-02T08:30:00",
        created_at="2026-09-30T16:46:04",
        label="2026-10-02 08:30:00",
    )
    assert pending_list_text(edited) == "2026-10-02 08:30:00"


def test_store_keeps_schedule_id_and_can_relabel(monkeypatch, tmp_path: Path) -> None:
    _use_tmp_data(monkeypatch, tmp_path)
    store = PendingPlanStore(account_id="acct_pending")
    entry = store.add(
        {"sequence": []},
        mode="cron",
        trigger_at="2026-10-01T09:00:00",
        label="每天 09:00",
        schedule_id="sched-9",
    )
    assert store.find_by_schedule_id("sched-9").id == entry.id

    reloaded = PendingPlanStore(account_id="acct_pending")
    again = reloaded.find_by_schedule_id("sched-9")
    assert again is not None
    assert again.label == "每天 09:00"

    assert reloaded.update_plan(again.id, {"sequence": [{"type": "text", "body": "你好"}]})
    edited_plan = PendingPlanStore(account_id="acct_pending").get(again.id)
    assert edited_plan is not None
    assert edited_plan.plan["sequence"][0]["body"] == "你好"

    reloaded = PendingPlanStore(account_id="acct_pending")
    again = reloaded.get(entry.id)
    assert again is not None
    reloaded.update_trigger(again.id, "2026-10-02T10:00:00", label="2026-10-02 10:00:00")
    updated = PendingPlanStore(account_id="acct_pending").get(again.id)
    assert updated is not None
    assert updated.trigger_at == "2026-10-02T10:00:00"
    assert updated.label == "2026-10-02 10:00:00"
    assert pending_list_text(updated) == "2026-10-02 10:00:00"


def test_recent_queue_puts_newer_plan_first():
    from gui.widgets.send_board import BoardNode
    from gui.tabs.run_monitor import RunMonitorPanel

    older = BoardNode(title="早", payload={"sort_at": "2026-10-13T09:00:00"})
    newer = BoardNode(title="晚", payload={"sort_at": "2026-10-13T18:00:00", "live": True})
    middle = BoardNode(title="中", payload={"sort_at": "2026-10-13T12:00:00"})
    nodes = [older, middle, newer]
    nodes.sort(key=RunMonitorPanel._recent_queue_key, reverse=True)
    assert [node.title for node in nodes] == ["晚", "中", "早"]


def test_unnamed_plan_label_includes_schedule_mode():
    from datetime import datetime

    from app.broadcast.plan import BroadcastPlan
    from gui.tabs.run_monitor import RunMonitorPanel

    plan = BroadcastPlan.model_validate(
        {
            "contacts": [{"display_name": "甲", "conv_local": "1001"}],
            "sequence": [{"type": "text", "body": "hello"}],
            "name": "未命名计划",
        }
    )
    when = datetime(2026, 10, 2, 10, 30, 0)

    assert RunMonitorPanel._display_plan_title(plan, mode="immediate", when=when) == "（立即执行）2026-10-02 10:30:00"
    assert RunMonitorPanel._display_plan_title(plan, mode="after", label="5小时后", when=when) == "（5小时后）2026-10-02 10:30:00"
    assert RunMonitorPanel._display_plan_title(
        plan,
        mode="after",
        when=when,
        created_at="2026-10-02T05:30:00",
    ) == "（5小时后）2026-10-02 10:30:00"
    assert RunMonitorPanel._display_plan_title(plan, mode="at", when=when) == "（具体时刻）2026-10-02 10:30:00"
    assert RunMonitorPanel._display_plan_title(plan, mode="cron", label="每天 09:00", when=when) == "（循环任务）每天 09:00"
