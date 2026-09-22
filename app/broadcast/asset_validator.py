"""素材步骤轻量校验（per-step 静默 dispatch，不走 DryRun / Frida 发送）。

约定（与 runner 发送前检查对齐，刻意保持 <50ms / 10 步）：
- TextStep(template_id=...) → map 查找 + 变体索引校验（等同 runner._resolve_text_body）
- VoiceStep → silk 文件存在 + 头 4 字节 magic + duration>0
- ImageStep/VideoStep → Path.exists() + os.access(R_OK)
- OaStep → load_oa_template 能读回 JSON
- BundleStep → 展开后每个子 step 递归上面几条
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.broadcast.asset_bundles import get_bundle
from app.broadcast.image_templates import load_template as load_image_template
from app.broadcast.library import VoiceLibrary
from app.broadcast.oa_templates import load_template as load_oa_template
from app.broadcast.paths import VOICE_LIB_ROOT
from app.broadcast.video_templates import load_template as load_video_template

# Tencent SILK v3：常见头是 b"\x02#!SILK_V3" 或 b"#!SILK_V3"，只看前 4 字节。
_SILK_HEADS = (b"\x02#!", b"#!SI")


def validate_step_payload(payload: dict[str, Any], *, account_id: str) -> tuple[bool, str]:
    """校验单个 sequence step 是否可发送。返回 (ok, error_message)。"""
    step_type = str(payload.get("type", "")).strip()
    if step_type == "text":
        return _validate_text(payload, account_id=account_id)
    if step_type == "voice":
        return _validate_voice(payload, account_id=account_id)
    if step_type == "image":
        return _validate_image(payload)
    if step_type == "video":
        return _validate_video(payload)
    if step_type == "oa":
        return _validate_oa(payload, account_id=account_id)
    if step_type == "bundle":
        return _validate_bundle(payload, account_id=account_id)
    return True, ""


def _is_device_path(path: str) -> bool:
    return path.startswith("/sdcard/") or path.startswith("/storage/emulated/")


def _resolve_voice_check_path(file_field: str) -> str:
    """将 voice file 字段解析为用于轻量校验的路径字符串。

    语义与 BroadcastRunner._voice_path 对齐：
    - 绝对路径（/sdcard/... 或 Windows 盘符）保持原样
    - 相对路径拼到 VOICE_LIB_ROOT 下
    """
    path_text = str(file_field or "").strip()
    if not path_text:
        return ""
    if path_text.startswith("/") or (len(path_text) > 1 and path_text[1] == ":"):
        return path_text
    return f"{VOICE_LIB_ROOT.rstrip('/')}/{path_text.lstrip('/')}"


def _check_readable_file(path: str, *, label: str) -> tuple[bool, str]:
    text = str(path or "").strip()
    if not text:
        return False, f"{label} 为空"
    if _is_device_path(text):
        # 采集落盘在模拟器 /sdcard/...，发送走 adb；Windows 上 Path.exists 恒为 False。
        # 轻量校验只确认路径形态，不在这里打 adb（会变成几百 ms）。
        return True, ""
    fp = Path(text)
    if not fp.exists() or not fp.is_file():
        return False, f"{label} 不存在: {text}"
    if not os.access(fp, os.R_OK):
        return False, f"{label} 不可读: {text}"
    return True, ""


def _resolve_text_body(payload: dict[str, Any], *, account_id: str) -> tuple[str, str]:
    """与 BroadcastRunner._resolve_text_body 同语义：map 查找 + 变体索引。"""
    body = str(payload.get("body", "")).strip()
    template_id = str(payload.get("template_id", "")).strip()
    if not template_id:
        if not body:
            raise ValueError("text 步骤缺少 body 或 template_id")
        return body, "inline"
    lib = VoiceLibrary.load(account_id=account_id)
    template = lib.get_text_template(template_id)
    total = len(template.variants)
    if total < 1:
        raise ValueError("text template variants 不能为空")
    pick = str(payload.get("variant_pick", "random")).strip() or "random"
    if pick == "random":
        idx = 0
    elif pick.startswith("index:"):
        idx_text = pick.removeprefix("index:").strip()
        if not idx_text.isdigit():
            raise ValueError(f"非法 variant_pick: {pick}")
        idx = int(idx_text)
        if idx < 0 or idx >= total:
            raise ValueError(f"variant_pick 越界: {pick}")
    else:
        raise ValueError(f"非法 variant_pick: {pick}")
    resolved = str(template.variants[idx]).strip()
    if not resolved:
        raise ValueError("解析后的发送正文为空")
    return resolved, f"{template.id}#{idx}"


def _validate_text(payload: dict[str, Any], *, account_id: str) -> tuple[bool, str]:
    try:
        _resolve_text_body(payload, account_id=account_id)
    except Exception as exc:
        return False, str(exc)
    return True, ""


def _validate_voice(payload: dict[str, Any], *, account_id: str) -> tuple[bool, str]:
    voice_id = str(payload.get("voice_id", "")).strip()
    if not voice_id:
        return False, "voice_id 为空"
    lib = VoiceLibrary.load(account_id=account_id)
    entry = next((row for row in lib.list_active() if row.id == voice_id), None)
    if entry is None:
        return False, f"语音素材不存在或已归档: {voice_id}"
    if entry.duration < 1:
        return False, "语音 duration 无效"
    check_path = _resolve_voice_check_path(str(entry.file or ""))
    ok, err = _check_readable_file(check_path, label="语音文件")
    if not ok:
        return False, err
    if _is_device_path(check_path):
        return True, ""
    try:
        head = Path(check_path).read_bytes()[:4]
    except OSError as exc:
        return False, f"语音文件不可读: {exc}"
    if head not in _SILK_HEADS:
        return False, f"silk 头 magic 非法: {head!r}"
    return True, ""


def _validate_image(payload: dict[str, Any]) -> tuple[bool, str]:
    return _check_readable_file(str(payload.get("path", "")).strip(), label="图片 path")


def _validate_video(payload: dict[str, Any]) -> tuple[bool, str]:
    ok, err = _check_readable_file(str(payload.get("path", "")).strip(), label="视频 path")
    if not ok:
        return False, err
    thumb = str(payload.get("thumb_path", "")).strip()
    if thumb:
        return _check_readable_file(thumb, label="视频缩略图")
    return True, ""


def _validate_oa(payload: dict[str, Any], *, account_id: str) -> tuple[bool, str]:
    template_id = str(payload.get("template_id", "")).strip()
    if not template_id:
        return False, "oa template_id 为空"
    try:
        data = load_oa_template(template_id, account_id=account_id)
    except Exception as exc:
        return False, f"公众号模板加载失败: {exc}"
    if not isinstance(data, dict) or not data:
        return False, "公众号模板 JSON 为空"
    return True, ""


def _validate_bundle(payload: dict[str, Any], *, account_id: str) -> tuple[bool, str]:
    bundle_id = str(payload.get("bundle_id", "")).strip()
    if not bundle_id:
        return False, "bundle_id 为空"
    bundle = get_bundle(bundle_id, account_id=account_id)
    if bundle is None:
        return False, f"素材包不存在: {bundle_id}"
    if not bundle.items:
        return False, f"素材包为空: {bundle_id}"
    for item in bundle.items:
        asset_type = item.asset_type.strip().lower()
        item_id = item.item_id.strip()
        if asset_type == "voice":
            item_payload = {"type": "voice", "voice_id": item_id}
        elif asset_type == "text":
            item_payload = {"type": "text", "template_id": item_id, "variant_pick": "random"}
        elif asset_type == "oa":
            item_payload = {"type": "oa", "template_id": item_id}
        elif asset_type == "image":
            try:
                data = load_image_template(item_id, account_id=account_id)
            except Exception as exc:
                return False, f"素材包 image 加载失败: {exc}"
            path = str(data.get("path", "") or data.get("file_path", "")).strip()
            item_payload = {"type": "image", "path": path}
        elif asset_type == "video":
            try:
                data = load_video_template(item_id, account_id=account_id)
            except Exception as exc:
                return False, f"素材包 video 加载失败: {exc}"
            item_payload = {
                "type": "video",
                "path": str(data.get("path", "") or data.get("video_path", "")).strip(),
                "thumb_path": str(data.get("thumb_path", "")).strip(),
            }
        else:
            return False, f"素材包内暂不支持类型: {asset_type}"
        ok, err = validate_step_payload(item_payload, account_id=account_id)
        if not ok:
            return False, f"素材包子项失败 ({asset_type}/{item_id}): {err}"
    return True, ""


__all__ = ["validate_step_payload"]
