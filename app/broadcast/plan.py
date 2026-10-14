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
READY_TYPES = frozenset({"voice", "text", "image", "oa", "doc", "channel", "video", "wxa", "bundle", "at", "notice"})
NOT_IMPLEMENTED_TYPES = frozenset()
ALLOWED_TYPES = READY_TYPES | NOT_IMPLEMENTED_TYPES

# Step 6 风控灰度后默认值（MuMu + 营销号）。
# 含义：在可接受上限速率基础上预留 20% 安全余量。
DEFAULT_MSG_GAP = (10.0, 30.0)       # 旧格式兼容常量（UI/测试引用）
DEFAULT_CONTACT_GAP = (60.0, 150.0)  # 旧格式兼容常量（UI/测试引用）
# 新模型的默认值
DEFAULT_STEP_GAP = 3.0   # 逐条间隔默认 3s
DEFAULT_CONTACT_GAP_FLAT = 1.0  # 联系人间隔默认 1s


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
    display_name: str = ""

    @field_validator("voice_id")
    @classmethod
    def _voice_id(cls, v: str) -> str:
        return _require_nonempty(v, "voice_id")

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, v: str) -> str:
        return v.strip()


class TextStep(BaseModel):
    type: Literal["text"]
    body: str = ""
    template_id: str = ""
    variant_pick: str = "random"
    # 仅用于界面展示。link 仍按文字发送。
    text_kind: str = "text"
    display_name: str = ""

    @field_validator("body", "template_id", "variant_pick", "text_kind", "display_name")
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


class AtStep(BaseModel):
    """群聊 @所有人，正文走和文字相同的 body / 文字模板。"""

    type: Literal["at"]
    body: str = ""
    template_id: str = ""
    variant_pick: str = "random"
    display_name: str = ""

    @field_validator("body", "template_id", "variant_pick", "display_name")
    @classmethod
    def _strip_fields(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _validate_text_source(self) -> "AtStep":
        if not self.body and not self.template_id:
            raise ValueError("at step 需提供 body 或 template_id")
        if self.template_id and not _is_valid_variant_pick(self.variant_pick):
            raise ValueError("variant_pick 仅支持 random 或 index:N")
        return self


class NoticeStep(BaseModel):
    """群公告。flag 固定为不额外 @所有人。"""

    type: Literal["notice"]
    body: str = ""
    template_id: str = ""
    variant_pick: str = "random"
    display_name: str = ""

    @field_validator("body", "template_id", "variant_pick", "display_name")
    @classmethod
    def _strip_fields(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _validate_text_source(self) -> "NoticeStep":
        if not self.body and not self.template_id:
            raise ValueError("notice step 需提供 body 或 template_id")
        if self.template_id and not _is_valid_variant_pick(self.variant_pick):
            raise ValueError("variant_pick 仅支持 random 或 index:N")
        return self


class ImageStep(BaseModel):
    type: Literal["image"]
    path: str
    display_name: str = ""

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _require_nonempty(v, "path")

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, v: str) -> str:
        return v.strip()


class OaStep(BaseModel):
    type: Literal["oa"]
    template_id: str
    display_name: str = ""

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, v: str) -> str:
        return _require_nonempty(v, "template_id")

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, v: str) -> str:
        return v.strip()


class WxaStep(BaseModel):
    type: Literal["wxa"]
    template_id: str
    thumb_path: str = ""
    display_name: str = ""

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, v: str) -> str:
        return _require_nonempty(v, "template_id")

    @field_validator("thumb_path", "display_name")
    @classmethod
    def _thumb(cls, v: str) -> str:
        return v.strip()


class VideoStep(BaseModel):
    type: Literal["video"]
    path: str
    thumb_path: str = ""
    duration: int | None = None
    display_name: str = ""

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _require_nonempty(v, "path")

    @field_validator("thumb_path", "display_name")
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


class DocStep(BaseModel):
    """企微文档卡片。发送仍走 LinkMessage，类型单独记账。"""

    type: Literal["doc"]
    template_id: str
    display_name: str = ""

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, v: str) -> str:
        return _require_nonempty(v, "template_id")

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, v: str) -> str:
        return v.strip()


class ChannelStep(BaseModel):
    """视频号卡片。发送仍走 LinkMessage，类型单独记账。"""

    type: Literal["channel"]
    template_id: str
    display_name: str = ""

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, v: str) -> str:
        return _require_nonempty(v, "template_id")

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, v: str) -> str:
        return v.strip()


LeafStep = Annotated[
    Union[VoiceStep, TextStep, AtStep, NoticeStep, ImageStep, OaStep, DocStep, WxaStep, VideoStep, ChannelStep],
    Field(discriminator="type"),
]


