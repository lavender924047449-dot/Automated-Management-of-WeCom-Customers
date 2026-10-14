from __future__ import annotations

import zipfile
from pathlib import Path

from app.broadcast.library_export import import_library, list_import_conflicts


def _bind_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr("app.broadcast.library_export.data_root_for", lambda _account_id: root)
    monkeypatch.setattr("app.broadcast.library_export.resolve_account_id", lambda account_id: account_id or "acc")


def _zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)


def test_import_without_existing_files_writes_directly(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "acct"
    _bind_root(monkeypatch, root)
    src = tmp_path / "lib.zip"
    _zip(src, {"oa_templates/new.json": '{"name":"新文档"}'})

    assert list_import_conflicts("acc", src) == []
    result = import_library("acc", src, conflict_policy="overwrite")

    assert result.written == 1
    assert result.skipped == 0
    assert (root / "oa_templates" / "new.json").read_text(encoding="utf-8") == '{"name":"新文档"}'


def test_import_conflict_policy_overwrites_or_skips(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "acct"
    existing = root / "oa_templates" / "doc.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"name":"旧文档"}', encoding="utf-8")
    _bind_root(monkeypatch, root)
    src = tmp_path / "lib.zip"
    _zip(src, {"oa_templates/doc.json": '{"name":"新文档"}', "oa_templates/extra.json": '{"name":"补充"}'})

    assert list_import_conflicts("acc", src) == ["oa_templates/doc.json"]

    skipped = import_library("acc", src, conflict_policy="skip")
    assert skipped.skipped == 1
    assert skipped.written == 1
    assert existing.read_text(encoding="utf-8") == '{"name":"旧文档"}'
    assert (root / "oa_templates" / "extra.json").is_file()

    overwritten = import_library("acc", src, conflict_policy="overwrite")
    assert overwritten.written == 2
    assert existing.read_text(encoding="utf-8") == '{"name":"新文档"}'
