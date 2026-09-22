from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import app.updater.frida_script_puller as fp


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._buf = BytesIO(text.encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_pull_adapter_if_needed_updates_when_remote_newer(tmp_path: Path, monkeypatch):
    local_manifest = tmp_path / "adapter_manifest.json"
    local_hooks = tmp_path / "send_engine_hooks.js"
    local_manifest.write_text(json.dumps({"adapter_version": "2026.09.01"}), encoding="utf-8")
    local_hooks.write_text("// old", encoding="utf-8")

    remote_manifest = {"adapter_version": "2026.09.18", "hooks_script": "send_engine_hooks.js"}
    remote_hooks = "console.log('new');"

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        url = str(getattr(req, "full_url", req))
        if url.endswith("/manifest.json"):
            return _FakeResponse(json.dumps(remote_manifest))
        return _FakeResponse(remote_hooks)

    monkeypatch.setattr(fp, "urlopen", fake_urlopen)
    out = fp.pull_adapter_if_needed(
        base_url="https://example.com/adapter",
        manifest_path=local_manifest,
        hooks_path=local_hooks,
    )
    assert out.changed is True
    assert "updated" in out.message
    assert json.loads(local_manifest.read_text(encoding="utf-8"))["adapter_version"] == "2026.09.18"
    assert local_hooks.read_text(encoding="utf-8") == remote_hooks


def test_pull_adapter_if_needed_respects_no_auto_adapter(tmp_path: Path):
    out = fp.pull_adapter_if_needed(
        no_auto_adapter=True,
        manifest_path=tmp_path / "m.json",
        hooks_path=tmp_path / "h.js",
    )
    assert out.changed is False
    assert out.message == "auto adapter disabled"
