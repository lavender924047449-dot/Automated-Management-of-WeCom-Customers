"""Step 2: VoiceLibrary 数据层测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.broadcast.library import VoiceLibrary
from app.security.encrypted_json_store import ENC_MARK


def test_voice_library_add_list_update_and_stats(tmp_path: Path):
    lib_path = tmp_path / "voice_lib_index.json"
    lib = VoiceLibrary(lib_path)

    e1 = lib.add(file="20260915_133012_3s.silk", duration=3, note="开场问候")
    e2 = lib.add(file="20260915_133125_2s.silk", duration=2)
    lib.update_label(e2.id, "pitch_short")
    lib.bump_usage(e2.id)
    lib.bump_usage(e2.id)

    rows = lib.list()
    assert len(rows) == 2
    assert rows[0].note == "开场问候"
    assert rows[1].label == "pitch_short"
    assert rows[1].usage_count == 2

    stats = lib.stats()
    assert stats["total"] == 2
    assert stats["total_duration_seconds"] == 5
    assert stats["with_note"] == 1
    assert stats["labeled"] == 1
    assert stats["unlabeled"] == 1
    assert stats["archived"] == 0
    assert stats["active"] == 2


def test_voice_library_save_load_roundtrip(tmp_path: Path):
    lib_path = tmp_path / "voice_lib_index.json"
    lib = VoiceLibrary(lib_path)
    row = lib.add(file="20260915_133012_3s.silk", duration=3, note="")
    lib.update_label(row.id, "welcome")
    saved = lib.save()
    assert saved == lib_path

    raw = json.loads(lib_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert raw.get(ENC_MARK) == "dpapi.v1"

    loaded = VoiceLibrary.load(lib_path)
    rows = loaded.list()
    assert len(rows) == 1
    assert rows[0].id == row.id
    assert rows[0].label == "welcome"


def test_voice_archive_and_active_filter(tmp_path: Path):
    lib = VoiceLibrary(tmp_path / "voice_lib_index.json")
    row = lib.add(file="a.silk", duration=2)
    lib.archive(row.id)
    assert lib.list()[0].is_archived is True
    assert lib.list_active() == []
    lib.unarchive(row.id)
    assert len(lib.list_active()) == 1


def test_gc_unnamed_ttl(tmp_path: Path):
    lib = VoiceLibrary(tmp_path / "voice_lib_index.json")
    now = datetime(2026, 9, 16, 18, 0, 0)
    old = (now - timedelta(minutes=40)).isoformat(timespec="seconds")
    fresh = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    lib.add(file="old_no_name.silk", duration=2, captured_at=old)
    lib.add(file="fresh_no_name.silk", duration=2, captured_at=fresh)
    lib.add(file="old_with_note.silk", duration=2, captured_at=old, note="保留")
    removed = lib.gc_unnamed(ttl=1800, now=now)
    assert removed == 1
    files = [x.file for x in lib.list()]
    assert "old_no_name.silk" not in files
    assert "fresh_no_name.silk" in files
    assert "old_with_note.silk" in files


def test_text_templates_roundtrip_and_archive(tmp_path: Path):
    lib_path = tmp_path / "voice_lib_index.json"
    lib = VoiceLibrary(lib_path)
    t1 = lib.add_text_template(
        name="开场模板",
        variants=["你好", "您好，打扰了"],
        tags=["新客", "首触达", "新客"],
    )
    lib.save()

    loaded = VoiceLibrary.load(lib_path)
    rows = loaded.list_text_templates()
    assert len(rows) == 1
    assert rows[0].id == t1.id
    assert rows[0].tags == ["新客", "首触达"]
    assert rows[0].variants == ["你好", "您好，打扰了"]

    loaded.archive_text_template(t1.id)
    assert loaded.list_text_templates() == []
    assert len(loaded.list_text_templates(include_archived=True)) == 1

