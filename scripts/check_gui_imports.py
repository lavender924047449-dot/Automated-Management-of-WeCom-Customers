"""检查 gui/ 目录是否直接 import runtime。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RUNTIME_IMPORT_RE = re.compile(r"^\s*(from|import)\s+runtime(?:\.|\s|$)")
# Step 6 完成 frozen_dispatch 收敛前，入口仍保留一次 runtime 分发。
ALLOWLIST = {"gui/main.py"}


def find_runtime_import_violations(repo_root: Path) -> list[str]:
    gui_root = repo_root / "gui"
    if not gui_root.exists():
        return []
    violations: list[str] = []
    for file_path in gui_root.rglob("*.py"):
        rel = file_path.relative_to(repo_root).as_posix()
        if rel in ALLOWLIST:
            continue
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if RUNTIME_IMPORT_RE.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")
    return violations


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    rows = find_runtime_import_violations(root)
    if not rows:
        print("OK: gui imports are clean")
        return 0
    print("Found runtime imports in gui/:")
    for row in rows:
        print(f"  - {row}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
