"""Step 4.1: 广播 Plan JSON schema。

一份 sequence 应用到 contacts 中所有人。Phase A 可执行类型为 voice / text / image
（Step 3.5 完成后 image 不再 blocked）。Phase B/C 类型可写入 schema，校验通过但
标记 not_implemented，供 Runner 跳过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from app.broadcast.dto import PlanContact

PHASE_A_TYPES = frozenset({"voice", "text", "image"})
READY_TYPES = frozenset({"voice", "text", "image", "oa", "video", "bundle"})
NOT_IMPLEMENTED_TYPES = frozenset({"wxa", "channel"})
ALLOWED_TYPES = READY_TYPES | NOT_IMPLEMENTED_TYPES

# Step 6 风控灰度后默认值（MuMu + 营销号）。
# 含义：在可接受上限速率基础上预留 20% 安全余量。
DEFAULT_MSG_GAP = (10.0, 30.0)
DEFAULT_CONTACT_GAP = (60.0, 150.0)


def _require_nonempty(value: str, field: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def _validate_gap(pair: tuple[float, float], field: str) -> tuple[float, float]:
    lo, hi = float(pair[0]), float(pair[1])
    if lo < 0 or hi < lo:
        raise ValueError(f"{field} 非法: [{lo}, {hi}]，要求 0 <= min <= max")
    return (lo, hi)


def _is_valid_variant_pick(value: str) -> bool:
    text = value.strip()
    if text == "random":
        return True
    if not text.startswith("index:"):
        return False
    idx = text.removeprefix("index:").strip()
    return idx.isdigit()


class VoiceStep(BaseModel):
    type: Literal["voice"]
    voice_id: str

    @field_validator("voice_id")
    @classmethod
    def _voice_id(cls, v: str) -> str:
        return _require_nonempty(v, "voice_id")


class TextStep(BaseModel):
    type: Literal["text"]
    body: str = ""
    template_id: str = ""
    variant_pick: str = "random"

    @field_validator("body", "template_id", "variant_pick")
    @classmethod
    def _strip_fields(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _validate_text_source(self) -> "TextStep":
        if not self.body and not self.template_id:
            raise ValueError("text step 需提供 body 或 template_id")
        if self.template_id and not _is_valid_variant_pick(self.variant_pick):
            raise ValueError("variant_pick 仅支持 random 或 index:N")
        return self


class ImageStep(BaseModel):
    type: Literal["image"]
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _require_nonempty(v, "path")


class OaStep(BaseModel):
    type: Literal["oa"]
    template_id: str

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, v: str) -> str:
        return _require_nonempty(v, "template_id")


class WxaStep(BaseModel):
    type: Literal["wxa"]
    template_id: str
    thumb_path: str = ""

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, v: str) -> str:
        return _require_nonempty(v, "template_id")

    @field_validator("thumb_path")
    @classmethod
    def _thumb(cls, v: str) -> str:
        return v.strip()


class VideoStep(BaseModel):
    type: Literal["video"]
    path: str
    thumb_path: str = ""
    duration: int | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _require_nonempty(v, "path")

    @field_validator("thumb_path")
    @classmethod
    def _thumb(cls, v: str) -> str:
        return v.strip()

    @field_validator("duration")
    @classmethod
    def _duration(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v < 1:
            raise ValueError("duration 必须 >= 1")
        return v


class ChannelStep(BaseModel):
    """Phase C 占位，避免后续再改 CLI/schema。"""

    type: Literal["channel"]
    template_id: str = ""

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, v: str) -> str:
        return v.strip()


class BundleStep(BaseModel):
    """素材包原子节点。"""

    type: Literal["bundle"]
    bundle_id: str

    @field_validator("bundle_id")
    @classmethod
    def _bundle_id(cls, v: str) -> str:
        return _require_nonempty(v, "bundle_id")


SequenceStep = Annotated[
    Union[VoiceStep, TextStep, ImageStep, OaStep, WxaStep, VideoStep, ChannelStep, BundleStep],
    Field(discriminator="type"),
]


def step_status(step: SequenceStep) -> Literal["ready", "not_implemented"]:
    if step.type in READY_TYPES:
        return "ready"
    return "not_implemented"


class BroadcastPlan(BaseModel):
    contacts: list[PlanContact] = Field(..., min_length=1)
    sequence: list[SequenceStep] = Field(..., min_length=1)
    msg_gap_seconds: tuple[float, float] = DEFAULT_MSG_GAP
    contact_gap_seconds: tuple[float, float] = DEFAULT_CONTACT_GAP
    perturb: bool = True

    @field_validator("contacts")
    @classmethod
    def _contacts(cls, values: list[PlanContact]) -> list[PlanContact]:
        seen: set[str] = set()
        out: list[PlanContact] = []
        for item in values:
            conv_local = item.conv_local.strip()
            key = conv_local or f"name:{item.display_name}"
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(
                PlanContact(
                    conv_local=conv_local,
                    display_name=item.display_name.strip(),
                )
            )
        if not out:
            raise ValueError("contacts 至少要 1 个有效联系人")
        return out

    @model_validator(mode="before")
    @classmethod
    def _compat_contacts_schema(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_contacts = data.get("contacts")
        if not isinstance(raw_contacts, list):
            return data
        normalized: list[dict[str, str] | Any] = []
        changed = False
        for item in raw_contacts:
            if isinstance(item, str):
                name = item.strip()
                if not name:
                    continue
                normalized.append({"conv_local": "", "display_name": name})
                changed = True
                continue
            normalized.append(item)
        if not changed:
            return data
        payload = dict(data)
        payload["contacts"] = normalized
        return payload

    @field_validator("msg_gap_seconds")
    @classmethod
    def _msg_gap(cls, v: tuple[float, float]) -> tuple[float, float]:
        return _validate_gap(v, "msg_gap_seconds")

    @field_validator("contact_gap_seconds")
    @classmethod
    def _contact_gap(cls, v: tuple[float, float]) -> tuple[float, float]:
        return _validate_gap(v, "contact_gap_seconds")

    @model_validator(mode="after")
    def _sequence_not_empty_after_parse(self) -> "BroadcastPlan":
        if not self.sequence:
            raise ValueError("sequence 不能为空")
        return self

    def ready_sequence(self) -> list[SequenceStep]:
        return [step for step in self.sequence if step_status(step) == "ready"]

    def blocked_sequence(self) -> list[SequenceStep]:
        return [step for step in self.sequence if step_status(step) != "ready"]

    def has_blocked_steps(self) -> bool:
        return bool(self.blocked_sequence())

    def blocked_summary(self) -> list[dict[str, Any]]:
        return [
            {"index": i, "type": step.type, "status": step_status(step)}
            for i, step in enumerate(self.sequence)
            if step_status(step) != "ready"
        ]


def save_plan(plan: BroadcastPlan, path: str | Path) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dest


def load_plan(path: str | Path) -> BroadcastPlan:
    dest = Path(path)
    if not dest.exists():
        raise FileNotFoundError(f"plan 不存在: {dest}")
    raw = json.loads(dest.read_text(encoding="utf-8"))
    return BroadcastPlan.model_validate(raw)


__all__ = [
    "ALLOWED_TYPES",
    "BroadcastPlan",
    "ChannelStep",
    "BundleStep",
    "DEFAULT_CONTACT_GAP",
    "DEFAULT_MSG_GAP",
    "ImageStep",
    "NOT_IMPLEMENTED_TYPES",
    "OaStep",
    "PHASE_A_TYPES",
    "READY_TYPES",
    "SequenceStep",
    "TextStep",
    "VideoStep",
    "VoiceStep",
    "WxaStep",
    "load_plan",
    "save_plan",
    "step_status",
]
