"""GUI 文案与素材类型展示映射（内部仍用英文 type 码）。"""

from __future__ import annotations

APP_TITLE = "企微客户自动化管理工具"

ASSET_TYPE_ZH: dict[str, str] = {
    "text": "文字",
    "voice": "语音",
    "image": "图片",
    "video": "视频",
    "wxa": "小程序",
    "oa": "公众号卡片",
    "doc": "企微文档",
    "channel": "视频号",
    "bundle": "素材包",
}

STEP_TYPE_ZH: dict[str, str] = {
    "at": "@所有人",
    "notice": "群公告",
}

ASSET_TYPE_FILTER_OPTIONS: list[str] = ["全部", *ASSET_TYPE_ZH.values()]

_ZH_TO_TYPE: dict[str, str] = {label: key for key, label in ASSET_TYPE_ZH.items()}

CHOICE_BUTTON_STYLE = (
    "QPushButton { padding: 2px 10px; border-radius: 10px; border: 1px solid #94a3b8; color: #334155; }"
    "QPushButton:checked { border: 1px solid #2563EB; color: #2563EB; background: #EFF6FF; }"
)

PROBE_LISTENER_LABELS = {
    "fta": "语音捕获",
    "oa": "公众号卡片",
    "wxa": "小程序",
}


def asset_type_to_zh(asset_type: str) -> str:
    key = str(asset_type).strip().lower()
    if key == "link":
        return ASSET_TYPE_ZH["text"]
    if key in STEP_TYPE_ZH:
        return STEP_TYPE_ZH[key]
    return ASSET_TYPE_ZH.get(key, str(asset_type))


def asset_type_from_filter(label: str) -> str:
    text = str(label).strip()
    if not text or text == "全部":
        return ""
    return _ZH_TO_TYPE.get(text, text)


def probe_listener_text(kind: str, *, running: bool, pid: int | None = None) -> str:
    prefix = PROBE_LISTENER_LABELS.get(kind, kind)
    if running and pid is not None:
        return f"{prefix}监听：开（PID {pid}）"
    return f"{prefix}监听：{'开' if running else '关'}"
