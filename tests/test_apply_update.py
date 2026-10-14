from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import app.updater.apply_update as upd
from app.updater.pack_update import PackError, build_program_update_zip


def _write_update_zip(path: Path, *, version: str = "0.2.0", members: dict[str, str] | None = None) -> None:
    payload = members or {
        "pyproject.toml": f'[project]\nversion = "{version}"\n',
        "程序/app/marker.txt": "new",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in payload.items():
            archive.writestr(name, body)


def _install_home(tmp_path: Path) -> Path:
    home = tmp_path / "install"
    (home / "程序" / "gui").mkdir(parents=True)
    (home / "程序" / "gui" / "old.py").write_text("old", encoding="utf-8")
    (home / "data").mkdir()
    (home / "data" / "keep.txt").write_text("keep", encoding="utf-8")
    (home / "vendor").mkdir()
    (home / "vendor" / "adb.exe").write_text("adb", encoding="utf-8")
    (home / "python").mkdir()
    (home / "python" / "pythonw.exe").write_bytes(b"py")
    (home / "启动.exe").write_bytes(b"exe")
    (home / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    return home


def test_apply_replaces_program_and_keeps_user_data(tmp_path: Path):
    home = _install_home(tmp_path)
    extracted = tmp_path / "extracted"
    (extracted / "程序" / "app").mkdir(parents=True)
    (extracted / "程序" / "app" / "marker.txt").write_text("new", encoding="utf-8")
    (extracted / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n', encoding="utf-8")

    upd.apply_extracted_update(home, extracted)

    assert (home / "程序" / "app" / "marker.txt").read_text(encoding="utf-8") == "new"
    assert not (home / "程序" / "gui" / "old.py").exists()
    assert (home / "data" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (home / "vendor" / "adb.exe").read_text(encoding="utf-8") == "adb"
    assert 'version = "0.2.0"' in (home / "pyproject.toml").read_text(encoding="utf-8")


def test_validate_rejects_data_and_path_traversal(tmp_path: Path):
    leaked = tmp_path / "leaked.zip"
    _write_update_zip(
        leaked,
        members={
            "pyproject.toml": '[project]\nversion = "0.2.0"\n',
            "程序/app/marker.txt": "new",
            "data/secret.txt": "nope",
        },
    )
    with pytest.raises(upd.UpdateError, match="不允许"):
        upd.validate_update_archive(leaked, expected_version="0.2.0")

    escaped = tmp_path / "escaped.zip"
    _write_update_zip(
        escaped,
        members={
            "pyproject.toml": '[project]\nversion = "0.2.0"\n',
            "程序/../../data/evil.txt": "nope",
        },
    )
    with pytest.raises(upd.UpdateError, match="不安全"):
        upd.validate_update_archive(escaped, expected_version="0.2.0")


def test_validate_rejects_version_mismatch(tmp_path: Path):
    archive = tmp_path / "program-update.zip"
    _write_update_zip(archive, version="0.1.0")
    with pytest.raises(upd.UpdateError, match="不一致"):
        upd.validate_update_archive(archive, expected_version="0.2.0")


def test_helper_replaces_program_and_relaunches(tmp_path: Path, monkeypatch):
    home = _install_home(tmp_path)
    archive = home / ".update-staging" / "program-update.zip"
    archive.parent.mkdir(parents=True)
    _write_update_zip(archive)
    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(list(cmd))
        return type("Proc", (), {"pid": 1})()

    monkeypatch.setattr(upd.subprocess, "Popen", fake_popen)
    code = upd.main(
        [
            "--wait-pid",
            "0",
            "--home",
            str(home),
            "--zip",
            str(archive),
            "--launcher",
            str(home / "启动.exe"),
            "--expected-version",
            "0.2.0",
        ]
    )
    assert code == 0
    assert launched == [[str(home / "启动.exe")]]
    assert (home / "程序" / "app" / "marker.txt").read_text(encoding="utf-8") == "new"
    assert (home / "data" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not archive.exists()


def test_download_rejects_non_github_url(tmp_path: Path):
    with pytest.raises(upd.UpdateError, match="example.com"):
        upd.download_update("https://example.com/program-update.zip", tmp_path / "x.zip")


def test_portable_install_root_requires_launcher_layout(monkeypatch, tmp_path: Path):
    assert upd.portable_install_root() is None
    home = _install_home(tmp_path)
    monkeypatch.setenv("TESTVOICE_HOME", str(home))
    assert upd.portable_install_root() == home.resolve()


def test_spawn_helper_breaks_away_from_job(monkeypatch, tmp_path: Path):
    home = _install_home(tmp_path)
    archive = tmp_path / "program-update.zip"
    archive.write_bytes(b"zip")
    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["creationflags"] = kwargs.get("creationflags")
        return type("Proc", (), {"pid": 42})()

    monkeypatch.setattr(upd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(upd.os, "getpid", lambda: 7)
    pid = upd.spawn_update_helper(home, archive, expected_version="0.2.0")
    assert pid == 42
    command = list(captured["cmd"])
    assert command[1:3] == ["-m", "app.updater.apply_update"]
    assert "--expected-version" in command
    assert command[-1] == "0.2.0"
    if upd.sys.platform == "win32":
        assert int(captured["creationflags"]) & upd.CREATE_BREAKAWAY_FROM_JOB


def test_pack_update_zip_contains_only_program_and_version(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "gui").mkdir(parents=True)
    (repo / "app").mkdir()
    (repo / "runtime" / "pkg").mkdir(parents=True)
    (repo / "gui" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "app" / "__init__.py").write_text('__version__ = "0.3.0"\n', encoding="utf-8")
    (repo / "runtime" / "pkg" / "engine.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "runtime" / "pkg" / "__pycache__").mkdir()
    (repo / "runtime" / "pkg" / "__pycache__" / "engine.cpython-311.pyc").write_bytes(b"pyc")
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.3.0"\n', encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "secret.txt").write_text("nope", encoding="utf-8")

    dest = tmp_path / "program-update.zip"
    assert build_program_update_zip(repo, dest) == "0.3.0"
    upd.validate_update_archive(dest, expected_version="0.3.0")
    with zipfile.ZipFile(dest) as archive:
        names = set(archive.namelist())
    assert "pyproject.toml" in names
    assert "程序/gui/main.py" in names
    assert "程序/app/__init__.py" in names
    assert "程序/runtime/pkg/engine.py" in names
    assert not any("data/" in name or name.endswith(".pyc") for name in names)


def test_pack_update_rejects_mismatched_versions(tmp_path: Path):
    repo = tmp_path / "repo"
    for name in ("gui", "app", "runtime"):
        (repo / name).mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n', encoding="utf-8")
    with pytest.raises(PackError, match="不一致"):
        build_program_update_zip(repo, tmp_path / "out.zip")
