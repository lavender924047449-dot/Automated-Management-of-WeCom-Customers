from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import base64

from app.security.dpapi_store import DpapiStore
from app.security.encrypted_json_store import ENC_BLOB, ENC_MARK, EncryptedJsonStore


@pytest.mark.skipif(not DpapiStore.is_available(), reason="win32crypt 不可用")
def test_dpapi_roundtrip():
    store = DpapiStore()
    plain = b'{"x":1,"name":"alice"}'
    cipher = store.protect(plain)
    assert cipher != plain
    back = store.unprotect(cipher)
    assert back == plain


@pytest.mark.skipif(not DpapiStore.is_available(), reason="win32crypt 不可用")
def test_encrypted_json_cross_process_roundtrip(tmp_path: Path):
    target = tmp_path / "secure.json"
    payload = {"contact": "海鸟与鱼", "conv_local": "7685582584742596232"}

    enc = EncryptedJsonStore()
    enc.save_json(target, payload, encrypt=True)

    outer = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(outer, dict)
    assert outer.get(ENC_MARK) == "dpapi.v1"

    code = """
import json
import sys
from app.security.encrypted_json_store import EncryptedJsonStore

path = sys.argv[1]
payload = EncryptedJsonStore().load_json(path)
print(json.dumps(payload, ensure_ascii=False))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    back = json.loads(proc.stdout.strip())
    assert back == payload


def test_foreign_dpapi_blob_returns_default(tmp_path: Path):
    target = tmp_path / "foreign.json"
    target.write_text(
        json.dumps({ENC_MARK: "dpapi.v1", ENC_BLOB: base64.b64encode(b"not-from-this-pc").decode("ascii")}),
        encoding="utf-8",
    )
    assert EncryptedJsonStore().load_json(target, default=[]) == []
