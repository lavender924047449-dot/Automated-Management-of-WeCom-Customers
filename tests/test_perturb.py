"""Step 4.2: silk 扰动测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.broadcast.perturb import SILK_MAGIC, create_perturbed_copy, perturb_silk_bytes


def _make_fake_silk() -> bytes:
    # 伪造最小 silk_v3 容器：header + 三帧（最后一帧最短，模拟静音候选）
    header = b"\x02" + SILK_MAGIC + b"\n"
    frame1 = (6).to_bytes(2, "little") + b"\x11\x22\x33\x44\x55\x66"
    frame2 = (4).to_bytes(2, "little") + b"\x10\x20\x30\x40"
    frame3 = (2).to_bytes(2, "little") + b"\x00\x00"
    return header + frame1 + frame2 + frame3


def test_perturb_preserves_header_and_changes_hash():
    raw = _make_fake_silk()
    out, appended, frame_size = perturb_silk_bytes(raw, min_append_frames=1, max_append_frames=3)
    assert out.startswith(b"\x02" + SILK_MAGIC)
    assert appended in (1, 2, 3)
    assert frame_size == 2
    assert hashlib.sha256(out).hexdigest() != hashlib.sha256(raw).hexdigest()


def test_create_perturbed_copy_roundtrip(tmp_path: Path):
    src = tmp_path / "voice.silk"
    src.write_bytes(_make_fake_silk())
    result = create_perturbed_copy(src, output_dir=tmp_path)
    assert result.output.exists()
    assert result.output.suffix == ".silk"
    assert result.output.read_bytes().startswith(b"\x02" + SILK_MAGIC)
    assert result.appended_frames >= 1
    assert result.frame_size >= 1
    assert result.output.read_bytes() != src.read_bytes()


def test_invalid_file_raises(tmp_path: Path):
    src = tmp_path / "bad.silk"
    src.write_bytes(b"not silk")
    with pytest.raises(ValueError):
        create_perturbed_copy(src, output_dir=tmp_path)

