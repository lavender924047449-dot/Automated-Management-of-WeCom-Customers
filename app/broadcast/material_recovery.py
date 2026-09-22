"""素材恢复/补齐：把历史账号里的素材按需复制到当前账号。"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.broadcast.account import list_accounts
from app.broadcast.paths import DEFAULT_ACCOUNT_ID, data_root_for, normalize_account_id
from app.security import get_secure_store

_SECURE_STORE = get_secure_store()
_MATERIAL_DIRS = ("image_templates", "video_templates", "oa_templates")


def _count_json_files(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for p in root.glob("*.json") if p.is_file())


def _voice_entry_count(index_path: Path) -> int:
    if not index_path.exists():
        return 0
    try:
        raw = _SECURE_STORE.load_json(index_path, default=[])
    except Exception:
        return 0
    return len(raw) if isinstance(raw, list) else 0


def _material_score(account_id: str) -> int:
    root = data_root_for(account_id)
    score = 0
    for subdir in _MATERIAL_DIRS:
        score += _count_json_files(root / subdir)
    score += _voice_entry_count(root / "voice_lib" / "voice_lib_index.json")
    return score


def _copy_json_dir_if_empty(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    if _count_json_files(dst) > 0:
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in src.glob("*.json"):
        target = dst / path.name
        if target.exists():
            continue
        shutil.copy2(path, target)
        copied += 1
    return copied


def choose_seed_account(target_account_id: str, preferred_source: str = "") -> str:
    target = normalize_account_id(target_account_id)
    preferred = normalize_account_id(preferred_source)
    if preferred and preferred != DEFAULT_ACCOUNT_ID and preferred != target and _material_score(preferred) > 0:
        return preferred
    best = ""
    best_score = 0
    for account_id in list_accounts():
        normalized = normalize_account_id(account_id)
        if normalized in {DEFAULT_ACCOUNT_ID, target}:
            continue
        score = _material_score(normalized)
        if score > best_score:
            best = normalized
            best_score = score
    return best


def hydrate_materials(target_account_id: str, *, preferred_source: str = "") -> dict[str, int | str]:
    """若目标账号素材为空，则从素材最完整的历史账号复制一份。

    仅在目标目录“为空”时复制，不覆盖已有素材。
    """

    target = normalize_account_id(target_account_id)
    source = choose_seed_account(target, preferred_source=preferred_source)
    if not source:
        return {"source": "", "voice_copied": 0, "template_copied": 0}

    src_root = data_root_for(source)
    dst_root = data_root_for(target)
    dst_root.mkdir(parents=True, exist_ok=True)

    template_copied = 0
    for subdir in _MATERIAL_DIRS:
        template_copied += _copy_json_dir_if_empty(src_root / subdir, dst_root / subdir)

    voice_copied = 0
    src_voice_dir = src_root / "voice_lib"
    dst_voice_dir = dst_root / "voice_lib"
    src_index = src_voice_dir / "voice_lib_index.json"
    dst_index = dst_voice_dir / "voice_lib_index.json"
    if src_index.exists() and _voice_entry_count(dst_index) == 0:
        dst_voice_dir.mkdir(parents=True, exist_ok=True)
        if not dst_index.exists():
            shutil.copy2(src_index, dst_index)
            voice_copied = 1
        src_text_templates = src_voice_dir / "text_templates.json"
        dst_text_templates = dst_voice_dir / "text_templates.json"
        if src_text_templates.exists() and not dst_text_templates.exists():
            shutil.copy2(src_text_templates, dst_text_templates)

    return {"source": source, "voice_copied": voice_copied, "template_copied": template_copied}


__all__ = ["choose_seed_account", "hydrate_materials"]
