"""把当前仓库打成目录分发用的 program-update.zip。"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from app.updater.version_check import PROGRAM_UPDATE_ASSET, read_pyproject_version

_SOURCE_DIRS = ("gui", "app", "runtime")
_SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", "artifacts", ".git"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


class PackError(Exception):
    """源码树不能打成更新包。"""


def read_dunder_version(path: Path) -> str:
    if not path.is_file():
        return ""
    body = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$', body)
    return str(match.group(1)).strip() if match else ""


def _include_file(path: Path, root: Path) -> bool:
    if not path.is_file():
        return False
    parts = path.relative_to(root).parts
    if any(part in _SKIP_DIR_NAMES for part in parts):
        return False
    return path.suffix.lower() not in _SKIP_SUFFIXES


def build_program_update_zip(repo_root: Path, dest_zip: Path) -> str:
    """写入更新包，返回版本号。pyproject 与 app/__init__.py 的版本必须一致。"""

    pyproject = repo_root / "pyproject.toml"
    version = read_pyproject_version(pyproject)
    code_version = read_dunder_version(repo_root / "app" / "__init__.py")
    if not version:
        raise PackError("pyproject.toml 里没有版本号")
    if version != code_version:
        raise PackError(
            f"版本号不一致：pyproject.toml 是 {version or '空'}，app/__init__.py 是 {code_version or '空'}"
        )
    for name in _SOURCE_DIRS:
        if not (repo_root / name).is_dir():
            raise PackError(f"缺少目录 {name}")

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(pyproject, "pyproject.toml")
        for name in _SOURCE_DIRS:
            source = repo_root / name
            for path in source.rglob("*"):
                if not _include_file(path, repo_root):
                    continue
                archive.write(path, Path("程序").joinpath(path.relative_to(repo_root)).as_posix())
    return version


def default_update_zip_path(repo_root: Path) -> Path:
    return repo_root / "dist" / PROGRAM_UPDATE_ASSET
