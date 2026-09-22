"""Step 4.1: BroadcastPlan schema 校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broadcast.plan import (
    BroadcastPlan,
    DEFAULT_CONTACT_GAP,
    DEFAULT_MSG_GAP,
    ImageStep,
    load_plan,
    save_plan,
    step_status,
)


def _valid_payload(**overrides):
    data = {
        "contacts": ["海鸟与鱼", "张三"],
        "sequence": [
            {"type": "voice", "voice_id": "voice-uuid-1"},
            {"type": "text", "body": "你好，方便聊聊吗？"},
            {"type": "image", "path": "/sdcard/Test-Voice/voice_lib/demo.png"},
        ],
        "msg_gap_seconds": [8, 25],
        "contact_gap_seconds": [45, 120],
        "perturb": True,
    }
    data.update(overrides)
    return data


def test_valid_plan_roundtrip(tmp_path: Path):
    plan = BroadcastPlan.model_validate(_valid_payload())
    assert [x.display_name for x in plan.contacts] == ["海鸟与鱼", "张三"]
    assert [x.conv_local for x in plan.contacts] == ["", ""]
    assert [step.type for step in plan.sequence] == ["voice", "text", "image"]
    assert plan.msg_gap_seconds == (8.0, 25.0)
    assert plan.contact_gap_seconds == (45.0, 120.0)
    assert plan.perturb is True
    assert plan.has_blocked_steps() is False
    assert all(step_status(step) == "ready" for step in plan.sequence)

    path = save_plan(plan, tmp_path / "plan.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sequence"][1]["body"].startswith("你好")
    loaded = load_plan(path)
    assert [x.display_name for x in loaded.contacts] == [x.display_name for x in plan.contacts]
    assert loaded.sequence[0].voice_id == "voice-uuid-1"


def test_text_step_template_mode():
    plan = BroadcastPlan.model_validate(
        _valid_payload(
            sequence=[
                {"type": "text", "template_id": "tpl-1", "variant_pick": "random"},
                {"type": "text", "template_id": "tpl-2", "variant_pick": "index:1"},
            ]
        )
    )
    first = plan.sequence[0]
    second = plan.sequence[1]
    assert first.type == "text"
    assert first.template_id == "tpl-1"
    assert second.variant_pick == "index:1"


def test_missing_required_fields():
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate({"sequence": [{"type": "text", "body": "hi"}]})
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate({"contacts": ["海鸟与鱼"]})
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(sequence=[{"type": "voice"}])
        )
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(sequence=[{"type": "text"}])
        )
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(sequence=[{"type": "image"}])
        )


def test_illegal_type_rejected():
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(sequence=[{"type": "sticker", "path": "x"}])
        )


def test_empty_or_blank_fields_rejected():
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(_valid_payload(contacts=["  "]))
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(sequence=[{"type": "text", "body": "   "}])
        )
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(msg_gap_seconds=[25, 8])
        )
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(sequence=[{"type": "text", "template_id": "tpl-1", "variant_pick": "pick:1"}])
        )
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(
            _valid_payload(sequence=[{"type": "text", "body": "   ", "template_id": "   "}])
        )


def test_contacts_dedup_and_strip():
    plan = BroadcastPlan.model_validate(
        _valid_payload(contacts=[" 海鸟与鱼 ", "海鸟与鱼", "张三"])
    )
    assert [x.display_name for x in plan.contacts] == ["海鸟与鱼", "张三"]


def test_contacts_plan_contact_roundtrip():
    plan = BroadcastPlan.model_validate(
        _valid_payload(
            contacts=[
                {"conv_local": "1001", "display_name": "海鸟与鱼"},
                {"conv_local": "1002", "display_name": "张三"},
            ]
        )
    )
    dumped = plan.model_dump(mode="json")
    assert dumped["contacts"][0] == {"conv_local": "1001", "display_name": "海鸟与鱼"}
    rebuilt = BroadcastPlan.model_validate(dumped)
    assert [x.conv_local for x in rebuilt.contacts] == ["1001", "1002"]


def test_legacy_string_contacts_compatible():
    plan = BroadcastPlan.model_validate(
        _valid_payload(contacts=[" 海鸟与鱼 ", "张三"])
    )
    assert [x.display_name for x in plan.contacts] == ["海鸟与鱼", "张三"]
    assert [x.conv_local for x in plan.contacts] == ["", ""]


def test_image_is_ready_after_step_35():
    step = ImageStep(type="image", path="/sdcard/Test-Voice/voice_lib/a.png")
    assert step_status(step) == "ready"
    plan = BroadcastPlan.model_validate(
        _valid_payload(sequence=[{"type": "image", "path": "/sdcard/a.png"}])
    )
    assert plan.has_blocked_steps() is False


def test_phase_b_types_parse_but_marked_not_implemented():
    plan = BroadcastPlan.model_validate(
        _valid_payload(
            sequence=[
                {"type": "text", "body": "先发文字"},
                {"type": "oa", "template_id": "oa-1"},
                {"type": "wxa", "template_id": "wxa-1", "thumb_path": "/sdcard/t.png"},
                {"type": "video", "path": "/sdcard/a.mp4", "duration": 8},
                {"type": "channel", "template_id": "ch-1"},
            ]
        )
    )
    assert [step.type for step in plan.ready_sequence()] == ["text", "oa", "video"]
    blocked = plan.blocked_summary()
    assert [row["type"] for row in blocked] == ["wxa", "channel"]
    assert all(row["status"] == "not_implemented" for row in blocked)
    assert plan.has_blocked_steps() is True


def test_default_gaps_match_step6_tuning():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["海鸟与鱼"],
            "sequence": [{"type": "text", "body": "hello"}],
            "perturb": True,
        }
    )
    assert plan.msg_gap_seconds == DEFAULT_MSG_GAP
    assert plan.contact_gap_seconds == DEFAULT_CONTACT_GAP
