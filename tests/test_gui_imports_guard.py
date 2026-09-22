from __future__ import annotations

from pathlib import Path

from scripts.check_gui_imports import find_runtime_import_violations


def test_gui_runtime_imports_guard_passes_current_repo():
    root = Path(__file__).resolve().parent.parent
    rows = find_runtime_import_violations(root)
    assert rows == []
