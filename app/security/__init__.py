"""安全存储模块。"""

from __future__ import annotations

from app.security.dpapi_store import DpapiStore, DpapiUnavailableError
from app.security.encrypted_json_store import EncryptedJsonStore

_STORE_SINGLETON: EncryptedJsonStore | None = None


def get_secure_store() -> EncryptedJsonStore:
    """返回进程级加密存储单例。"""

    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        _STORE_SINGLETON = EncryptedJsonStore()
    return _STORE_SINGLETON


__all__ = ["DpapiStore", "DpapiUnavailableError", "EncryptedJsonStore", "get_secure_store"]
