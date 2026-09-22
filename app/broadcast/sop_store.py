"""SOP 模板存储与标签命中解析。"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

from app.broadcast.dto import ContactRowDTO, PlanContact
from app.broadcast.labels import LocalLabelStore
from app.broadcast.paths import account_plans_dir, normalize_account_id
from app.broadcast.plan import DEFAULT_CONTACT_GAP, DEFAULT_MSG_GAP
from app.security import get_secure_store

_SECURE_STORE = get_secure_store()
_SOP_FILE_NAME = "sop_templates.json"


def _normalize_gap(raw: Any, *, defaults: tuple[float, float], field: str) -> tuple[float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        lo = float(raw[0])
        hi = float(raw[1])
        if lo < 0 or hi < lo:
            raise ValueError(f"{field} 非法: [{lo}, {hi}]，要求 0 <= min <= max")
        return (lo, hi)
    return (float(defaults[0]), float(defaults[1]))


class SopTemplate(BaseModel):
    """SOP 模板：用于复用 sequence + 默认参数 + 标签筛选。"""

    id: str = ""
    name: str
    labels: list[str] = []
    sequence: list[dict[str, Any]]
    msg_gap_seconds: tuple[float, float] = DEFAULT_MSG_GAP
    contact_gap_seconds: tuple[float, float] = DEFAULT_CONTACT_GAP
    perturb: bool = True
    created_at: str = ""

    @field_validator("id", "name", "created_at")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("labels")
    @classmethod
    def _labels(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in values:
            text = str(raw).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    @field_validator("msg_gap_seconds", mode="before")
    @classmethod
    def _msg_gap(cls, value: Any) -> tuple[float, float]:
        return _normalize_gap(value, defaults=DEFAULT_MSG_GAP, field="msg_gap_seconds")

    @field_validator("contact_gap_seconds", mode="before")
    @classmethod
    def _contact_gap(cls, value: Any) -> tuple[float, float]:
        return _normalize_gap(value, defaults=DEFAULT_CONTACT_GAP, field="contact_gap_seconds")

    @model_validator(mode="after")
    def _finalize(self) -> "SopTemplate":
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.name:
            self.name = "未命名 SOP"
        if not self.sequence:
            raise ValueError("sequence 至少需要 1 个步骤")
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        return self


def sop_path(account_id: str | None) -> Path:
    return account_plans_dir(normalize_account_id(account_id)) / _SOP_FILE_NAME


def list_sops(account_id: str | None) -> list[SopTemplate]:
    path = sop_path(account_id)
    if not path.exists():
        return []
    raw = _SECURE_STORE.load_json(path, default=[])
    if not isinstance(raw, list):
        return []
    out: list[SopTemplate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(SopTemplate.model_validate(item))
    return sorted(out, key=lambda row: (row.created_at, row.name))


def load_sop(sop_id: str, *, account_id: str | None) -> SopTemplate:
    key = sop_id.strip()
    if not key:
        raise ValueError("sop_id 不能为空")
    for row in list_sops(account_id):
        if row.id == key:
            return row
    raise LookupError(f"SOP 不存在: {key}")


def save_sop(sop: SopTemplate | dict[str, Any], *, account_id: str | None) -> SopTemplate:
    row = sop if isinstance(sop, SopTemplate) else SopTemplate.model_validate(sop)
    rows = list_sops(account_id)
    replaced = False
    for idx, existing in enumerate(rows):
        if existing.id == row.id:
            rows[idx] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    payload = [item.model_dump(mode="json") for item in rows]
    _SECURE_STORE.save_json(sop_path(account_id), payload, encrypt=True)
    return row


def delete_sop(sop_id: str, *, account_id: str | None) -> bool:
    key = sop_id.strip()
    if not key:
        raise ValueError("sop_id 不能为空")
    rows = list_sops(account_id)
    next_rows = [row for row in rows if row.id != key]
    if len(next_rows) == len(rows):
        return False
    payload = [item.model_dump(mode="json") for item in next_rows]
    _SECURE_STORE.save_json(sop_path(account_id), payload, encrypt=True)
    return True


def resolve_contacts_by_labels(
    labels: list[str],
    contact_rows: list[dict[str, Any] | ContactRowDTO],
    *,
    account_id: str | None,
) -> list[PlanContact]:
    targets = {str(item).strip() for item in labels if str(item).strip()}
    if not targets:
        return []
    beta_rows = LocalLabelStore.load(account_id=account_id).rows()
    out: list[PlanContact] = []
    seen: set[str] = set()
    for raw in contact_rows:
        try:
            dto = raw if isinstance(raw, ContactRowDTO) else ContactRowDTO.model_validate(raw)
        except Exception:
            continue
        merged_labels: list[str] = []
        for label in [*dto.labels, *beta_rows.get(dto.conv_local, [])]:
            text = str(label).strip()
            if text and text not in merged_labels:
                merged_labels.append(text)
        if not (targets & set(merged_labels)):
            continue
        if dto.conv_local in seen:
            continue
        seen.add(dto.conv_local)
        out.append(PlanContact(conv_local=dto.conv_local, display_name=dto.display_name))
    return sorted(out, key=lambda row: row.display_name)


__all__ = [
    "SopTemplate",
    "delete_sop",
    "list_sops",
    "load_sop",
    "resolve_contacts_by_labels",
    "save_sop",
    "sop_path",
]
