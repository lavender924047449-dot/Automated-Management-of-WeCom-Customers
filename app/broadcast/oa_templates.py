"""公众号图文卡片模板仓库（Step 7，A 方案：按账号隔离 + 待确认队列）。

目录布局（每账号一份）：
    data/<account_id>/oa_templates/
        <template_id>.json          # 已确认落库的模板（供 Runner 直接使用）
        _pending/
            <template_id>.json      # 探针刚捕获、等待 GUI 确认命名的候选

任何"直接由探针写入"的操作都必须走 `pending=True`，走 `_pending/`；
只有 GUI 弹窗里用户点『保存』后才通过 `commit_pending()` 提升到 `template_root()`。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.broadcast.account import resolve_account_id
from app.broadcast.paths import data_root_for
from app.config import resolve_path
from app.security import get_secure_store

OA_TEMPLATE_SUBDIR = "oa_templates"
PENDING_SUBDIR = "_pending"
LEGACY_OA_TEMPLATE_DIR = "app/broadcast/oa_templates"

_SECURE_STORE = get_secure_store()


def template_root(account_id: str | None = None) -> Path:
    aid = resolve_account_id(account_id)
    root = data_root_for(aid) / OA_TEMPLATE_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def pending_root(account_id: str | None = None) -> Path:
    root = template_root(account_id) / PENDING_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_tags(raw: Any) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for x in raw:
            t = str(x).strip()
            if not t or t in seen:
                continue
            seen.add(t)
            tags.append(t)
    return tags


def _prepare_payload(payload: dict[str, Any], template_id: str | None) -> dict[str, Any]:
    tid = (template_id or payload.get("template_id") or str(uuid.uuid4())).strip()
    if not tid:
        raise ValueError("template_id 不能为空")
    body = dict(payload)
    body["template_id"] = tid
    body.setdefault("captured_at", datetime.now().isoformat(timespec="seconds"))
    body["name"] = str(body.get("name", "")).strip() or tid
    body["tags"] = _normalize_tags(body.get("tags", []))
    return body


def save_template(
    payload: dict[str, Any],
    *,
    template_id: str | None = None,
    account_id: str | None = None,
    pending: bool = False,
) -> Path:
    """写入模板。默认 `pending=False`（GUI commit 使用）；
    探针必须显式传 `pending=True` 走待确认队列。"""
    body = _prepare_payload(payload, template_id)
    tid = body["template_id"]
    root = pending_root(account_id) if pending else template_root(account_id)
    out = root / f"{tid}.json"
    _SECURE_STORE.save_json(out, body, encrypt=True)
    return out


def load_template(
    template_ref: str,
    *,
    account_id: str | None = None,
) -> dict[str, Any]:
    ref = template_ref.strip()
    if not ref:
        raise ValueError("template 不能为空")
    p = Path(ref)
    if p.suffix.lower() == ".json":
        fp = resolve_path(p)
    else:
        # 先在当前账号目录（含 pending）找，再退回 legacy 全局目录
        acct_dir = template_root(account_id)
        for candidate in (acct_dir / f"{ref}.json", acct_dir / PENDING_SUBDIR / f"{ref}.json",
                          resolve_path(Path(LEGACY_OA_TEMPLATE_DIR)) / f"{ref}.json"):
            if candidate.exists():
                fp = candidate
                break
        else:
            raise FileNotFoundError(f"oa template 不存在: {ref}")
    if not fp.exists():
        raise FileNotFoundError(f"oa template 不存在: {fp}")
    payload = _SECURE_STORE.load_json(fp, default={})
    if not isinstance(payload, dict):
        raise ValueError(f"oa template 结构非法: {fp}")
    return payload


def list_templates(account_id: str | None = None) -> list[Path]:
    """已确认的模板列表（不含 pending）。"""
    root = template_root(account_id)
    return sorted(p for p in root.glob("*.json") if p.parent == root)


def list_pending(account_id: str | None = None) -> list[Path]:
    root = pending_root(account_id)
    return sorted(root.glob("*.json"))


def commit_pending(
    pending_path: str | Path,
    *,
    name: str,
    tags: list[str] | None = None,
    account_id: str | None = None,
    template_id: str | None = None,
) -> Path:
    """把 `_pending/*.json` 提升到 `template_root()`，附上用户命名/标签。"""
    src = Path(pending_path)
    if not src.exists():
        raise FileNotFoundError(f"pending 模板不存在: {src}")
    payload = _SECURE_STORE.load_json(src, default={})
    if not isinstance(payload, dict):
        raise ValueError(f"pending 模板结构非法: {src}")
    payload["name"] = (name or "").strip() or str(payload.get("name", "")).strip() or src.stem
    payload["tags"] = _normalize_tags(tags or payload.get("tags", []))
    tid = (template_id or str(payload.get("template_id") or src.stem)).strip()
    payload["template_id"] = tid
    root = template_root(account_id)
    out = root / f"{tid}.json"
    _SECURE_STORE.save_json(out, payload, encrypt=True)
    src.unlink(missing_ok=True)
    return out


def discard_pending(pending_path: str | Path) -> None:
    Path(pending_path).unlink(missing_ok=True)


__all__ = [
    "OA_TEMPLATE_SUBDIR",
    "PENDING_SUBDIR",
    "LEGACY_OA_TEMPLATE_DIR",
    "template_root",
    "pending_root",
    "save_template",
    "load_template",
    "list_templates",
    "list_pending",
    "commit_pending",
    "discard_pending",
]
