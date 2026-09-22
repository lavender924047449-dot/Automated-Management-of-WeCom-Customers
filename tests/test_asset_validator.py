from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.broadcast.asset_validator import validate_step_payload


def test_inline_text_ok() -> None:
    ok, err = validate_step_payload({"type": "text", "body": "hello"}, account_id="default")
    assert ok and err == ""


def test_text_missing_body_and_template() -> None:
    ok, err = validate_step_payload({"type": "text"}, account_id="default")
    assert not ok
    assert "body" in err or "template_id" in err


def test_image_local_file_ok(tmp_path: Path) -> None:
    image = tmp_path / "a.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    ok, err = validate_step_payload({"type": "image", "path": str(image)}, account_id="default")
    assert ok, err


def test_image_missing_file() -> None:
    ok, err = validate_step_payload(
        {"type": "image", "path": "D:/not-exist/nope.jpg"},
        account_id="default",
    )
    assert not ok
    assert "不存在" in err


def test_image_device_path_ok() -> None:
    ok, err = validate_step_payload(
        {"type": "image", "path": "/sdcard/Test-Voice/image_lib/a.jpg"},
        account_id="default",
    )
    assert ok, err


def test_video_device_path_ok() -> None:
    ok, err = validate_step_payload(
        {
            "type": "video",
            "path": "/sdcard/Test-Voice/video_lib/a.mp4",
            "thumb_path": "/sdcard/Test-Voice/video_lib/a.wwdata",
        },
        account_id="default",
    )
    assert ok, err


def test_video_empty_path() -> None:
    ok, err = validate_step_payload({"type": "video", "path": ""}, account_id="default")
    assert not ok


def test_oa_missing_id() -> None:
    ok, err = validate_step_payload({"type": "oa", "template_id": ""}, account_id="default")
    assert not ok


class _FakeVoiceLib:
    def __init__(self, rows):
        self._rows = rows

    def list_active(self):
        return self._rows


def test_voice_relative_file_device_path_ok(monkeypatch) -> None:
    from app.broadcast import asset_validator as mod

    row = SimpleNamespace(id="voice-1", file="20260918_xxx.silk", duration=3)
    monkeypatch.setattr(mod.VoiceLibrary, "load", lambda account_id: _FakeVoiceLib([row]))
    ok, err = validate_step_payload({"type": "voice", "voice_id": "voice-1"}, account_id="default")
    assert ok, err


def test_voice_absolute_sdcard_path_ok(monkeypatch) -> None:
    from app.broadcast import asset_validator as mod

    row = SimpleNamespace(id="voice-2", file="/sdcard/Test-Voice/voice_lib/x.silk", duration=6)
    monkeypatch.setattr(mod.VoiceLibrary, "load", lambda account_id: _FakeVoiceLib([row]))
    ok, err = validate_step_payload({"type": "voice", "voice_id": "voice-2"}, account_id="default")
    assert ok, err
