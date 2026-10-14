"""发送单元展开。"""

from app.broadcast.plan import BroadcastPlan
from app.broadcast.send_units import expand_send_units, select_units


class _Item:
    def __init__(self, asset_type: str, item_id: str) -> None:
        self.asset_type = asset_type
        self.item_id = item_id


class _Bundle:
    def __init__(self) -> None:
        self.name = "原子包"
        self.items = [_Item("text", "t1"), _Item("text", "t2")]
        self.step_gaps = [8.0]


def test_expanded_bundle_stays_under_one_step():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["甲", "乙"],
            "sequence": [
                {"type": "text", "body": "开头"},
                {
                    "type": "bundle",
                    "bundle_id": "b1",
                    "bundle_name": "晨间",
                    "presentation": "expanded",
                    "items": [
                        {"type": "text", "body": "一"},
                        {"type": "text", "body": "二"},
                    ],
                    "item_gaps": [5],
                },
            ],
            "step_gaps": [6],
            "contact_gap": 9,
        }
    )
    units = expand_send_units(plan)
    first = [unit for unit in units if unit.contact_name == "甲"]
    assert [unit.ordinal for unit in first] == [1, 2, 3]
    assert [(unit.step_index, unit.inner_index, unit.step_type) for unit in first] == [
        (0, -1, "text"),
        (1, 0, "text"),
        (1, 1, "text"),
    ]
    assert first[1].gap_after_seconds == 5
    assert first[0].gap_after_seconds == 6
    assert first[-1].contact_break_after is True
    assert units[-1].step_index == 3


def test_atomic_bundle_reads_library_items():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["甲"],
            "sequence": [{"type": "bundle", "bundle_id": "b9", "presentation": "atomic"}],
        }
    )
    units = expand_send_units(plan, load_bundle=lambda _bid: _Bundle())
    assert [unit.inner_index for unit in units] == [0, 1]
    assert units[0].bundle_item_id == "t1"
    assert units[0].gap_after_seconds == 8
    assert units[0].presentation == "atomic"


def test_resume_skips_sent_and_failed():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["甲"],
            "sequence": [
                {"type": "text", "body": "a"},
                {"type": "text", "body": "b"},
                {"type": "text", "body": "c"},
            ],
        }
    )
    units = expand_send_units(plan)
    rows = [
        {"step_index": 0, "status": "sent", "type": "text"},
        {"step_index": 1, "status": "failed", "type": "text"},
    ]
    selected = select_units(units, rows)
    assert [unit.step_index for unit in selected] == [2]
    failed = select_units(units, rows, only_failed=True)
    assert [unit.step_index for unit in failed] == [1]


def test_select_units_only_keys_matches_inner_index():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["甲"],
            "sequence": [
                {
                    "type": "bundle",
                    "bundle_id": "b1",
                    "presentation": "expanded",
                    "items": [
                        {"type": "text", "body": "一"},
                        {"type": "text", "body": "二"},
                    ],
                    "item_gaps": [3],
                }
            ],
        }
    )
    units = expand_send_units(plan)
    selected = select_units(units, only_keys={(0, 1)})
    assert len(selected) == 1
    assert selected[0].key == (0, 1)


def test_resume_from_paused_unit():
    plan = BroadcastPlan.model_validate(
        {
            "contacts": ["甲"],
            "sequence": [
                {"type": "text", "body": "a"},
                {"type": "text", "body": "b"},
                {"type": "text", "body": "c"},
            ],
        }
    )
    units = expand_send_units(plan)
    rows = [
        {"step_index": 0, "inner_index": -1, "status": "sent", "type": "text"},
        {"step_index": 1, "inner_index": -1, "status": "paused", "type": "text"},
        {"step_index": 2, "inner_index": -1, "status": "failed", "type": "text"},
    ]
    selected = select_units(units, rows)
    assert [unit.step_index for unit in selected] == [1]