class BundleStep(BaseModel):
    """素材包。atomic 收成一个节点；expanded 的子步骤快照在 items 里。"""

    type: Literal["bundle"]
    bundle_id: str
    bundle_name: str = ""
    presentation: Literal["atomic", "expanded"] = "atomic"
    items: list[LeafStep] = Field(default_factory=list)
    item_gaps: list[float] = Field(default_factory=list)

    @field_validator("bundle_id")
    @classmethod
    def _bundle_id(cls, v: str) -> str:
        return _require_nonempty(v, "bundle_id")

    @field_validator("bundle_name")
    @classmethod
    def _bundle_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("item_gaps")
    @classmethod
    def _item_gaps(cls, v: list[float]) -> list[float]:
        return [max(3.0, float(x)) for x in v]

    @model_validator(mode="after")
    def _align_item_gaps(self) -> "BundleStep":
        expected = max(0, len(self.items) - 1)
        if len(self.item_gaps) < expected:
            self.item_gaps = list(self.item_gaps) + [DEFAULT_STEP_GAP] * (expected - len(self.item_gaps))
        elif len(self.item_gaps) > expected:
            self.item_gaps = self.item_gaps[:expected]
        return self


SequenceStep = Annotated[
    Union[VoiceStep, TextStep, AtStep, NoticeStep, ImageStep, OaStep, DocStep, WxaStep, VideoStep, ChannelStep, BundleStep],
    Field(discriminator="type"),
]


def step_status(step: SequenceStep) -> Literal["ready", "not_implemented"]:
    if step.type in READY_TYPES:
        return "ready"
    return "not_implemented"


class BroadcastPlan(BaseModel):
    contacts: list[PlanContact] = Field(..., min_length=1)
    sequence: list[SequenceStep] = Field(..., min_length=1)
    step_gaps: list[float] = Field(default_factory=list)
    contact_gap: float = DEFAULT_CONTACT_GAP_FLAT
    name: str = ""
    schedule_mode: str = ""
    schedule_label: str = ""

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
    def _compat_old_format(cls, data: Any) -> Any:
        """向后兼容：旧格式 msg_gap_seconds/contact_gap_seconds/perturb → 新格式。"""
        if not isinstance(data, dict):
            return data
        # 旧 msg_gap_seconds → step_gaps（如果 step_gaps 未提供）
        if "msg_gap_seconds" in data and "step_gaps" not in data:
            raw = data.pop("msg_gap_seconds")
            # 旧格式是 [lo, hi] 元组，取中间值作为每条固定间隔
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                avg = (float(raw[0]) + float(raw[1])) / 2.0
                seq = data.get("sequence", [])
                n = max(0, len(seq) - 1) if isinstance(seq, list) else 0
                data["step_gaps"] = [max(3.0, avg)] * n
        elif "msg_gap_seconds" in data:
            data.pop("msg_gap_seconds", None)
        # 旧 contact_gap_seconds → contact_gap（如果 contact_gap 未提供）
        if "contact_gap_seconds" in data and "contact_gap" not in data:
            raw = data.pop("contact_gap_seconds")
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                data["contact_gap"] = max(1.0, (float(raw[0]) + float(raw[1])) / 2.0)
        elif "contact_gap_seconds" in data:
            data.pop("contact_gap_seconds", None)
        # perturb 旧字段忽略
        data.pop("perturb", None)
        return data

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

    @field_validator("step_gaps")
    @classmethod
    def _step_gaps(cls, v: list[float]) -> list[float]:
        return [max(3.0, float(x)) for x in v]

    @field_validator("contact_gap")
    @classmethod
    def _contact_gap_val(cls, v: float) -> float:
        return max(1.0, float(v))

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return str(v or "").strip()

    @model_validator(mode="after")
    def _align_step_gaps(self) -> "BroadcastPlan":
        """保证 step_gaps 长度 = max(0, len(sequence)-1)，不足则补默认值。"""
        expected = max(0, len(self.sequence) - 1)
        if len(self.step_gaps) < expected:
            self.step_gaps = list(self.step_gaps) + [DEFAULT_STEP_GAP] * (expected - len(self.step_gaps))
        elif len(self.step_gaps) > expected:
            self.step_gaps = self.step_gaps[:expected]
        return self

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
    "AtStep",
    "BroadcastPlan",
    "ChannelStep",
    "BundleStep",
    "LeafStep",
    "DEFAULT_CONTACT_GAP",
    "DEFAULT_CONTACT_GAP_FLAT",
    "DEFAULT_MSG_GAP",
    "DEFAULT_STEP_GAP",
    "DocStep",
    "ImageStep",
    "NOT_IMPLEMENTED_TYPES",
    "NoticeStep",
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
