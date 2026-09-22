"""语音素材库：JSON 索引读写与标签管理。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.broadcast.account import resolve_account_id
from app.broadcast.paths import account_voice_index_path
from app.config import resolve_path
from app.security import get_secure_store

_SECURE_STORE = get_secure_store()


class VoiceLibraryEntry(BaseModel):
    id: str
    file: str
    duration: int = Field(ge=1)
    captured_at: str
    note: str = ""
    label: str = ""
    usage_count: int = Field(default=0, ge=0)
    is_archived: bool = False

    @field_validator("id", "file", "captured_at", "note", "label")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        return v.strip()

    @field_validator("id")
    @classmethod
    def _id_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("id 不能为空")
        return v

    @field_validator("file")
    @classmethod
    def _file_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("file 不能为空")
        return v


class VoiceLibrary:
    """语音索引仓库。"""

    def __init__(
        self,
        index_path: str | Path | None = None,
        *,
        account_id: str | None = None,
    ) -> None:
        self.account_id = resolve_account_id(account_id)
        self.index_path = resolve_path(index_path) if index_path else account_voice_index_path(self.account_id)
        self._entries: list[VoiceLibraryEntry] = []
        self._text_templates: list[TextTemplate] = []

    @classmethod
    def load(
        cls,
        index_path: str | Path | None = None,
        *,
        account_id: str | None = None,
    ) -> "VoiceLibrary":
        lib = cls(index_path=index_path, account_id=account_id)
        if not lib.index_path.exists():
            lib._text_templates = lib._load_text_templates_file()
            return lib
        raw = _SECURE_STORE.load_json(lib.index_path, default=[])
        if not isinstance(raw, list):
            raise ValueError(f"voice index 不是数组: {lib.index_path}")
        lib._entries = [VoiceLibraryEntry.model_validate(item) for item in raw]
        lib._text_templates = lib._load_text_templates_file()
        return lib

    def save(self) -> Path:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [entry.model_dump(mode="json") for entry in self._entries]
        _SECURE_STORE.save_json(self.index_path, payload, encrypt=True)
        self._save_text_templates_file()
        return self.index_path

    def add(
        self,
        *,
        file: str,
        duration: int,
        note: str = "",
        label: str = "",
        captured_at: str | None = None,
        usage_count: int = 0,
    ) -> VoiceLibraryEntry:
        ts = captured_at or datetime.now().isoformat(timespec="seconds")
        entry = VoiceLibraryEntry(
            id=str(uuid.uuid4()),
            file=file,
            duration=duration,
            captured_at=ts,
            note=note,
            label=label,
            usage_count=usage_count,
        )
        self._entries.append(entry)
        return entry

    def list(self) -> list[VoiceLibraryEntry]:
        return list(self._entries)

    def list_active(self) -> list[VoiceLibraryEntry]:
        return [item for item in self._entries if not item.is_archived]

    def update_label(self, entry_id: str, label: str) -> VoiceLibraryEntry:
        entry = self._find(entry_id)
        entry.label = label.strip()
        return entry

    def update_note(self, entry_id: str, note: str) -> VoiceLibraryEntry:
        entry = self._find(entry_id)
        entry.note = note.strip()
        return entry

    def bump_usage(self, entry_id: str, delta: int = 1) -> VoiceLibraryEntry:
        entry = self._find(entry_id)
        entry.usage_count = max(0, entry.usage_count + int(delta))
        return entry

    def archive(self, entry_id: str) -> VoiceLibraryEntry:
        entry = self._find(entry_id)
        entry.is_archived = True
        return entry

    def delete(self, entry_id: str) -> VoiceLibraryEntry:
        entry = self._find(entry_id)
        self._entries = [item for item in self._entries if item.id != entry.id]
        return entry

    def unarchive(self, entry_id: str) -> VoiceLibraryEntry:
        entry = self._find(entry_id)
        entry.is_archived = False
        return entry

    def gc_unnamed(self, ttl: int = 1800, *, now: datetime | None = None) -> int:
        ttl_seconds = max(0, int(ttl))
        current = now or datetime.now()
        kept: list[VoiceLibraryEntry] = []
        removed = 0
        for entry in self._entries:
            if entry.label or entry.note:
                kept.append(entry)
                continue
            captured = _parse_ts(entry.captured_at)
            if captured is None:
                kept.append(entry)
                continue
            age = (current - captured).total_seconds()
            if age > ttl_seconds:
                removed += 1
                continue
            kept.append(entry)
        self._entries = kept
        return removed

    def list_text_templates(self, *, include_archived: bool = False) -> list["TextTemplate"]:
        if include_archived:
            return list(self._text_templates)
        return [item for item in self._text_templates if not item.is_archived]

    def add_text_template(
        self,
        *,
        name: str,
        variants: Iterable[str],
        tags: Iterable[str] | None = None,
        template_id: str | None = None,
        is_archived: bool = False,
        kind: Literal["text", "link"] = "text",
    ) -> "TextTemplate":
        tag_list = list(tags or [])
        template = TextTemplate(
            id=(template_id or str(uuid.uuid4())),
            name=name,
            variants=list(variants),
            tags=tag_list,
            is_archived=bool(is_archived),
            kind=kind,
        )
        self._text_templates.append(template)
        return template

    def add_link_template(
        self,
        *,
        name: str,
        url: str,
        tags: Iterable[str] | None = None,
        template_id: str | None = None,
    ) -> "TextTemplate":
        """便捷入口：把「小程序链接」当作 kind=link 的 TextTemplate 存起来。

        发送侧统一走 text 通道（url 会作为唯一 variant 发出）。
        """
        return self.add_text_template(
            name=name,
            variants=[url],
            tags=tags,
            template_id=template_id,
            kind="link",
        )

    def update_text_template(
        self,
        template_id: str,
        *,
        name: str | None = None,
        variants: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        kind: Literal["text", "link"] | None = None,
    ) -> "TextTemplate":
        template = self._find_text_template(template_id)
        if name is not None:
            template.name = _strip_nonempty(name, "name")
        if variants is not None:
            template.variants = _normalize_variants(variants)
        if tags is not None:
            template.tags = _normalize_tags(tags)
        if kind is not None:
            template.kind = kind
        template.updated_at = datetime.now().isoformat(timespec="seconds")
        return template

    def archive_text_template(self, template_id: str) -> "TextTemplate":
        template = self._find_text_template(template_id)
        template.is_archived = True
        template.updated_at = datetime.now().isoformat(timespec="seconds")
        return template

    def delete_text_template(self, template_id: str) -> "TextTemplate":
        template = self._find_text_template(template_id)
        self._text_templates = [item for item in self._text_templates if item.id != template.id]
        return template

    def unarchive_text_template(self, template_id: str) -> "TextTemplate":
        template = self._find_text_template(template_id)
        template.is_archived = False
        template.updated_at = datetime.now().isoformat(timespec="seconds")
        return template

    def get_text_template(self, template_id: str, *, include_archived: bool = False) -> "TextTemplate":
        template = self._find_text_template(template_id)
        if template.is_archived and not include_archived:
            raise KeyError(f"text template 已归档: {template_id!r}")
        return template

    def stats(self) -> dict[str, Any]:
        total = len(self._entries)
        total_duration = sum(item.duration for item in self._entries)
        labeled = sum(1 for item in self._entries if item.label)
        with_note = sum(1 for item in self._entries if item.note)
        archived = sum(1 for item in self._entries if item.is_archived)
        return {
            "total": total,
            "total_duration_seconds": total_duration,
            "labeled": labeled,
            "with_note": with_note,
            "archived": archived,
            "active": total - archived,
            "unlabeled": total - labeled,
        }

    def _find(self, entry_id: str) -> VoiceLibraryEntry:
        key = entry_id.strip()
        for entry in self._entries:
            if entry.id == key:
                return entry
        raise KeyError(f"voice entry 不存在: {entry_id!r}")

    @property
    def text_templates_path(self) -> Path:
        return self.index_path.parent / "text_templates.json"

    def _load_text_templates_file(self) -> list["TextTemplate"]:
        if not self.text_templates_path.exists():
            return []
        raw = _SECURE_STORE.load_json(self.text_templates_path, default=[])
        if not isinstance(raw, list):
            raise ValueError(f"text template index 不是数组: {self.text_templates_path}")
        return [TextTemplate.model_validate(item) for item in raw]

    def _save_text_templates_file(self) -> Path:
        self.text_templates_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [item.model_dump(mode="json") for item in self._text_templates]
        _SECURE_STORE.save_json(self.text_templates_path, payload, encrypt=True)
        return self.text_templates_path

    def _find_text_template(self, template_id: str) -> "TextTemplate":
        key = template_id.strip()
        for template in self._text_templates:
            if template.id == key:
                return template
        raise KeyError(f"text template 不存在: {template_id!r}")


def _strip_nonempty(value: str, field: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def _normalize_variants(values: Iterable[str]) -> list[str]:
    out = []
    for raw in values:
        text = str(raw).strip()
        if text:
            out.append(text)
    if not out:
        raise ValueError("variants 至少要 1 条非空文本")
    return out


def _normalize_tags(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _parse_ts(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class TextTemplate(BaseModel):
    id: str
    name: str
    variants: list[str]
    tags: list[str] = Field(default_factory=list)
    is_archived: bool = False
    # kind 用于把「小程序链接」这种本质是文字的素材在 GUI 上单独归类。
    # 默认 "text" 保证既有 text_templates.json（无该字段）向前兼容：
    # Pydantic 反序列化老数据时自动补 "text"。
    kind: Literal["text", "link"] = "text"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @field_validator("id", "name")
    @classmethod
    def _strip_nonempty_field(cls, v: str, info) -> str:
        return _strip_nonempty(v, str(info.field_name))

    @field_validator("variants")
    @classmethod
    def _variants(cls, v: list[str]) -> list[str]:
        return _normalize_variants(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _normalize_tags(v)


__all__ = ["TextTemplate", "VoiceLibrary", "VoiceLibraryEntry"]
