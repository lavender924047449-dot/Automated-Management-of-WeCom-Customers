"""把群公告页的输入收成一份可执行计划。"""

from __future__ import annotations

from app.broadcast.dto import PlanContact
from app.broadcast.plan import DEFAULT_STEP_GAP, BroadcastPlan


def build_notice_plan(
    *,
    groups: list[PlanContact],
    notice_body: str,
    at_enabled: bool,
    at_body: str,
    contact_gap: float,
    name: str,
    schedule_mode: str,
    schedule_label: str,
) -> BroadcastPlan:
    """每个群先发群公告；勾选 @所有人 时，紧接着再发一条 @所有人 消息。"""
    chosen = [
        PlanContact(conv_local=item.conv_local, display_name=item.display_name)
        for item in groups
        if str(item.conv_local).strip() and str(item.display_name).strip()
    ]
    if not chosen:
        raise ValueError("请先勾选群聊。")
    chosen.sort(key=lambda item: item.display_name)
    notice = str(notice_body or "").strip()
    if not notice:
        raise ValueError("请填写公告文字。")
    sequence: list[dict[str, str]] = [{"type": "notice", "body": notice}]
    if at_enabled:
        mention = str(at_body or "").strip()
        if not mention:
            raise ValueError("请填写 @所有人 的文字。")
        sequence.append({"type": "at", "body": mention})
    step_gaps = [DEFAULT_STEP_GAP] if len(sequence) > 1 else []
    return BroadcastPlan.model_validate(
        {
            "contacts": [item.model_dump(mode="json") for item in chosen],
            "sequence": sequence,
            "step_gaps": step_gaps,
            "contact_gap": contact_gap,
            "name": name,
            "schedule_mode": schedule_mode,
            "schedule_label": schedule_label,
        }
    )
