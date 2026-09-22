"""素材包存储：按账号隔离。"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from app.broadcast.account import resolve_account_id
from app.broadcast.paths import data_root_for
from app.security import get_secure_store

_SECURE_STORE = get_secure_store()
_BUNDLE_FILE = "asset_bundles.json"


class BundleItem(BaseModel):
    asset_type: str
    item_id: str

    @field_validator("asset_type", "item_id")
    @classmethod
    def _strip_nonempty(cls, value: str, info) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{info.field_name} 不能为空")
        return text


class AssetBundle(BaseModel):
    id: str = Field(default_factory=lambda: f"b_{uuid.uuid4().hex[:12]}")
    name: str
    items: list[BundleItem] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @field_validator("id", "name")
    @classmethod
    def _nonempty(cls, value: str, info) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{info.field_name} 不能为空")
        return text

    @field_validator("tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in values:
            text = str(raw).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out


def bundle_store_path(account_id: str | None = None) -> Path:
    aid = resolve_account_id(account_id)
    root = data_root_for(aid) / "broadcast"
    root.mkdir(parents=True, exist_ok=True)
    return root / _BUNDLE_FILE


def list_bundles(account_id: str | None = None) -> list[AssetBundle]:
    fp = bundle_store_path(account_id)
    if not fp.exists():
        return []
    raw = _SECURE_STORE.load_json(fp, default=[])
    if not isinstance(raw, list):
        raise ValueError(f"素材包索引结构非法: {fp}")
    out: list[AssetBundle] = []
    for row in raw:
        out.append(AssetBundle.model_validate(row))
    return out


def save_bundle(bundle: AssetBundle, *, account_id: str | None = None) -> AssetBundle:
    bundles = list_bundles(account_id)
    now = datetime.now().isoformat(timespec="seconds")
    saved = bundle.model_copy(update={"updated_at": now})
    exists = False
    for i, row in enumerate(bundles):
        if row.id == saved.id:
            bundles[i] = saved
            exists = True
            break
    if not exists:
        saved = saved.model_copy(update={"created_at": now, "updated_at": now})
        bundles.append(saved)
    _persist_bundles(bundles, account_id=account_id)
    return saved


def delete_bundle(bundle_id: str, *, account_id: str | None = None) -> bool:
    key = str(bundle_id).strip()
    if not key:
        return False
    bundles = list_bundles(account_id)
    kept = [row for row in bundles if row.id != key]
    if len(kept) == len(bundles):
        return False
    _persist_bundles(kept, account_id=account_id)
    return True


def get_bundle(bundle_id: str, *, account_id: str | None = None) -> AssetBundle | None:
    key = str(bundle_id).strip()
    for row in list_bundles(account_id):
        if row.id == key:
            return row
    return None


def _persist_bundles(rows: list[AssetBundle], *, account_id: str | None = None) -> Path:
    fp = bundle_store_path(account_id)
    payload = [row.model_dump(mode="json") for row in rows]
    _SECURE_STORE.save_json(fp, payload, encrypt=True)
    return fp


__all__ = [
    "AssetBundle",
    "BundleItem",
    "bundle_store_path",
    "list_bundles",
    "save_bundle",
    "delete_bundle",
    "get_bundle",
]
