"""Step 6: 反风控灰度结果归档与默认参数回写建议。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import resolve_path


@dataclass(frozen=True)
class TierObservation:
    tier_id: str
    msg_gap_seconds: tuple[float, float]
    contact_gap_seconds: tuple[float, float]
    contacts_tested: int
    total_steps: int
    failed_steps: int
    warning_count: int
    notes: str = ""

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "TierObservation":
        return cls(
            tier_id=str(row["tier_id"]).strip(),
            msg_gap_seconds=_as_gap(row["msg_gap_seconds"], "msg_gap_seconds"),
            contact_gap_seconds=_as_gap(row["contact_gap_seconds"], "contact_gap_seconds"),
            contacts_tested=int(row.get("contacts_tested", 0)),
            total_steps=int(row.get("total_steps", 0)),
            failed_steps=int(row.get("failed_steps", 0)),
            warning_count=int(row.get("warning_count", 0)),
            notes=str(row.get("notes", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier_id": self.tier_id,
            "msg_gap_seconds": [self.msg_gap_seconds[0], self.msg_gap_seconds[1]],
            "contact_gap_seconds": [self.contact_gap_seconds[0], self.contact_gap_seconds[1]],
            "contacts_tested": self.contacts_tested,
            "total_steps": self.total_steps,
            "failed_steps": self.failed_steps,
            "warning_count": self.warning_count,
            "failed_rate": self.failed_rate,
            "notes": self.notes,
        }

    @property
    def failed_rate(self) -> float:
        if self.total_steps <= 0:
            return 0.0
        return self.failed_steps / self.total_steps


def _as_gap(raw: Any, field: str) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"{field} 必须是长度为 2 的数组")
    lo = float(raw[0])
    hi = float(raw[1])
    if lo < 0 or hi < lo:
        raise ValueError(f"{field} 非法: [{lo}, {hi}]")
    return (lo, hi)


def _apply_speed_margin(gap: tuple[float, float], speed_margin: float) -> tuple[float, float]:
    # 速度下降 20% 等价于间隔放大 1 / (1 - 0.2) = 1.25
    factor = 1.0 / (1.0 - speed_margin)
    return (round(gap[0] * factor, 2), round(gap[1] * factor, 2))


def recommend_defaults(
    tiers: list[TierObservation],
    *,
    speed_margin: float = 0.2,
    max_failed_rate: float = 0.05,
    min_contacts: int = 5,
) -> dict[str, Any]:
    if not tiers:
        raise ValueError("tiers 不能为空")
    if not (0 < speed_margin < 1):
        raise ValueError("speed_margin 必须在 (0, 1) 之间")

    safe_idx = -1
    for idx, tier in enumerate(tiers):
        if tier.contacts_tested < min_contacts:
            continue
        if tier.warning_count > 0:
            continue
        if tier.failed_rate > max_failed_rate:
            continue
        safe_idx = idx

    if safe_idx < 0:
        raise ValueError("没有找到满足条件的安全档位")

    safe_tier = tiers[safe_idx]
    unsafe_tier = tiers[safe_idx + 1] if safe_idx + 1 < len(tiers) else None

    if unsafe_tier is None:
        msg_gap = safe_tier.msg_gap_seconds
        contact_gap = safe_tier.contact_gap_seconds
        based_on = "safe_tier"
    else:
        msg_gap_margin = _apply_speed_margin(unsafe_tier.msg_gap_seconds, speed_margin)
        contact_gap_margin = _apply_speed_margin(unsafe_tier.contact_gap_seconds, speed_margin)
        msg_gap = (
            max(safe_tier.msg_gap_seconds[0], msg_gap_margin[0]),
            max(safe_tier.msg_gap_seconds[1], msg_gap_margin[1]),
        )
        contact_gap = (
            max(safe_tier.contact_gap_seconds[0], contact_gap_margin[0]),
            max(safe_tier.contact_gap_seconds[1], contact_gap_margin[1]),
        )
        based_on = f"unsafe_tier:{unsafe_tier.tier_id}"

    return {
        "recommended_msg_gap_seconds": [msg_gap[0], msg_gap[1]],
        "recommended_contact_gap_seconds": [contact_gap[0], contact_gap[1]],
        "safe_tier_id": safe_tier.tier_id,
        "based_on": based_on,
        "speed_margin": speed_margin,
        "max_failed_rate": max_failed_rate,
        "min_contacts": min_contacts,
    }


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Step 6 风控灰度结果归档与默认参数建议")
    parser.add_argument("--input", required=True, help="输入 observations JSON 文件")
    parser.add_argument("--output", default="", help="输出报告 JSON 文件")
    parser.add_argument("--speed-margin", type=float, default=0.2, help="速度安全余量（默认 0.2）")
    parser.add_argument("--max-failed-rate", type=float, default=0.05, help="失败率阈值（默认 0.05）")
    parser.add_argument("--min-contacts", type=int, default=5, help="每档最少联系人数量（默认 5）")
    args = parser.parse_args()

    src = resolve_path(args.input)
    payload = json.loads(Path(src).read_text(encoding="utf-8"))
    tiers = [TierObservation.from_dict(row) for row in payload.get("tiers", [])]
    result = recommend_defaults(
        tiers,
        speed_margin=args.speed_margin,
        max_failed_rate=args.max_failed_rate,
        min_contacts=args.min_contacts,
    )
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_file": str(src),
        "tiers": [row.to_dict() for row in tiers],
        "result": result,
    }
    out = args.output.strip()
    if not out:
        out = f"runtime/reports/step6_risk_tuning_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path = resolve_path(out)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["result"], ensure_ascii=False))
    print(f"report={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
