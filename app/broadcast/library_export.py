"""素材库一键导出 / 导入。

导出：当前账号的全部索引 + 媒体文件 → zip。
导入：解压 zip → 逐文件检查冲突 → 按策略写入。
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from loguru import logger

from app.broadcast.account import resolve_account_id
from app.broadcast.paths import data_root_for


# -- 导出 ------------------------------------------------------------------

def export_library(account_id: str, dest_zip: Path) -> Path:
    """将当前账号的素材数据目录打包为 zip。"""
    aid = resolve_account_id(account_id)
    root = data_root_for(aid)
    dest = Path(dest_zip)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        _zip_tree(zf, root, root)
    logger.info("素材库已导出 account_id={} dest={}", aid, dest)
    return dest


def _zip_tree(zf: zipfile.ZipFile, base: Path, current: Path) -> None:
    """递归把 current 目录下所有文件写入 zip，arcname 相对于 base。"""
    if not current.exists():
        return
    if current.is_file():
        arcname = current.relative_to(base).as_posix()
        zf.write(current, arcname)
        return
    for child in sorted(current.iterdir()):
        _zip_tree(zf, base, child)


# -- 导入 ------------------------------------------------------------------

ConflictChoice = Literal["overwrite", "skip", "overwrite_all", "skip_all"]
ConflictPolicy = Literal["overwrite", "skip"]


@dataclass
class ImportResult:
    written: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def list_import_conflicts(account_id: str, src_zip: Path) -> list[str]:
    """返回 zip 里已经存在于当前账号目录的相对路径。"""
    root = data_root_for(resolve_account_id(account_id))
    conflicts: list[str] = []
    with zipfile.ZipFile(src_zip, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _safe_zip_rel(info.filename)
            if rel is None:
                continue
            if (root / rel).exists():
                conflicts.append(rel.as_posix())
    return conflicts


def _safe_zip_rel(name: str) -> Path | None:
    text = str(name or "").replace("\\", "/").strip()
    if not text or text.endswith("/"):
        return None
    rel = Path(text)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel


def import_library(
    account_id: str,
    src_zip: Path,
    conflict_cb: Callable[[str], ConflictChoice] | None = None,
    *,
    conflict_policy: ConflictPolicy | None = None,
) -> ImportResult:
    """解压 zip 到账号数据目录。conflict_policy 一次决定全部已存在文件是覆盖还是跳过。"""
    aid = resolve_account_id(account_id)
    root = data_root_for(aid)
    result = ImportResult()
    global_policy: ConflictChoice | None = None

    with tempfile.TemporaryDirectory(prefix="voice_import_") as tmpdir:
        tmp = Path(tmpdir)
        try:
            with zipfile.ZipFile(src_zip, "r") as zf:
                zf.extractall(tmp)
        except Exception as exc:
            result.errors.append(f"解压失败: {exc}")
            return result

        for src_file in sorted(tmp.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(tmp)
            dest_file = root / rel

            if dest_file.exists():
                if conflict_policy == "overwrite":
                    choice = "overwrite"
                elif conflict_policy == "skip":
                    choice = "skip"
                elif global_policy == "overwrite_all":
                    choice = "overwrite"
                elif global_policy == "skip_all":
                    choice = "skip"
                elif conflict_cb is not None:
                    raw = conflict_cb(str(rel))
                    if raw in ("overwrite_all", "skip_all"):
                        global_policy = raw
                    choice = "overwrite" if raw in ("overwrite", "overwrite_all") else "skip"
                else:
                    choice = "skip"

                if choice == "skip":
                    result.skipped += 1
                    continue

            try:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)
                result.written += 1
            except Exception as exc:
                result.errors.append(f"{rel}: {exc}")

    logger.info(
        "素材库导入完成 account_id={} written={} skipped={} errors={}",
        aid, result.written, result.skipped, len(result.errors),
    )
    return result


__all__ = [
    "ConflictChoice",
    "ImportResult",
    "export_library",
    "import_library",
    "list_import_conflicts",
]
