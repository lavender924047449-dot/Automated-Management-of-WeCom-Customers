"""把计划展开成一条条可发送的消息。

顶层 step_index 与原来的执行器一致：联系人下标 × 序列长度 + 序列下标。
素材包内部用 inner_index 区分，叶子步骤的 inner_index 为 -1。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from app.broadcast.plan import BroadcastPlan, BundleStep, SequenceStep

TERMINAL_STATUSES = frozenset({"sent", "failed", "blocked", "resolve_failed"})
FAILED_STATUSES = frozenset({"failed", "resolve_failed", "blocked"})


@dataclass
class SendUnit:
    contact_index: int
    contact_name: str
    step_index: int
    sequence_index: int
    inner_index: int
    ordinal: int
    step_type: str
    leaf: Any | None = None
    bundle_asset_type: str = ""
    bundle_item_id: str = ""
    bundle_id: str = ""
    bundle_name: str = ""
    presentation: str = ""
    gap_after_seconds: float = 0.0
    contact_break_after: bool = False
    blocked_reason: str = ""

    @property
    def key(self) -> tuple[int, int]:
        return (int(self.step_index), int(self.inner_index))


def format_gap(seconds: float) -> str:
    """间隔文案：整时、整分，否则秒。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 3600 and abs(value % 3600) < 1e-6:
        return f"间隔 {int(value // 3600)}时"
    if value >= 60 and abs(value % 60) < 1e-6:
        return f"间隔 {int(value // 60)}分"
    if abs(value - round(value)) < 1e-6:
        return f"间隔 {int(round(value))}秒"
    return f"间隔 {value:g}秒"


def format_contact_gap(seconds: float) -> str:
    text = format_gap(seconds)
    return "换人" + text


def expand_send_units(
    plan: BroadcastPlan,
    *,
    load_bundle: Callable[[str], Any | None] | None = None,
) -> list[SendUnit]:
    sequence = list(plan.sequence)
    seq_len = len(sequence)
    units: list[SendUnit] = []
    for contact_index, contact in enumerate(plan.contacts):
        contact_units: list[SendUnit] = []
        for sequence_index, step in enumerate(sequence):
            global_index = contact_index * seq_len + sequence_index
            parts = _expand_step(
                step,
                sequence_index=sequence_index,
                global_index=global_index,
                load_bundle=load_bundle,
            )
            if parts and sequence_index < len(plan.step_gaps) and sequence_index < seq_len - 1:
                parts[-1] = replace(parts[-1], gap_after_seconds=float(plan.step_gaps[sequence_index]))
            contact_units.extend(parts)
        numbered: list[SendUnit] = []
        for ordinal, unit in enumerate(contact_units, start=1):
            numbered.append(
                replace(
                    unit,
                    contact_index=contact_index,
                    contact_name=contact.display_name,
                    ordinal=ordinal,
                )
            )
        if numbered and contact_index < len(plan.contacts) - 1:
            numbered[-1] = replace(numbered[-1], contact_break_after=True)
        units.extend(numbered)
    return units


def latest_unit_status(rows: list[dict[str, Any]]) -> dict[tuple[int, int], str]:
    latest: dict[tuple[int, int], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("type", "")).strip() == "plan_meta":
            continue
        try:
            step_index = int(row.get("step_index", -1))
        except (TypeError, ValueError):
            continue
        if step_index < 0:
            continue
        raw_inner = row.get("inner_index", -1)
        try:
            inner_index = int(raw_inner)
        except (TypeError, ValueError):
            inner_index = -1
        status = str(row.get("status", "")).strip()
        if not status or status in {"before_send", "retry_scheduled"}:
            continue
        latest[(step_index, inner_index)] = status
    return latest


def select_units(
    units: list[SendUnit],
    rows: list[dict[str, Any]] | None = None,
    *,
    only_failed: bool = False,
    only_keys: set[tuple[int, int]] | None = None,
    only_step_indexes: set[int] | None = None,
    start_step_index: int = 0,
) -> list[SendUnit]:
    """选出这一轮要发的单元。

    only_keys 精确到某一条。only_step_indexes 选中某个顶层步骤的全部内条。
    only_failed 只补失败项。否则从第一条还没成功、也没失败的消息接到结尾，
    已经成功或失败的跳过。
    """
    if only_keys is not None:
        wanted = {(int(a), int(b)) for a, b in only_keys}
        return [unit for unit in units if unit.key in wanted]
    if only_step_indexes is not None:
        wanted_steps = {int(idx) for idx in only_step_indexes}
        return [unit for unit in units if unit.step_index in wanted_steps]
    latest = latest_unit_status(list(rows or []))
    if only_failed:
        return [unit for unit in units if latest.get(unit.key, "") in FAILED_STATUSES]
    start = max(0, int(start_step_index))
    selected: list[SendUnit] = []
    opened = False
    for unit in units:
        if unit.step_index < start:
            continue
        status = latest.get(unit.key, "")
        if not opened:
            if status in TERMINAL_STATUSES:
                continue
            opened = True
        elif status in TERMINAL_STATUSES:
            continue
        selected.append(unit)
    return selected


