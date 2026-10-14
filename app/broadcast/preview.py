"""Step 20: 素材预览后端接口（adb pull + silk decode + 本地缓存）。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from app.audio.silk_decoder import SilkDecoder
from app.bootstrap.runtime_bootstrap import resolve_adb_binary
from app.broadcast.paths import VOICE_LIB_ROOT

PREVIEW_CACHE_DIR_ENV = "TESTVOICE_PREVIEW_CACHE_DIR"
DEFAULT_PREVIEW_CACHE_DIR = Path(os.environ.get("APPDATA", Path.home())) / "TestVoice" / "preview_cache"
DEFAULT_PREVIEW_CACHE_LIMIT_BYTES = 200 * 1024 * 1024
ANDROID_PREFIXES = ("/sdcard/", "/storage/emulated/")
DEFAULT_ADB_SERIAL = "127.0.0.1:16384"


def cache_root() -> Path:
    custom = os.environ.get(PREVIEW_CACHE_DIR_ENV, "").strip()
    base = Path(custom) if custom else DEFAULT_PREVIEW_CACHE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base


def is_android_path(path: str) -> bool:
    text = str(path or "").strip()
    return text.startswith(ANDROID_PREFIXES)


def decode_silk(
    silk_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    decoder: SilkDecoder | None = None,
) -> Path:
    active_decoder = decoder or SilkDecoder()
    return active_decoder.decode_to_cache(silk_path, cache_dir=cache_dir)


class PreviewService:
    """素材预览服务：统一把素材转成桌面端可预览路径。"""

    def __init__(
        self,
        *,
        adb_serial: str = DEFAULT_ADB_SERIAL,
        cache_dir: str | Path | None = None,
        cache_limit_bytes: int = DEFAULT_PREVIEW_CACHE_LIMIT_BYTES,
    ) -> None:
        self.adb_serial = adb_serial
        self.cache_dir = Path(cache_dir) if cache_dir else cache_root()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_limit_bytes = max(1, int(cache_limit_bytes))
        self._silk_decoder: SilkDecoder | None = None
        self._silk_decoder_error = False

    def pull_preview(self, payload: Mapping[str, Any]) -> str:
        asset_type = str(payload.get("asset_type", "")).strip().lower()
        if asset_type == "voice":
            return self._preview_voice(payload)
        if asset_type in {"image", "video"}:
            return self._preview_media(payload, asset_type=asset_type)
        return ""

    def gc_cache(self) -> int:
        files = sorted(
            [p for p in self.cache_dir.rglob("*") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in files)
        removed = 0
        for fp in files:
            if total <= self.cache_limit_bytes:
                break
            try:
                size = fp.stat().st_size
                fp.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            removed += 1
        return removed

    def _preview_voice(self, payload: Mapping[str, Any]) -> str:
        raw = self._resolve_voice_source(payload)
        if not raw:
            return ""
        local_silk = self._materialize_local(raw, fallback_ext=".silk")
        if not local_silk:
            return ""
        if local_silk.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".ogg"}:
            self.gc_cache()
            return str(local_silk)
        wav = self._decode_silk_to_wav(local_silk)
        self.gc_cache()
        # QMediaPlayer 不能可靠播放 silk；解码失败时不要把 silk 当音频源。
        return str(wav) if wav is not None and wav.exists() else ""

    def _preview_media(self, payload: Mapping[str, Any], *, asset_type: str) -> str:
        source = str(payload.get("path") or payload.get("file_path") or payload.get("file") or "").strip()
        if not source:
            return ""
        ext = ".mp4" if asset_type == "video" else ".png"
        local = self._materialize_local(source, fallback_ext=ext)
        self.gc_cache()
        return str(local) if local is not None else ""

    def _resolve_voice_source(self, payload: Mapping[str, Any]) -> str:
        path_value = str(payload.get("path", "")).strip()
        if path_value:
            if is_android_path(path_value) or Path(path_value).is_file():
                return path_value
            # 素材库历史索引可能只保存相对于 voice_lib 的文件名。
            if not Path(path_value).is_absolute():
                return f"{VOICE_LIB_ROOT.rstrip('/')}/{path_value.lstrip('/')}"
        file_value = str(payload.get("file", "")).strip()
        if not file_value:
            return ""
        if is_android_path(file_value):
            return file_value
        return f"{VOICE_LIB_ROOT.rstrip('/')}/{file_value.lstrip('/')}"

    def _materialize_local(self, source_path: str, *, fallback_ext: str) -> Path | None:
        src = str(source_path or "").strip()
        if not src:
            return None
        if is_android_path(src):
            return self._pull_from_android(src, fallback_ext=fallback_ext)
        host = Path(src)
        if host.exists():
            return host
        return None

    def _pull_from_android(self, remote_path: str, *, fallback_ext: str) -> Path | None:
        ext = Path(remote_path).suffix or fallback_ext
        name = _cache_name(remote_path, ext=ext)
        target = self.cache_dir / name
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = [resolve_adb_binary(), "-s", self.adb_serial, "pull", remote_path, str(target)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not target.exists():
            target.unlink(missing_ok=True)
            return None
        return target

    def _decode_silk_to_wav(self, silk_path: Path) -> Path | None:
        try:
            if self._silk_decoder is None and not self._silk_decoder_error:
                self._silk_decoder = SilkDecoder()
        except Exception:
            self._silk_decoder_error = True
            self._silk_decoder = None
            return None
        if self._silk_decoder is None:
            return None
        try:
            return self._silk_decoder.decode_to_cache(silk_path, cache_dir=self.cache_dir)
        except Exception:
            return None


def _cache_name(raw_path: str, *, ext: str) -> str:
    digest = hashlib.sha1(raw_path.encode("utf-8")).hexdigest()
    stamp = int(time.time())
    suffix = ext if ext.startswith(".") else f".{ext}"
    return f"{digest}_{stamp}{suffix}"


__all__ = [
    "DEFAULT_PREVIEW_CACHE_DIR",
    "DEFAULT_PREVIEW_CACHE_LIMIT_BYTES",
    "PreviewService",
    "cache_root",
    "decode_silk",
    "is_android_path",
]
