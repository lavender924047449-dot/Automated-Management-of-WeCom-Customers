"""按素材库里的那一条素材组装预览数据。

发送列表、素材包、计划序列都只拿类型和编号来这里取预览，
这样同一条素材出现在哪里，双击看到的都是素材库里的那一份。
"""

from __future__ import annotations

from typing import Any


def usable_asset_name(name: str, item_id: str = "") -> str:
    text = str(name or "").strip()
    key = str(item_id or "").strip()
    if not text or text == key:
        return ""
    if text in {"未命名", "未命名素材包", "未命名计划"} or text.startswith("未命名 #"):
        return ""
    return text


def lookup_asset_name(
    account_id: str,
    asset_type: str,
    *,
    item_id: str = "",
    path: str = "",
) -> str:
    payload = asset_preview_payload(
        account_id,
        {
            "type": asset_type,
            "item_id": item_id,
            "template_id": item_id,
            "voice_id": item_id,
            "path": path,
        },
    )
    return usable_asset_name(str(payload.get("name") or ""), item_id)


def asset_preview_payload(
    account_id: str,
    step: dict[str, Any] | None,
    *,
    name: str = "",
) -> dict[str, Any]:
    """返回和「已保存的素材」同一套预览字段。找不到素材时尽量用步骤上已有的内容。"""
    raw = dict(step or {})
    kind = str(raw.get("type") or raw.get("asset_type") or "").strip().lower()
    if kind == "link":
        kind = "text"
    item_id = str(raw.get("item_id") or raw.get("template_id") or raw.get("voice_id") or "").strip()
    explicit = usable_asset_name(name, item_id) or usable_asset_name(str(raw.get("display_name") or ""), item_id)
    aid = str(account_id or "").strip() or "default"
    if kind == "voice":
        voice_id = str(raw.get("voice_id") or item_id).strip()
        return _voice_payload(aid, voice_id, explicit)
    if kind in {"text", "at", "notice"}:
        template_id = str(raw.get("template_id") or item_id).strip()
        payload = _text_payload(aid, template_id, explicit, str(raw.get("body") or ""))
        payload["asset_type"] = kind
        return payload
    if kind in {"image", "video"}:
        return _media_payload(
            aid,
            kind,
            str(raw.get("template_id") or item_id).strip(),
            str(raw.get("path") or "").strip(),
            str(raw.get("thumb_path") or "").strip(),
            explicit,
        )
    if kind in {"oa", "doc", "channel"}:
        template_id = str(raw.get("template_id") or item_id).strip()
        return _card_payload(aid, kind, template_id, explicit)
    if kind == "wxa":
        template_id = str(raw.get("template_id") or item_id).strip()
        return _wxa_payload(aid, template_id, explicit, str(raw.get("thumb_path") or "").strip())
    if not kind:
        return {}
    return {
        "asset_type": kind,
        "item_id": item_id,
        "name": explicit or "未命名",
    }


def _voice_payload(account_id: str, item_id: str, explicit: str) -> dict[str, Any]:
    file_path = ""
    library_name = ""
    if item_id:
        try:
            from app.broadcast.library import VoiceLibrary

            for row in VoiceLibrary.load(account_id=account_id).list():
                if row.id != item_id:
                    continue
                library_name = usable_asset_name(row.note or row.label or "", item_id)
                file_path = str(row.file or "").strip()
                break
        except Exception:
            library_name = ""
    return {
        "asset_type": "voice",
        "item_id": item_id,
        "name": explicit or library_name or "未命名",
        "file": file_path,
        "path": file_path,
    }


def _text_payload(account_id: str, item_id: str, explicit: str, body: str) -> dict[str, Any]:
    variants: list[str] = []
    library_name = ""
    if item_id:
        try:
            from app.broadcast.library import VoiceLibrary

            template = VoiceLibrary.load(account_id=account_id).get_text_template(item_id)
            library_name = usable_asset_name(str(template.name or ""), item_id)
            variants = [str(item).strip() for item in template.variants if str(item).strip()]
        except Exception:
            library_name = ""
    if not variants and body.strip():
        variants = [body.strip()]
    return {
        "asset_type": "text",
        "item_id": item_id,
        "name": explicit or library_name or (variants[0] if variants else "") or "未命名",
        "variants": variants,
    }


