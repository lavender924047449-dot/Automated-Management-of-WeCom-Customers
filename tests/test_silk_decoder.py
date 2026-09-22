"""
Stage 4.5.5.1.7 SilkDecoder 测试.

* 单测: 只检查参数、错误路径 (不需要真 exe)
* 集成测: 需要 vendor/silk/silk_v3_decoder.exe + vendor/ffmpeg/ffmpeg.exe 都在
        + PC 企微 Cache/Voice 里有 .silk (真机 spike, 缺任一 → skip)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.audio.silk_decoder import SilkDecoder, SilkDecoderNotFound
from app.config import resolve_path


# ---------------- 基本参数检查 ---------------- #

def test_missing_silk_exe_raises(tmp_path: Path):
    fake_silk = tmp_path / "not_exist.exe"
    with pytest.raises(SilkDecoderNotFound, match="silk_v3_decoder"):
        SilkDecoder(silk_exe=fake_silk)


def test_missing_ffmpeg_raises(tmp_path: Path):
    # 借用真实 silk exe (若存在), 但 ffmpeg 给假路径
    real_silk = resolve_path("vendor/silk/silk_v3_decoder.exe")
    if not real_silk.is_file():
        pytest.skip("silk_v3_decoder.exe 未部署, 无法测")
    with pytest.raises(SilkDecoderNotFound, match="ffmpeg"):
        SilkDecoder(ffmpeg_exe=tmp_path / "no_ffmpeg.exe")


def test_decode_missing_input_raises(tmp_path: Path):
    real_silk = resolve_path("vendor/silk/silk_v3_decoder.exe")
    real_ff = resolve_path("vendor/ffmpeg/ffmpeg.exe")
    if not real_silk.is_file() or not real_ff.is_file():
        pytest.skip("解码器 exe 未部署")
    d = SilkDecoder()
    with pytest.raises(FileNotFoundError):
        d.decode(tmp_path / "no.silk", tmp_path / "out.wav")


# ---------------- 集成测试 (需要真 exe + 真 silk 文件) ---------------- #

def _find_a_real_silk() -> Path | None:
    """从 PC 企微缓存里找一个 .silk 文件; 找不到返回 None."""
    wxwork = Path.home() / "Documents" / "WXWork"
    if not wxwork.is_dir():
        return None
    for acct in wxwork.iterdir():
        if not acct.is_dir() or not acct.name.isdigit():
            continue
        voice_dir = acct / "Cache" / "Voice"
        if not voice_dir.is_dir():
            continue
        for silk in voice_dir.rglob("*.silk"):
            if silk.is_file() and silk.stat().st_size > 100:
                return silk
    return None


@pytest.fixture
def real_silk_file():
    p = _find_a_real_silk()
    if p is None:
        pytest.skip("PC 企微 Cache/Voice 里没找到 .silk 文件, 跳过集成测")
    return p


@pytest.fixture
def real_decoder():
    if not resolve_path("vendor/silk/silk_v3_decoder.exe").is_file():
        pytest.skip("vendor/silk/silk_v3_decoder.exe 未部署")
    if not resolve_path("vendor/ffmpeg/ffmpeg.exe").is_file():
        pytest.skip("vendor/ffmpeg/ffmpeg.exe 未部署")
    return SilkDecoder()


def test_decode_produces_valid_wav(
    real_decoder: SilkDecoder, real_silk_file: Path, tmp_path: Path
):
    wav_out = tmp_path / "decoded.wav"
    result = real_decoder.decode(real_silk_file, wav_out)
    assert result == wav_out
    assert wav_out.is_file()
    assert wav_out.stat().st_size > 1000     # 至少 1 KB
    # WAV 头: 'RIFF....WAVE'
    head = wav_out.read_bytes()[:12]
    assert head[:4] == b"RIFF"
    assert head[8:12] == b"WAVE"
    # PCM 文件应被清理
    assert not wav_out.with_suffix(".pcm").exists()


def test_decode_keep_pcm(
    real_decoder: SilkDecoder, real_silk_file: Path, tmp_path: Path
):
    wav_out = tmp_path / "decoded.wav"
    real_decoder.decode(real_silk_file, wav_out, keep_pcm=True)
    assert wav_out.with_suffix(".pcm").exists()


def test_decode_to_cache_idempotent(
    real_decoder: SilkDecoder, real_silk_file: Path, tmp_path: Path
):
    cache = tmp_path / "silk_cache"
    wav1 = real_decoder.decode_to_cache(real_silk_file, cache_dir=cache)
    mtime1 = wav1.stat().st_mtime
    assert wav1.is_file()
    assert wav1.parent == cache
    assert wav1.name.endswith(".wav")

    # 二次调用应该直接命中缓存, 文件不重新写
    import time
    time.sleep(0.05)
    wav2 = real_decoder.decode_to_cache(real_silk_file, cache_dir=cache)
    assert wav2 == wav1
    assert wav2.stat().st_mtime == mtime1
