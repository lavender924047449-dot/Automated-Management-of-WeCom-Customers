from __future__ import annotations

import json
from io import BytesIO

import app.updater.version_check as vc


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._buf = BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_check_latest_version_detects_update(monkeypatch):
    payload = {"tag_name": "v0.2.0", "html_url": "https://github.com/org/Test-Voice/releases/tag/v0.2.0"}

    def fake_urlopen(_req, timeout=0):  # noqa: ARG001
        return _FakeResponse(payload)

    monkeypatch.setattr(vc, "urlopen", fake_urlopen)
    out = vc.check_latest_version(current_version="0.1.0", api_url="https://api.github.com/repos/org/Test-Voice/releases/latest")
    assert out.has_update is True
    assert out.latest_version == "0.2.0"
    assert out.releases_url.endswith("/v0.2.0")


def test_check_latest_version_handles_404_with_friendly_message(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(_req, timeout=0):  # noqa: ARG001
        raise HTTPError(
            url="https://api.github.com/repos/org/Test-Voice/releases/latest",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(vc, "urlopen", fake_urlopen)
    out = vc.check_latest_version(current_version="0.1.0", api_url="https://api.github.com/repos/org/Test-Voice/releases/latest")
    assert out.has_update is False
    assert "尚未找到 GitHub Releases" in out.error


def test_check_latest_version_handles_network_error(monkeypatch):
    def fake_urlopen(_req, timeout=0):  # noqa: ARG001
        raise OSError("network down")

    monkeypatch.setattr(vc, "urlopen", fake_urlopen)
    out = vc.check_latest_version(current_version="0.1.0", api_url="https://api.github.com/repos/org/Test-Voice/releases/latest")
    assert out.has_update is False
    assert out.latest_version == "0.1.0"
    assert "network down" in out.error


def test_version_key_compares_numeric_parts():
    assert vc._version_key("v1.10.0") > vc._version_key("1.2.9")