def _media_payload(
    account_id: str,
    kind: str,
    item_id: str,
    path: str,
    thumb_path: str,
    explicit: str,
) -> dict[str, Any]:
    loaded = _load_media(account_id, kind, item_id, path)
    found_id = str(loaded.get("template_id") or item_id).strip()
    found_path = str(loaded.get("path") or loaded.get("video_path") or path).strip()
    found_thumb = str(loaded.get("thumb_path") or thumb_path).strip()
    library_name = ""
    if loaded and not loaded.get("unnamed"):
        library_name = usable_asset_name(str(loaded.get("name") or ""), found_id)
    return {
        "asset_type": kind,
        "item_id": found_id,
        "name": explicit or library_name or "未命名",
        "path": found_path,
        "thumb_path": found_thumb,
    }


def _card_payload(account_id: str, kind: str, item_id: str, explicit: str) -> dict[str, Any]:
    loaded = _load_oa(account_id, item_id)
    from app.broadcast.link_cards import card_kind_of

    stored_kind = str(loaded.get("card_kind") or "").strip().lower()
    if stored_kind in {"oa", "doc", "channel"}:
        display_kind = stored_kind
    elif loaded and card_kind_of(loaded) != "oa":
        display_kind = card_kind_of(loaded)
    else:
        display_kind = kind or "oa"
    found_id = str(loaded.get("template_id") or item_id).strip()
    library_name = ""
    if loaded and not loaded.get("unnamed"):
        library_name = usable_asset_name(str(loaded.get("name") or loaded.get("title") or ""), found_id)
    title = str(loaded.get("title") or "").strip()
    return {
        "asset_type": display_kind or kind,
        "item_id": found_id,
        "name": explicit or library_name or "未命名",
        "oa_title": title or explicit or library_name,
        "oa_desc": str(loaded.get("desc") or loaded.get("digest") or "").strip(),
        "oa_source": str(loaded.get("sourcename") or loaded.get("appname") or "").strip(),
        "oa_url": str(loaded.get("url") or "").strip(),
    }


def _wxa_payload(account_id: str, item_id: str, explicit: str, thumb_path: str) -> dict[str, Any]:
    loaded = _load_wxa(account_id, item_id)
    found_id = str(loaded.get("template_id") or item_id).strip()
    library_name = ""
    if loaded and not loaded.get("unnamed"):
        library_name = usable_asset_name(str(loaded.get("name") or loaded.get("title") or ""), found_id)
    return {
        "asset_type": "wxa",
        "item_id": found_id,
        "name": explicit or library_name or "未命名",
        "wxa_title": str(loaded.get("title") or "").strip() or explicit or library_name,
        "wxa_app": str(loaded.get("appName") or "").strip(),
        "wxa_user": str(loaded.get("username") or "").strip(),
        "thumb_path": str(loaded.get("thumb_path") or thumb_path).strip(),
    }


def _load_oa(account_id: str, item_id: str) -> dict[str, Any]:
    if not item_id:
        return {}
    try:
        from app.broadcast.oa_templates import load_template

        loaded = load_template(item_id, account_id=account_id)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_wxa(account_id: str, item_id: str) -> dict[str, Any]:
    if not item_id:
        return {}
    try:
        from app.broadcast.wxa_templates import load_template

        loaded = load_template(item_id, account_id=account_id)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_media(account_id: str, kind: str, item_id: str, path: str) -> dict[str, Any]:
    if kind == "image":
        from app.broadcast import image_templates as module
    elif kind == "video":
        from app.broadcast import video_templates as module
    else:
        return {}
    if item_id:
        try:
            loaded = module.load_template(item_id, account_id=account_id)
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            return loaded
    target = path.strip()
    if not target:
        return {}
    try:
        files = module.list_templates(account_id)
    except Exception:
        return {}
    for fp in files:
        try:
            loaded = module.load_template(str(fp), account_id=account_id)
        except Exception:
            continue
        if not isinstance(loaded, dict):
            continue
        stored = str(loaded.get("path") or loaded.get("video_path") or "").strip()
        if stored == target:
            return loaded
    return {}
