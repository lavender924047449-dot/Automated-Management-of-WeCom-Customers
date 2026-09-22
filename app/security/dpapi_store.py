"""Windows DPAPI 封装：字节级加解密。"""

from __future__ import annotations

import base64
from dataclasses import dataclass


class DpapiUnavailableError(RuntimeError):
    """当前环境不可用 DPAPI。"""


@dataclass
class DpapiStore:
    """基于 `win32crypt` 的 DPAPI 封装。"""

    description: str = "Test-Voice Sensitive Store"
    entropy: bytes | None = None

    @staticmethod
    def is_available() -> bool:
        try:
            import win32crypt  # type: ignore  # noqa: F401

            return True
        except Exception:
            return False

    def _require_backend(self):
        try:
            import win32crypt  # type: ignore
        except Exception as exc:  # pragma: no cover - 运行时环境相关
            raise DpapiUnavailableError("缺少 pywin32/win32crypt，无法使用 DPAPI") from exc
        return win32crypt

    def protect(self, plain: bytes) -> bytes:
        if not isinstance(plain, (bytes, bytearray)):
            raise TypeError("plain 必须是 bytes")
        win32crypt = self._require_backend()
        return win32crypt.CryptProtectData(bytes(plain), self.description, self.entropy, None, None, 0)

    def unprotect(self, cipher: bytes) -> bytes:
        if not isinstance(cipher, (bytes, bytearray)):
            raise TypeError("cipher 必须是 bytes")
        win32crypt = self._require_backend()
        _desc, raw = win32crypt.CryptUnprotectData(bytes(cipher), self.entropy, None, None, 0)
        return bytes(raw)

    def protect_to_b64(self, plain_text: str) -> str:
        raw = plain_text.encode("utf-8")
        cipher = self.protect(raw)
        return base64.b64encode(cipher).decode("ascii")

    def unprotect_from_b64(self, cipher_b64: str) -> str:
        raw = base64.b64decode(cipher_b64.encode("ascii"))
        plain = self.unprotect(raw)
        return plain.decode("utf-8")


__all__ = ["DpapiStore", "DpapiUnavailableError"]
