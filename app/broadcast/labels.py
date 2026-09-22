"""客户标签本地存储（Step 11 β 兜底分支）。"""

from __future__ import annotations

from pathlib import Path

from app.broadcast.account import resolve_account_id
from app.broadcast.paths import account_contacts_cache_path
from app.config import resolve_path
from app.security import get_secure_store

DEFAULT_LABELS_FILE_NAME = "customer_labels.json"
_SECURE_STORE = get_secure_store()


class LocalLabelStore:
    """维护 conv_local -> labels 的本地映射。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        account_id: str | None = None,
    ) -> None:
        if path is not None:
            self.path = resolve_path(path)
        else:
            aid = resolve_account_id(account_id)
            self.path = account_contacts_cache_path(aid) / DEFAULT_LABELS_FILE_NAME
        self._rows: dict[str, list[str]] = {}

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        account_id: str | None = None,
    ) -> "LocalLabelStore":
        store = cls(path=path, account_id=account_id)
        if not store.path.exists():
            return store
        raw = _SECURE_STORE.load_json(store.path, default={})
        if not isinstance(raw, dict):
            raise ValueError(f"labels 文件格式非法: {store.path}")
        for conv_local, labels in raw.items():
            key = str(conv_local).strip()
            if not key:
                continue
            if not isinstance(labels, list):
                continue
            store._rows[key] = _normalize_labels(labels)
        return store

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _SECURE_STORE.save_json(self.path, self._rows, encrypt=True)
        return self.path

    def set_labels(self, conv_local: str, labels: list[str]) -> list[str]:
        key = _normalize_conv(conv_local)
        value = _normalize_labels(labels)
        self._rows[key] = value
        return value

    def add_label(self, conv_local: str, label: str) -> list[str]:
        key = _normalize_conv(conv_local)
        current = list(self._rows.get(key, []))
        item = label.strip()
        if item and item not in current:
            current.append(item)
        self._rows[key] = current
        return current

    def remove_label(self, conv_local: str, label: str) -> list[str]:
        key = _normalize_conv(conv_local)
        item = label.strip()
        rows = [x for x in self._rows.get(key, []) if x != item]
        self._rows[key] = rows
        return rows

    def labels_for(self, conv_local: str) -> list[str]:
        return list(self._rows.get(_normalize_conv(conv_local), []))

    def remove_conv(self, conv_local: str) -> bool:
        key = _normalize_conv(conv_local)
        return self._rows.pop(key, None) is not None

    def rows(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._rows.items()}

    def resolve(self, tag: str) -> list[str]:
        key = tag.strip()
        if not key:
            raise ValueError("tag 不能为空")
        out: list[str] = []
        for conv_local, labels in self._rows.items():
            if any(key == label or key in label for label in labels):
                out.append(conv_local)
        if not out:
            raise LookupError(f"未找到标签: {key}")
        return out


def _normalize_conv(value: str) -> str:
    key = str(value).strip()
    if not key:
        raise ValueError("conv_local 不能为空")
    return key


def _normalize_labels(labels: list[str]) -> list[str]:
    out: list[str] = []
    for raw in labels:
        text = str(raw).strip()
        if not text or text in out:
            continue
        out.append(text)
    return out


__all__ = ["DEFAULT_LABELS_FILE_NAME", "LocalLabelStore"]