def _expand_step(
    step: SequenceStep,
    *,
    sequence_index: int,
    global_index: int,
    load_bundle: Callable[[str], Any | None] | None,
) -> list[SendUnit]:
    if not isinstance(step, BundleStep):
        return [
            SendUnit(
                contact_index=0,
                contact_name="",
                step_index=global_index,
                sequence_index=sequence_index,
                inner_index=-1,
                ordinal=0,
                step_type=step.type,
                leaf=step,
            )
        ]
    if step.presentation == "expanded":
        return _units_from_leaves(
            leaves=list(step.items),
            gaps=list(step.item_gaps),
            sequence_index=sequence_index,
            global_index=global_index,
            bundle_id=step.bundle_id,
            bundle_name=step.bundle_name,
            presentation="expanded",
            empty_reason="素材包没有可发送的步骤",
        )
    bundle = load_bundle(step.bundle_id) if load_bundle is not None else None
    if bundle is None:
        return [
            _blocked_unit(
                sequence_index=sequence_index,
                global_index=global_index,
                bundle_id=step.bundle_id,
                bundle_name=step.bundle_name,
                presentation="atomic",
                reason="素材包不存在",
            )
        ]
    items = list(getattr(bundle, "items", []) or [])
    gaps = [float(x) for x in (getattr(bundle, "step_gaps", []) or [])]
    name = step.bundle_name or str(getattr(bundle, "name", "") or "")
    leaves: list[tuple[str, str]] = []
    for item in items:
        asset_type = str(getattr(item, "asset_type", "") or "").strip()
        item_id = str(getattr(item, "item_id", "") or "").strip()
        if asset_type and item_id:
            leaves.append((asset_type, item_id))
    return _units_from_asset_items(
        items=leaves,
        gaps=gaps,
        sequence_index=sequence_index,
        global_index=global_index,
        bundle_id=step.bundle_id,
        bundle_name=name,
        presentation="atomic",
    )


def _units_from_leaves(
    *,
    leaves: list[Any],
    gaps: list[float],
    sequence_index: int,
    global_index: int,
    bundle_id: str,
    bundle_name: str,
    presentation: str,
    empty_reason: str,
) -> list[SendUnit]:
    if not leaves:
        return [
            _blocked_unit(
                sequence_index=sequence_index,
                global_index=global_index,
                bundle_id=bundle_id,
                bundle_name=bundle_name,
                presentation=presentation,
                reason=empty_reason,
            )
        ]
    out: list[SendUnit] = []
    for index, leaf in enumerate(leaves):
        gap = float(gaps[index]) if index < len(gaps) else 0.0
        out.append(
            SendUnit(
                contact_index=0,
                contact_name="",
                step_index=global_index,
                sequence_index=sequence_index,
                inner_index=index,
                ordinal=0,
                step_type=str(getattr(leaf, "type", "") or ""),
                leaf=leaf,
                bundle_id=bundle_id,
                bundle_name=bundle_name,
                presentation=presentation,
                gap_after_seconds=gap,
            )
        )
    if out:
        out[-1] = replace(out[-1], gap_after_seconds=0.0)
    return out


def _units_from_asset_items(
    *,
    items: list[tuple[str, str]],
    gaps: list[float],
    sequence_index: int,
    global_index: int,
    bundle_id: str,
    bundle_name: str,
    presentation: str,
) -> list[SendUnit]:
    if not items:
        return [
            _blocked_unit(
                sequence_index=sequence_index,
                global_index=global_index,
                bundle_id=bundle_id,
                bundle_name=bundle_name,
                presentation=presentation,
                reason="素材包为空",
            )
        ]
    out: list[SendUnit] = []
    for index, (asset_type, item_id) in enumerate(items):
        gap = float(gaps[index]) if index < len(gaps) else 0.0
        out.append(
            SendUnit(
                contact_index=0,
                contact_name="",
                step_index=global_index,
                sequence_index=sequence_index,
                inner_index=index,
                ordinal=0,
                step_type=asset_type,
                bundle_asset_type=asset_type,
                bundle_item_id=item_id,
                bundle_id=bundle_id,
                bundle_name=bundle_name,
                presentation=presentation,
                gap_after_seconds=gap,
            )
        )
    if out:
        out[-1] = replace(out[-1], gap_after_seconds=0.0)
    return out


def _blocked_unit(
    *,
    sequence_index: int,
    global_index: int,
    bundle_id: str,
    bundle_name: str,
    presentation: str,
    reason: str,
) -> SendUnit:
    return SendUnit(
        contact_index=0,
        contact_name="",
        step_index=global_index,
        sequence_index=sequence_index,
        inner_index=0 if presentation else -1,
        ordinal=0,
        step_type="bundle",
        bundle_id=bundle_id,
        bundle_name=bundle_name,
        presentation=presentation,
        blocked_reason=reason,
    )
