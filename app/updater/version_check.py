"""GitHub Releases 版本检查。

发布附件名必须是 ``program-update.zip``。压缩包根目录只放「程序」和
``pyproject.toml``，由目录分发安装覆盖代码，不碰 data 和 vendor。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from importlib import metadata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import project_root

DEFAULT_GITHUB_REPO = "lavender924047449-dot/Automated-Management-of-WeCom-Customers"
DEFAULT_RELEASES_PAGE = f"https://github.com/{DEFAULT_GITHUB_REPO}/releases"
DEFAULT_RELEASES_API = f"https://api.github.com/repos/{DEFAULT_GITHUB_REPO}/releases/latest"
DEFAULT_ADAPTER_VERSION = "5.0.10.6015"
PROGRAM_UPDATE_ASSET = "program-update.zip"


@dataclass(slots=True)
class VersionCheckResult:
    current_version: str
    latest_version: str
    has_update: bool
    releases_url: str
    error: str = ""
    asset_name: str = ""
    asset_url: str = ""


def get_local_app_version() -> str:
    """读取当前应用版本，优先包元数据，失败后回退 pyproject.toml。"""

    try:
        return metadata.version("wecom-voice-blaster")
    except Exception:
        pass
    fallback = _read_version_from_pyproject()
    return fallback or "0.0.0"


def check_latest_version(
    *,
    current_version: str | None = None,
    timeout_seconds: float = 2.5,
    api_url: str | None = None,
) -> VersionCheckResult:
    """检查 GitHub 最新发行版。网络失败时返回 error，不抛异常。"""

    current = (current_version or get_local_app_version()).strip() or "0.0.0"
    endpoint = (api_url or os.environ.get("TESTVOICE_RELEASES_API", "") or DEFAULT_RELEASES_API).strip()
    releases_url = _releases_page_from_api(endpoint)
    if not endpoint:
        return VersionCheckResult(
            current_version=current,
            latest_version=current,
            has_update=False,
            releases_url=DEFAULT_RELEASES_PAGE,
            error="missing releases api",
        )
    req = Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-voice-updater",
        },
    )
    try:
        with urlopen(req, timeout=float(timeout_seconds)) as resp:  # noqa: S310 - fixed trusted host by default
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return VersionCheckResult(
            current_version=current,
            latest_version=current,
            has_update=False,
            releases_url=releases_url or DEFAULT_RELEASES_PAGE,
            error=_format_check_error(exc),
        )

    latest = _extract_latest_version(payload) or current
    release_link = str(payload.get("html_url", "")).strip() if isinstance(payload, dict) else ""
    if release_link:
        releases_url = release_link
    asset_name, asset_url = _extract_update_asset(payload)
    return VersionCheckResult(
        current_version=current,
        latest_version=latest,
        has_update=_is_newer(latest, current),
        releases_url=releases_url or DEFAULT_RELEASES_PAGE,
        error="",
        asset_name=asset_name,
        asset_url=asset_url,
    )


def _extract_update_asset(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return "", ""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).strip()
        if name.lower() != PROGRAM_UPDATE_ASSET.lower():
            continue
        url = str(asset.get("browser_download_url", "")).strip()
        if url:
            return name, url
    return "", ""


def _extract_latest_version(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("tag_name", "name"):
        value = str(payload.get(key, "")).strip()
        normalized = _normalize_version(value)
        if normalized:
            return normalized
    return ""


def _normalize_version(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    m = re.search(r"\d+\.\d+\.\d+(?:[.-][0-9a-z]+)?", text)
    if m:
        return m.group(0)
    if re.fullmatch(r"\d+(?:\.\d+)*", text):
        return text
    return ""


def _is_newer(latest: str, current: str) -> bool:
    lkey = _version_key(latest)
    ckey = _version_key(current)
    return lkey > ckey


def _version_key(raw: str) -> tuple[int, ...]:
    text = _normalize_version(raw)
    if not text:
        return (0,)
    prefix = text.split("-", 1)[0].split(".", 3)
    out: list[int] = []
    for part in prefix:
        if part.isdigit():
            out.append(int(part))
        else:
            out.append(0)
    return tuple(out) or (0,)


def read_pyproject_version(path: Path) -> str:
    if not path.is_file():
        return ""
    body = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', body)
    return str(match.group(1)).strip() if match else ""


def _read_version_from_pyproject() -> str:
    return read_pyproject_version(project_root() / "pyproject.toml")


def _format_check_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError) and exc.code == 404:
        return (
            "尚未找到 GitHub Releases（仓库不存在或尚未发布任何版本）。"
            "内部使用可忽略；若需在线更新，请设置环境变量 TESTVOICE_RELEASES_API 指向你的 Releases API。"
        )
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    return str(exc)


def _releases_page_from_api(api_url: str) -> str:
    text = str(api_url or "").strip()
    if not text:
        return DEFAULT_RELEASES_PAGE
    if "/releases/latest" in text:
        return text.replace("api.github.com/repos/", "github.com/").replace("/releases/latest", "/releases")
    return DEFAULT_RELEASES_PAGE

