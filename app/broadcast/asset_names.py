"""同种类素材、素材包之间的重名检查，以及发送序列里过期步骤的识别。"""

from __future__ import annotations

from app.broadcast import image_templates as image_tpl_mod
from app.broadcast import oa_templates as oa_tpl_mod
from app.broadcast import video_templates as video_tpl_mod
from app.broadcast import wxa_templates as wxa_tpl_mod
from app.broadcast.asset_bundles import canon_asset_type, list_bundles
from app.broadcast.library import VoiceLibrary
from app.broadcast.link_cards import LINK_CARD_KINDS, card_kind_of

_TYPE_LABEL = {
    "text": "文字",
    "voice": "语音",
    "image": "图片",
    "video": "视频",
    "link": "文字",
    "wxa": "小程序",
    "oa": "公众号卡片",
    "doc": "企微文档",
    "channel": "视频号",
    "at": "@所有人",
    "notice": "群公告",
}

_TEMPLATE_LOADERS = {
    "image": (image_tpl_mod.list_templates, image_tpl_mod.load_template),
    "video": (video_tpl_mod.list_templates, video_tpl_mod.load_template),
    "oa": (oa_tpl_mod.list_templates, oa_tpl_mod.load_template),
    "wxa": (wxa_tpl_mod.list_templates, wxa_tpl_mod.load_template),
}


def _usable_name(name: str) -> str:
    text = str(name or "").strip()
    if not text or text.startswith("未命名 #"):
        return ""
    return text


def _named_assets(account_id: str, asset_type: str) -> list[tuple[str, str]]:
    """返回该种类下已命名条目的 (item_id, name)。未命名占位不参与。"""
    kind = canon_asset_type(asset_type)
    aid = account_id.strip() or "default"
    if kind == "voice":
        lib = VoiceLibrary.load(account_id=aid)
        out: list[tuple[str, str]] = []
        for item in lib.list():
            if not item.note.strip() and not item.label.strip():
                continue
            name = _usable_name(item.note.strip() or item.label.strip())
            if name:
                out.append((item.id, name))
        return out
    if kind in {"text", "link"}:
        lib = VoiceLibrary.load(account_id=aid)
        out = []
        for item in lib.list_text_templates(include_archived=True):
            item_kind = "link" if getattr(item, "kind", "text") == "link" else "text"
            if item_kind != kind or getattr(item, "unnamed", False):
                continue
            name = _usable_name(item.name)
            if name:
                out.append((item.id, name))
        return out
    if kind in LINK_CARD_KINDS:
        list_fn, load_fn = _TEMPLATE_LOADERS["oa"]
        return _template_names_from(list_fn, load_fn, aid, card_kind=kind)
    if kind in _TEMPLATE_LOADERS:
        list_fn, load_fn = _TEMPLATE_LOADERS[kind]
        return _template_names_from(list_fn, load_fn, aid)
    return []


def _template_names_from(
    list_fn,
    load_fn,
    account_id: str,
    *,
    card_kind: str = "",
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for fp in list_fn(account_id):
        try:
            payload = load_fn(str(fp), account_id=account_id)
        except Exception:
            continue
        if not isinstance(payload, dict) or payload.get("unnamed"):
            continue
        if card_kind and card_kind_of(payload) != card_kind:
            continue
        name = _usable_name(str(payload.get("name", "")))
        if not name:
            continue
        item_id = str(payload.get("template_id", "")).strip() or fp.stem
        out.append((item_id, name))
    return out


def asset_name_conflict(
    account_id: str,
    asset_type: str,
    name: str,
    *,
    exclude_item_id: str = "",
) -> str | None:
    """同一种类里已有相同名字时返回提示文案。不同种类、以及素材和素材包之间允许同名。"""
    target = _usable_name(name)
    if not target:
        return None
    kind = canon_asset_type(asset_type)
    exclude = str(exclude_item_id or "").strip()
    for item_id, existing in _named_assets(account_id, kind):
        if exclude and item_id == exclude:
            continue
        if existing == target:
            label = _TYPE_LABEL.get(kind, "素材")
            return f"已有同名{label}「{target}」，请换一个名字。"
    return None


def bundle_name_conflict(
    account_id: str,
    name: str,
    *,
    exclude_id: str = "",
) -> str | None:
    """素材包之间重名时返回提示文案。和素材同名是允许的。"""
    target = _usable_name(name)
    if not target:
        return None
    exclude = str(exclude_id or "").strip()
    for bundle in list_bundles(account_id):
        if exclude and bundle.id == exclude:
            continue
        if bundle.name.strip() == target:
            return f"已有同名素材包「{target}」，请换一个名字。"
    return None


def sequence_step_removed(
    payload: dict,
    removed_keys: set[tuple[str, str]],
    removed_paths: set[str],
    removed_bundle_ids: set[str],
) -> bool:
    """发送序列里的一步是否对应该删除的素材，或已被删掉的素材包节点。"""
    if not isinstance(payload, dict):
        return False
    if str(payload.get("_row_type", "")) == "gap":
        return False
    step_type = str(payload.get("type", "")).strip().lower()
    if step_type == "bundle":
        return str(payload.get("bundle_id", "")).strip() in removed_bundle_ids
    if step_type == "voice":
        voice_id = str(payload.get("voice_id", "")).strip()
        return bool(voice_id) and ("voice", voice_id) in removed_keys
    if step_type in {"text", "link", "at", "notice"}:
        template_id = str(payload.get("template_id", "")).strip()
        if not template_id:
            return False
        text_kind = str(payload.get("text_kind", step_type)).strip().lower()
        kind = "link" if text_kind == "link" or step_type == "link" else "text"
        return (kind, template_id) in removed_keys
    if step_type in LINK_CARD_KINDS:
        template_id = str(payload.get("template_id", "")).strip()
        return bool(template_id) and any((kind, template_id) in removed_keys for kind in LINK_CARD_KINDS)
    if step_type == "wxa":
        template_id = str(payload.get("template_id", "")).strip()
        return bool(template_id) and ("wxa", template_id) in removed_keys
    if step_type in {"image", "video"}:
        path = str(payload.get("path", "")).strip()
        return bool(path) and path in removed_paths
    return False
