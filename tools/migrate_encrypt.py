"""Step 16.4: 明文 JSON/JSONL 一次性迁移到 DPAPI 密文。"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import project_root
from app.security import get_secure_store

STORE = get_secure_store()


def _targets(root: Path) -> list[Path]:
    files: list[Path] = []
    files.extend((root / "data").glob("*/voice_lib/voice_lib_index.json"))
    files.extend((root / "data").glob("*/contacts_cache/contacts_cache.json"))
    files.extend((root / "data").glob("*/reports/*.jsonl"))
    files.extend((root / "data").glob("*/reports/*.json"))
    files.extend((root / "data").glob("*/schedules.json"))
    files.extend((root / "app" / "broadcast" / "oa_templates").glob("*.json"))
    legacy = root / "runtime" / "voice_lib_index.json"
    if legacy.exists():
        files.append(legacy)
    uniq: dict[str, Path] = {}
    for fp in files:
        uniq[str(fp.resolve())] = fp
    return sorted(uniq.values())


def _migrate_json(path: Path, *, dry_run: bool) -> bool:
    payload = STORE.load_json(path, default=None)
    if payload is None:
        return False
    if dry_run:
        return True
    STORE.save_json(path, payload, encrypt=True)
    return True


def _migrate_jsonl(path: Path, *, dry_run: bool) -> bool:
    rows = STORE.load_jsonl(path)
    if not rows:
        return False
    if dry_run:
        return True
    path.write_text("", encoding="utf-8")
    for row in rows:
        STORE.append_jsonl(path, row, encrypt=True)
    return True


def migrate(root: Path, *, dry_run: bool = False) -> tuple[int, int]:
    changed = 0
    scanned = 0
    for path in _targets(root):
        if not path.exists():
            continue
        scanned += 1
        ok = _migrate_jsonl(path, dry_run=dry_run) if path.suffix == ".jsonl" else _migrate_json(path, dry_run=dry_run)
        if ok:
            changed += 1
            print(f"[migrate_encrypt] {'would encrypt' if dry_run else 'encrypted'}: {path}")
    return scanned, changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate plaintext JSON/JSONL into DPAPI encrypted payloads")
    parser.add_argument("--root", default="", help="project root; 默认自动定位")
    parser.add_argument("--dry-run", action="store_true", help="只打印待迁移文件，不写盘")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve() if args.root else project_root()
    scanned, changed = migrate(root, dry_run=bool(args.dry_run))
    print(f"[migrate_encrypt] scanned={scanned} changed={changed} dry_run={bool(args.dry_run)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
