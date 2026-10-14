"""把计划和发送状态收成执行结果页上的树。"""

from __future__ import annotations

from collections.abc import Callable

from app.broadcast.asset_preview_source import usable_asset_name
from app.broadcast.plan import BroadcastPlan, BundleStep
from app.broadcast.send_units import SendUnit, expand_send_units, format_contact_gap, format_gap
from gui.i18n import asset_type_to_zh
from gui.widgets.send_board import BoardNode

NameLookup = Callable[[str, str, str], str]


def status_text(state: dict | None, *, bundle_failed: bool = False, bundle_phase: str = "") -> str:
    if bundle_phase in {"prepare", "sending"} and bundle_failed:
        percent = "30%" if bundle_phase == "prepare" else "70%"
        return f"发送失败 · 发送中 {percent}"
    if bundle_phase == "prepare":
        return "30%"
    if bundle_phase == "sending":
        return "70%"
    if not state:
        return "未开始"
    phase = str(state.get("phase", ""))
    status = str(state.get("status", ""))
    if phase == "prepare":
        return "30%"
    if phase == "sending":
        return "70%"
    if status == "sent":
        return "发送成功"
    if status in {"failed", "blocked", "resolve_failed"}:
        return "发送失败"
    if status == "paused":
        return "已暂停"
    return "未开始"


def action_for(state: dict | None) -> str:
    if not state:
        return ""
    status = str(state.get("status", ""))
    if status == "paused":
        return "resume"
    if status in {"failed", "blocked", "resolve_failed"}:
        return "retry"
    return ""


def build_plan_node(
    plan: BroadcastPlan,
    *,
    title: str,
    clock: str,
    states: dict[tuple[int, int], dict] | None = None,
    load_bundle=None,
    show_actions: bool = False,
    name_of: NameLookup | None = None,
) -> BoardNode:
    known = states or {}
    units = expand_send_units(plan, load_bundle=load_bundle)
    by_contact: dict[int, list[SendUnit]] = {}
    for unit in units:
        by_contact.setdefault(unit.contact_index, []).append(unit)
    contacts: list[BoardNode] = []
    contact_ids = sorted(by_contact)
    for position, contact_index in enumerate(contact_ids):
        group = by_contact[contact_index]
        name = group[0].contact_name or "未命名联系人"
        messages = _message_nodes(plan, group, known, show_actions=show_actions, name_of=name_of)
        contacts.append(
            BoardNode(
                title=f"● {name} · {len(group)}条",
                kind="contact",
                children=messages,
                payload={"contact_index": contact_index},
            )
        )
        if position < len(contact_ids) - 1:
            contacts.append(
                BoardNode(
                    title=f"⇄ {format_contact_gap(plan.contact_gap)}",
                    kind="gap",
                    payload={},
                )
            )
    clock_part = f"{clock}  " if str(clock).strip() else ""
    shown = title or "未命名计划"
    heading = f"▣ {clock_part}{shown}".strip()
    return BoardNode(title=heading, kind="plan", children=contacts, payload={"plan_name": title})


def _message_nodes(
    plan: BroadcastPlan,
    units: list[SendUnit],
    states: dict[tuple[int, int], dict],
    *,
    show_actions: bool,
    name_of: NameLookup | None = None,
) -> list[BoardNode]:
    nodes: list[BoardNode] = []
    index = 0
    while index < len(units):
        unit = units[index]
        step = plan.sequence[unit.sequence_index] if unit.sequence_index < len(plan.sequence) else None
        if isinstance(step, BundleStep) and unit.inner_index >= 0:
            bundle_units: list[SendUnit] = []
            while index < len(units) and units[index].step_index == unit.step_index and units[index].inner_index >= 0:
                bundle_units.append(units[index])
                index += 1
            nodes.append(_bundle_node(bundle_units, states, show_actions=show_actions, name_of=name_of))
            tail = bundle_units[-1]
        else:
            nodes.append(_leaf_node(unit, states, show_actions=show_actions, name_of=name_of))
            tail = unit
            index += 1
        if (
            index < len(units)
            and tail.gap_after_seconds > 0
            and units[index].contact_index == tail.contact_index
            and units[index].step_index != tail.step_index
        ):
            nodes.append(BoardNode(title=f"⏱ {format_gap(tail.gap_after_seconds)}", kind="gap"))
    return nodes


