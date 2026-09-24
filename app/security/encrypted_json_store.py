"""加密 JSON 存储：兼容明文历史数据，默认写入 DPAPI 密文。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.security.dpapi_store import DpapiStore

ENC_MARK = "__enc__"
ENC_VERSION = "dpapi.v1"
ENC_BLOB = "blob"


class EncryptedJsonStore:
    """为 json/jsonl 提供统一加密读写。"""

    def __init__(self, *, dpapi: DpapiStore | None = None) -> None:
        self.dpapi = dpapi or DpapiStore()

    def is_envelope(self, obj: object) -> bool:
        if not isinstance(obj, dict):
            return False
        return obj.get(ENC_MARK) == ENC_VERSION and isinstance(obj.get(ENC_BLOB), str)

    def save_json(self, path: str | Path, payload: Any, *, encrypt: bool = True) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not encrypt:
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return target
        plain = json.dumps(payload, ensure_ascii=False)
        wrapped = {
            ENC_MARK: ENC_VERSION,
            ENC_BLOB: self.dpapi.protect_to_b64(plain),
        }
        target.write_text(json.dumps(wrapped, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def load_json(self, path: str | Path, *, default: Any = None) -> Any:
        target = Path(path)
        if not target.exists():
            return default
        try:
            text = target.read_text(encoding="utf-8").strip()
            if not text:
                return default
            parsed = json.loads(text)
            if self.is_envelope(parsed):
                plain = self.dpapi.unprotect_from_b64(parsed[ENC_BLOB])
                return json.loads(plain)
            return parsed
        except Exception:
            # DPAPI 密文绑的是「加密时的那台 Windows 用户」。换电脑或换账号后
            # CryptUnprotectData 会失败；当成空库，避免采集进程直接退出。
            return default

    def append_jsonl(self, path: str | Path, row: dict[str, Any], *, encrypt: bool = True) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if encrypt:
            plain = json.dumps(row, ensure_ascii=False)
            line_obj = {
                ENC_MARK: ENC_VERSION,
                ENC_BLOB: self.dpapi.protect_to_b64(plain),
            }
            line = json.dumps(line_obj, ensure_ascii=False)
        else:
            line = json.dumps(row, ensure_ascii=False)
        with target.open("a", encoding="utf-8") as fp:
            fp.write(line)
            fp.write("\n")
        return target

    def load_jsonl(self, path: str | Path) -> list[dict[str, Any]]:
        target = Path(path)
        rows: list[dict[str, Any]] = []
        if not target.exists():
            return rows
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = self._decode_jsonl_line(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _decode_jsonl_line(self, line: str) -> Any:
        obj = json.loads(line)
        if self.is_envelope(obj):
            plain = self.dpapi.unprotect_from_b64(obj[ENC_BLOB])
            return json.loads(plain)
        return obj


__all__ = ["EncryptedJsonStore", "ENC_MARK", "ENC_VERSION", "ENC_BLOB"]
