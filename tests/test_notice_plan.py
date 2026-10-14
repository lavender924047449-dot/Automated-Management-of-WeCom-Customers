"""群公告计划：先公告，勾选后再 @所有人。"""

from __future__ import annotations

import pytest

from app.broadcast.dto import PlanContact
from app.broadcast.notice_plan import build_notice_plan


def _group(conv_local: str, name: str) -> PlanContact:
    return PlanContact(conv_local=conv_local, display_name=name)


def test_notice_only_plan() -> None:
    plan = build_notice_plan(
        groups=[_group("2", "B群"), _group("1", "A群")],
        notice_body="  本周安排  ",
        at_enabled=False,
        at_body="不会发出去",
        contact_gap=2,
        name="周一公告",
        schedule_mode="immediate",
        schedule_label="立即执行",
    )
    assert [item.display_name for item in plan.contacts] == ["A群", "B群"]
    assert [step.type for step in plan.sequence] == ["notice"]
    assert plan.sequence[0].body == "本周安排"
    assert plan.step_gaps == []
    assert plan.contact_gap == 2
    assert plan.name == "周一公告"


def test_notice_then_at_plan() -> None:
    plan = build_notice_plan(
        groups=[_group("1", "群甲")],
        notice_body="本周安排",
        at_enabled=True,
        at_body="  请查收  ",
        contact_gap=1,
        name="",
        schedule_mode="after",
        schedule_label="5分钟后",
    )
    assert [step.type for step in plan.sequence] == ["notice", "at"]
    assert plan.sequence[1].body == "请查收"
    assert plan.step_gaps == [3.0]


def test_notice_plan_requires_text() -> None:
    with pytest.raises(ValueError, match="群聊"):
        build_notice_plan(
            groups=[],
            notice_body="有字",
            at_enabled=False,
            at_body="",
            contact_gap=1,
            name="",
            schedule_mode="immediate",
            schedule_label="立即执行",
        )
    with pytest.raises(ValueError, match="公告"):
        build_notice_plan(
            groups=[_group("1", "群甲")],
            notice_body="   ",
            at_enabled=False,
            at_body="",
            contact_gap=1,
            name="",
            schedule_mode="immediate",
            schedule_label="立即执行",
        )
    with pytest.raises(ValueError, match="@所有人"):
        build_notice_plan(
            groups=[_group("1", "群甲")],
            notice_body="本周安排",
            at_enabled=True,
            at_body="  ",
            contact_gap=1,
            name="",
            schedule_mode="immediate",
            schedule_label="立即执行",
        )
