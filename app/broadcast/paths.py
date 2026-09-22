"""广播模块路径常量与多账号命名空间。"""

from __future__ import annotations

from pathlib import Path

from app.config import resolve_path

# Android 设备侧素材目录（Step 1 验证通过，保持不变）。
VOICE_LIB_ROOT = "/sdcard/Test-Voice/voice_lib"
VOICE_LIB_ROOT_FALLBACK = (
    "/storage/emulated/0/Android/data/com.tencent.wework/files/Test-Voice/voice_lib"
)

# 主机侧数据命名（Step 12）。
DEFAULT_ACCOUNT_ID = "default"
DATA_DIR_NAME = "data"
VOICE_INDEX_FILE = "voice_lib_index.json"
CONTACTS_CACHE_FILE = "contacts_cache.json"
PLANS_DIR_NAME = "plans"
REPORTS_DIR_NAME = "reports"


def normalize_account_id(account_id: str | None) -> str:
    raw = str(account_id or "").strip()
    if not raw:
        return DEFAULT_ACCOUNT_ID
    keep = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_", "."}:
            keep.append(ch)
        else:
            keep.append("_")
    text = "".join(keep).strip("._")
    return text or DEFAULT_ACCOUNT_ID


def data_root_for(account_id: str | None) -> Path:
    aid = normalize_account_id(account_id)
    return resolve_path(Path(DATA_DIR_NAME) / aid)


def account_voice_index_path(account_id: str | None) -> Path:
    aid = normalize_account_id(account_id)
    target = data_root_for(aid) / "voice_lib" / VOICE_INDEX_FILE
    legacy = resolve_path(Path("runtime") / VOICE_INDEX_FILE)
    if aid == DEFAULT_ACCOUNT_ID and legacy.exists() and not target.exists():
        return legacy
    return target


def account_contacts_cache_path(account_id: str | None) -> Path:
    return data_root_for(account_id) / "contacts_cache" / CONTACTS_CACHE_FILE


def account_plans_dir(account_id: str | None) -> Path:
    return data_root_for(account_id) / PLANS_DIR_NAME


def account_reports_dir(account_id: str | None) -> Path:
    return data_root_for(account_id) / REPORTS_DIR_NAME


__all__ = [
    "VOICE_LIB_ROOT",
    "VOICE_LIB_ROOT_FALLBACK",
    "DEFAULT_ACCOUNT_ID",
    "VOICE_INDEX_FILE",
    "CONTACTS_CACHE_FILE",
    "PLANS_DIR_NAME",
    "REPORTS_DIR_NAME",
    "normalize_account_id",
    "data_root_for",
    "account_voice_index_path",
    "account_contacts_cache_path",
    "account_plans_dir",
    "account_reports_dir",
]