def _bundle_node(
    units: list[SendUnit],
    states: dict[tuple[int, int], dict],
    *,
    show_actions: bool,
    name_of: NameLookup | None = None,
) -> BoardNode:
    first = units[0]
    atomic = first.presentation == "atomic"
    failed = False
    paused = False
    processed = 0
    children: list[BoardNode] = []
    terminal = {"sent", "failed", "blocked", "resolve_failed"}
    for index, unit in enumerate(units):
        state = states.get(unit.key)
        status = str(state.get("status", "")) if state else ""
        if status in terminal:
            processed += 1
        if status in {"failed", "blocked", "resolve_failed"}:
            failed = True
        if status == "paused":
            paused = True
        children.append(_leaf_node(unit, states, show_actions=show_actions, nested=True, name_of=name_of))
        if index < len(units) - 1 and unit.gap_after_seconds > 0:
            children.append(BoardNode(title=f"⏱ {format_gap(unit.gap_after_seconds)}", kind="gap"))

    outer = states.get((first.step_index, -1))
    if outer and str(outer.get("status", "")) == "failed":
        failed = True
    total = len(units)
    progress = f"{processed}/{total}"
    # 包进度只按已经结束的条数走。正在发送的那一条自己显示 30%/70%，不能把包进度写成同一条的百分比。
    if failed:
        text = f"素材包进度：失败 · {progress} 条素材"
    elif total > 0 and processed == total:
        text = f"素材包进度：已完成 · {progress} 条素材"
    elif paused and processed < total:
        text = f"素材包进度：已暂停 · {progress} 条素材"
    elif processed > 0:
        text = f"素材包进度：{progress} 条素材已完成"
    else:
        text = f"素材包进度：0/{total} 条素材"

    label = asset_type_to_zh("bundle")
    name = first.bundle_name or "未命名素材包"
    return BoardNode(
        title=f"◆ {label} · {name}",
        status=text,
        kind="bundle_atomic" if atomic else "bundle",
        expanded=not atomic,
        children=children,
        payload={"step_index": first.step_index, "inner_index": -1, "bundle_id": first.bundle_id},
    )

def _unit_identity(unit: SendUnit) -> tuple[str, str, str]:
    kind = (unit.bundle_asset_type or unit.step_type or "").strip()
    item_id = str(unit.bundle_item_id or "").strip()
    path = ""
    leaf = unit.leaf
    if leaf is not None:
        item_id = item_id or str(getattr(leaf, "template_id", "") or getattr(leaf, "voice_id", "") or "").strip()
        path = str(getattr(leaf, "path", "") or "").strip()
    return kind, item_id, path


def _preview_step(unit: SendUnit) -> dict:
    """给双击预览用的步骤快照。原子包只有素材类型和编号，预览时再去素材库补全。"""
    if unit.leaf is not None and hasattr(unit.leaf, "model_dump"):
        step = unit.leaf.model_dump(mode="json")
        name = str(getattr(unit.leaf, "display_name", "") or "")
        return {"step": step, "name": name}
    kind, item_id, path = _unit_identity(unit)
    step: dict = {"type": kind, "item_id": item_id}
    if path:
        step["path"] = path
    if kind == "voice":
        step["voice_id"] = item_id
    elif kind == "link":
        step["type"] = "text"
        step["template_id"] = item_id
    elif kind in {"text", "oa", "doc", "channel", "wxa", "image", "video"}:
        step["template_id"] = item_id
    return {"step": step, "name": ""}


def _leaf_node(
    unit: SendUnit,
    states: dict[tuple[int, int], dict],
    *,
    show_actions: bool,
    nested: bool = False,
    name_of: NameLookup | None = None,
) -> BoardNode:
    state = states.get(unit.key)
    label = asset_type_to_zh(unit.step_type)
    kind, item_id, path = _unit_identity(unit)
    name = ""
    if unit.leaf is not None:
        name = str(getattr(unit.leaf, "display_name", "") or "")
        if unit.step_type in {"at", "notice"} and not usable_asset_name(name, item_id):
            name = str(getattr(unit.leaf, "body", "") or "")
    shown = usable_asset_name(name, item_id)
    if not shown and name_of is not None:
        try:
            shown = usable_asset_name(name_of(kind, item_id, path), item_id)
        except Exception:
            shown = ""
    shown = shown or "未命名"
    prefix = f"{unit.ordinal}." if not nested else f"{unit.inner_index + 1})"
    return BoardNode(
        title=f"{prefix} {label} · {shown}",
        status=status_text(state),
        kind="message",
        action=action_for(state) if show_actions else "",
        payload={
            "step_index": unit.step_index,
            "inner_index": unit.inner_index,
            "step_type": unit.step_type,
            "contact": unit.contact_name,
            "leaf_type": unit.step_type,
            "preview": _preview_step(unit),
        },
    )
