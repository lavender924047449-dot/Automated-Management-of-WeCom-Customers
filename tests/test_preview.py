from __future__ import annotations

import os
from pathlib import Path
from subprocess import CompletedProcess

from app.broadcast.preview import PreviewService


def test_pull_preview_image_android_path(tmp_path, monkeypatch):
    service = PreviewService(cache_dir=tmp_path)

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        target = Path(cmd[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-image")
        return CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("app.broadcast.preview.resolve_adb_binary", lambda: "adb")
    monkeypatch.setattr("app.broadcast.preview.subprocess.run", fake_run)

    out = service.pull_preview(
        {
            "asset_type": "image",
            "path": "/sdcard/Test-Voice/voice_lib/demo.png",
        }
    )
    assert out
    assert Path(out).exists()
    assert calls
    assert calls[0][:4] == ["adb", "-s", "127.0.0.1:16384", "pull"]


def test_pull_preview_voice_relative_file_with_decode(tmp_path, monkeypatch):
    local_silk = tmp_path / "local.silk"
    local_silk.write_bytes(b"\x02#!SILK_V3\n")
    wav_out = tmp_path / "decoded.wav"
    wav_out.write_bytes(b"RIFF0000WAVE")

    service = PreviewService(cache_dir=tmp_path)

    monkeypatch.setattr(service, "_resolve_voice_source", lambda _payload: str(local_silk))
    monkeypatch.setattr(service, "_decode_silk_to_wav", lambda _path: wav_out)

    out = service.pull_preview({"asset_type": "voice", "file": "x.silk"})
    assert out == str(wav_out)


def test_gc_cache_lru_removes_oldest(tmp_path):
    old = tmp_path / "old.bin"
    new = tmp_path / "new.bin"
    old.write_bytes(b"a" * 6)
    new.write_bytes(b"b" * 6)
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    service = PreviewService(cache_dir=tmp_path, cache_limit_bytes=10)
    removed = service.gc_cache()

    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_voice_decode_failure_is_not_exposed_as_playable_silk(tmp_path, monkeypatch):
    local_silk = tmp_path / "broken.silk"
    local_silk.write_bytes(b"not-decodable")
    service = PreviewService(cache_dir=tmp_path)
    monkeypatch.setattr(service, "_resolve_voice_source", lambda _payload: str(local_silk))
    monkeypatch.setattr(service, "_decode_silk_to_wav", lambda _path: None)

    assert service.pull_preview({"asset_type": "voice", "file": "broken.silk"}) == ""


def test_voice_relative_path_resolves_under_voice_library(tmp_path: Path):
    from app.broadcast.paths import VOICE_LIB_ROOT

    service = PreviewService(cache_dir=tmp_path)
    assert service._resolve_voice_source({"path": "relative/demo.silk"}) == (
        f"{VOICE_LIB_ROOT}/relative/demo.silk"
    )
