"""Step 18: Frida 适配脚本在线更新（manifest + hooks 双文件）。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import resolve_path

DEFAULT_ADAPTER_BASE_URL = (
    "https://github.com/DivergerThinking/Test-Voice/raw/main/dist/adapter/5.0.10.6015"
)
DEFAULT_LOCAL_ADAPTER_DIR = Path("runtime") / "wework_re_android"
DEFAULT_LOCAL_MANIFEST = DEFAULT_LOCAL_ADAPTER_DIR / "adapter_manifest.json"
DEFAULT_LOCAL_HOOKS = DEFAULT_LOCAL_ADAPTER_DIR / "send_engine_hooks.js"
ADAPTER_BASE_URL_ENV = "TESTVOICE_ADAPTER_BASE_URL"
NO_AUTO_ADAPTER_ENV = "TESTVOICE_NO_AUTO_ADAPTER"


@dataclass(slots=True)
class PullResult:
    changed: bool
    manifest_path: Path
    hooks_path: Path
    message: str = ""
    error: str = ""


def should_disable_auto_adapter(*, no_auto_adapter: bool = False) -> bool:
    if no_auto_adapter:
        return True
    env = os.environ.get(NO_AUTO_ADAPTER_ENV, "").strip().lower()
    return env in {"1", "true", "yes", "on"}


def pull_adapter_if_needed(
    *,
    base_url: str | None = None,
    manifest_path: str | Path | None = None,
    hooks_path: str | Path | None = None,
    timeout_seconds: float = 2.5,
    no_auto_adapter: bool = False,
) -> PullResult:
    local_manifest = resolve_path(manifest_path or DEFAULT_LOCAL_MANIFEST)
    local_hooks = resolve_path(hooks_path or DEFAULT_LOCAL_HOOKS)
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_hooks.parent.mkdir(parents=True, exist_ok=True)

    if should_disable_auto_adapter(no_auto_adapter=no_auto_adapter):
        return PullResult(
            changed=False,
            manifest_path=local_manifest,
            hooks_path=local_hooks,
            message="auto adapter disabled",
        )

    base = (base_url or os.environ.get(ADAPTER_BASE_URL_ENV, "") or DEFAULT_ADAPTER_BASE_URL).rstrip("/")
    remote_manifest_url = f"{base}/manifest.json"
    local_manifest_obj = _read_local_manifest(local_manifest)
    local_version = _version_key(local_manifest_obj)

    try:
        remote_manifest_obj = _fetch_json(remote_manifest_url, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return PullResult(
            changed=False,
            manifest_path=local_manifest,
            hooks_path=local_hooks,
            error=f"manifest fetch failed: {exc}",
        )

    remote_version = _version_key(remote_manifest_obj)
    hooks_name = str(remote_manifest_obj.get("hooks_script", "")).strip() or "send_engine_hooks.js"
    remote_hooks_url = f"{base}/{hooks_name}"

    need_update = (not local_manifest.exists()) or (not local_hooks.exists()) or (remote_version > local_version)
    if not need_update:
        return PullResult(
            changed=False,
            manifest_path=local_manifest,
            hooks_path=local_hooks,
            message="adapter up-to-date",
        )

    try:
        hooks_body = _fetch_text(remote_hooks_url, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return PullResult(
            changed=False,
            manifest_path=local_manifest,
            hooks_path=local_hooks,
            error=f"hooks fetch failed: {exc}",
        )

    _write_json(local_manifest, remote_manifest_obj)
    local_hooks.write_text(hooks_body, encoding="utf-8")
    return PullResult(
        changed=True,
        manifest_path=local_manifest,
        hooks_path=local_hooks,
        message=f"adapter updated to {remote_version}",
    )


def _fetch_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    text = _fetch_text(url, timeout_seconds=timeout_seconds)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("manifest is not json object")
    return payload


def _fetch_text(url: str, *, timeout_seconds: float) -> str:
    req = Request(
        str(url).strip(),
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "test-voice-adapter-updater",
        },
    )
    try:
        with urlopen(req, timeout=float(timeout_seconds)) as resp:  # noqa: S310
            data = resp.read().decode("utf-8", errors="ignore")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(str(exc)) from exc
    if not data.strip():
        raise RuntimeError("empty response body")
    return data


def _read_local_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(obj, dict):
        return obj
    return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _version_key(payload: dict[str, Any]) -> tuple[int, ...]:
    raw = str(payload.get("adapter_version", "")).strip()
    if not raw:
        raw = str(payload.get("wework_version", "")).strip()
    parts: list[int] = []
    for token in raw.replace("-", ".").split("."):
        token = token.strip()
        if token.isdigit():
            parts.append(int(token))
        else:
            num = "".join(ch for ch in token if ch.isdigit())
            parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


__all__ = [
    "ADAPTER_BASE_URL_ENV",
    "DEFAULT_ADAPTER_BASE_URL",
    "DEFAULT_LOCAL_HOOKS",
    "DEFAULT_LOCAL_MANIFEST",
    "NO_AUTO_ADAPTER_ENV",
    "PullResult",
    "pull_adapter_if_needed",
    "should_disable_auto_adapter",
]
