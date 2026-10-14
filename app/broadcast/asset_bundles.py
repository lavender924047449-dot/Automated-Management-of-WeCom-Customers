"""素材包存储：按账号隔离。"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

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
    step_gaps: list[float] = Field(default_factory=list)
    contact_gap: float = 60.0
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

    @field_validator("contact_gap")
    @classmethod
    def _contact_gap(cls, v: float) -> float:
        return max(3.0, float(v))

    @field_validator("step_gaps")
    @classmethod
    def _step_gaps(cls, v: list[float]) -> list[float]:
        return [max(3.0, float(x)) for x in v]

    @model_validator(mode="after")
    def _align_step_gaps(self) -> "AssetBundle":
        """保证 step_gaps 长度 = max(0, len(items)-1)。"""
        expected = max(0, len(self.items) - 1)
        if len(self.step_gaps) < expected:
            self.step_gaps = list(self.step_gaps) + [10.0] * (expected - len(self.step_gaps))
        elif len(self.step_gaps) > expected:
            self.step_gaps = self.step_gaps[:expected]
        return self


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


def canon_asset_type(asset_type: str) -> str:
    """素材类型比较用的规范名。小程序卡片是 wxa，公众号卡片是 oa。"""
    return str(asset_type or "").strip().lower()


def _rewrite_bundle_items(
    account_id: str | None,
    keep_item,
) -> tuple[list[tuple[str, str]], list[str]]:
    """按 keep_item 过滤每个素材包。被拿空的包直接删除。

    返回 (被移除的 (type, item_id), 被删除的素材包 id)。没有变化时不写盘。
    """
    removed_items: list[tuple[str, str]] = []
    removed_bundle_ids: list[str] = []
    rewritten: list[AssetBundle] = []
    changed = False
    for bundle in list_bundles(account_id):
        kept: list[BundleItem] = []
        for item in bundle.items:
            if keep_item(item):
                kept.append(item)
                continue
            removed_items.append((canon_asset_type(item.asset_type), item.item_id.strip()))
        if len(kept) == len(bundle.items):
            rewritten.append(bundle)
            continue
        changed = True
        if not kept:
            removed_bundle_ids.append(bundle.id)
            continue
        payload = bundle.model_dump(mode="json")
        payload["items"] = [row.model_dump(mode="json") for row in kept]
        rewritten.append(AssetBundle.model_validate(payload))
    if changed:
        _persist_bundles(rewritten, account_id=account_id)
    return removed_items, removed_bundle_ids


def prune_missing_items(
    existing: set[tuple[str, str]] | list[tuple[str, str]],
    *,
    account_id: str | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """去掉素材包里已经不在素材库中的条目。包被拿空则整包删除。"""
    keys = {
        (canon_asset_type(asset_type), str(item_id).strip())
        for asset_type, item_id in existing
        if str(item_id).strip()
    }

    def _keep(item: BundleItem) -> bool:
        return (canon_asset_type(item.asset_type), item.item_id.strip()) in keys

    return _rewrite_bundle_items(account_id, _keep)


def remove_referenced_items(
    deleted: list[tuple[str, str]] | set[tuple[str, str]],
    *,
    account_id: str | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """从所有素材包中移除指定素材。包被拿空则整包删除。"""
    keys = {
        (canon_asset_type(asset_type), str(item_id).strip())
        for asset_type, item_id, *_rest in deleted
        if str(item_id).strip()
    }

    def _keep(item: BundleItem) -> bool:
        return (canon_asset_type(item.asset_type), item.item_id.strip()) not in keys

    return _rewrite_bundle_items(account_id, _keep)


__all__ = [
    "AssetBundle",
    "BundleItem",
    "bundle_store_path",
    "list_bundles",
    "save_bundle",
    "delete_bundle",
    "get_bundle",
    "canon_asset_type",
    "prune_missing_items",
    "remove_referenced_items",
]
