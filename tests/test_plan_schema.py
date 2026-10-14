"""Step 4.1: BroadcastPlan schema 校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broadcast.plan import (
    BroadcastPlan,
    DEFAULT_CONTACT_GAP,
    DEFAULT_CONTACT_GAP_FLAT,
    DEFAULT_MSG_GAP,
    DEFAULT_STEP_GAP,
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
        "step_gaps": [10.0, 10.0],
        "contact_gap": 60.0,
    }
    data.update(overrides)
    return data


def test_valid_plan_roundtrip(tmp_path: Path):
    plan = BroadcastPlan.model_validate(_valid_payload())
    assert [x.display_name for x in plan.contacts] == ["海鸟与鱼", "张三"]
    assert [x.conv_local for x in plan.contacts] == ["", ""]
    assert [step.type for step in plan.sequence] == ["voice", "text", "image"]
    assert plan.step_gaps == [10.0, 10.0]
    assert plan.contact_gap == 60.0
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
    # step_gaps 值 < 3 会被 clamp 到 3，不抛异常
    plan = BroadcastPlan.model_validate(
        _valid_payload(step_gaps=[1.0, 1.0])
    )
    assert all(g >= 3.0 for g in plan.step_gaps)
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


def test_doc_and_channel_are_ready_link_cards():
    plan = BroadcastPlan.model_validate(
        _valid_payload(
            sequence=[
                {"type": "text", "body": "先发文字"},
                {"type": "oa", "template_id": "oa-1"},
                {"type": "doc", "template_id": "doc-1"},
                {"type": "wxa", "template_id": "wxa-1", "thumb_path": "/sdcard/t.png"},
                {"type": "video", "path": "/sdcard/a.mp4", "duration": 8},
                {"type": "channel", "template_id": "ch-1"},
            ]
        )
    )
    assert [step.type for step in plan.ready_sequence()] == [
        "text",
        "oa",
        "doc",
        "wxa",
        "video",
        "channel",
    ]
    assert plan.blocked_summary() == []
    assert plan.has_blocked_steps() is False


def test_default_gaps_match_step6_tuning():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["海鸟与鱼"],
            "sequence": [{"type": "text", "body": "hello"}],
        }
    )
    # 单条序列 → step_gaps 为空列表（长度 = 0）
    assert plan.step_gaps == []
    assert plan.contact_gap == DEFAULT_CONTACT_GAP_FLAT


def test_backward_compat_old_format():
    """旧格式 msg_gap_seconds / contact_gap_seconds / perturb 正确迁移。"""
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["海鸥"],
            "sequence": [
                {"type": "text", "body": "a"},
                {"type": "text", "body": "b"},
            ],
            "msg_gap_seconds": [10, 30],
            "contact_gap_seconds": [60, 150],
            "perturb": True,
        }
    )
    # step_gaps 取 (10+30)/2 = 20.0
    assert len(plan.step_gaps) == 1
    assert plan.step_gaps[0] == 20.0
    # contact_gap 取 (60+150)/2 = 105.0
    assert plan.contact_gap == 105.0


def test_plan_name_and_expanded_bundle_snapshot():
    plan = BroadcastPlan.model_validate(
        {
            "name": "  早报  ",
            "contacts": ["海鸟与鱼"],
            "sequence": [
                {
                    "type": "bundle",
                    "bundle_id": "b1",
                    "bundle_name": "晨间包",
                    "presentation": "expanded",
                    "items": [
                        {"type": "text", "body": "甲", "display_name": "问候"},
                        {"type": "text", "body": "乙"},
                    ],
                    "item_gaps": [4],
                }
            ],
        }
    )
    assert plan.name == "早报"
    bundle = plan.sequence[0]
    assert bundle.presentation == "expanded"
    assert bundle.bundle_name == "晨间包"
    assert [item.body for item in bundle.items] == ["甲", "乙"]
    assert bundle.item_gaps == [4.0]
    assert bundle.items[0].display_name == "问候"


def test_legacy_bundle_stays_atomic():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["海鸟与鱼"],
            "sequence": [{"type": "bundle", "bundle_id": "b1"}],
        }
    )
    bundle = plan.sequence[0]
    assert bundle.presentation == "atomic"
    assert bundle.items == []
    assert plan.name == ""


def test_at_and_notice_steps_roundtrip(tmp_path: Path):
    plan = BroadcastPlan.model_validate(
        _valid_payload(
            sequence=[
                {"type": "at", "body": "大家好"},
                {"type": "notice", "template_id": "tpl-1", "variant_pick": "index:0", "display_name": "周公告"},
            ],
            step_gaps=[3.0],
        )
    )
    assert [step.type for step in plan.sequence] == ["at", "notice"]
    assert step_status(plan.sequence[0]) == "ready"
    assert step_status(plan.sequence[1]) == "ready"
    dest = tmp_path / "plan.json"
    save_plan(plan, dest)
    loaded = load_plan(dest)
    assert loaded.sequence[0].body == "大家好"
    assert loaded.sequence[1].template_id == "tpl-1"
    assert loaded.sequence[1].display_name == "周公告"


def test_at_step_requires_body_or_template():
    with pytest.raises(ValidationError):
        BroadcastPlan.model_validate(_valid_payload(sequence=[{"type": "at"}], step_gaps=[]))
