"""
打包运行时路径: project_root / bundled_root / resolve_path / ensure_runtime_layout.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import (
    bundled_root,
    ensure_runtime_layout,
    is_frozen,
    project_root,
    resolve_path,
)


def test_dev_mode_not_frozen():
    assert is_frozen() is False
    root = project_root()
    assert (root / "app").is_dir()
    assert (root / "config.yaml").is_file()
    assert bundled_root() == root


def test_project_root_honors_home_env(monkeypatch, tmp_path: Path):
    home = tmp_path / "install"
    home.mkdir()
    monkeypatch.setenv("TESTVOICE_HOME", str(home))
    assert project_root() == home.resolve()
    assert bundled_root() == home.resolve()


def test_resolve_path_relative_and_absolute(tmp_path: Path):
    abs_p = tmp_path / "x.txt"
    abs_p.write_text("ok", encoding="utf-8")
    assert resolve_path(abs_p) == abs_p
    assert resolve_path("config.yaml") == project_root() / "config.yaml"


def test_project_root_frozen_uses_exe_dir(monkeypatch, tmp_path: Path):
    exe_dir = tmp_path / "dist-app"
    internal = exe_dir / "_internal"
    exe_dir.mkdir()
    internal.mkdir()
    exe = exe_dir / "wecom-voice-blaster-gui.exe"
    exe.write_bytes(b"mz")

    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(exe))
    monkeypatch.setattr("sys._MEIPASS", str(internal), raising=False)

    assert is_frozen() is True
    assert project_root() == exe_dir.resolve()
    assert bundled_root() == internal.resolve()


def test_resolve_path_falls_back_to_bundled(monkeypatch, tmp_path: Path):
    exe_dir = tmp_path / "dist-app"
    internal = exe_dir / "_internal"
    ffmpeg_dir = internal / "vendor" / "ffmpeg"
    ffmpeg_dir.mkdir(parents=True)
    (ffmpeg_dir / "ffmpeg.exe").write_bytes(b"ff")
    exe = exe_dir / "app.exe"
    exe_dir.mkdir(exist_ok=True)
    exe.write_bytes(b"mz")

    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(exe))
    monkeypatch.setattr("sys._MEIPASS", str(internal), raising=False)

    found = resolve_path("vendor/ffmpeg/ffmpeg.exe")
    assert found.resolve() == (internal / "vendor" / "ffmpeg" / "ffmpeg.exe").resolve()
    assert found.is_file()


def test_ensure_runtime_layout_copies_from_bundle(monkeypatch, tmp_path: Path):
    exe_dir = tmp_path / "dist-app"
    internal = exe_dir / "_internal"
    exe_dir.mkdir()
    internal.mkdir()
    (internal / "config.yaml").write_text("device:\n  adb_port: 1\n", encoding="utf-8")
    (internal / "contacts.example.csv").write_text("contact\n", encoding="utf-8")
    samples = internal / "samples"
    samples.mkdir()
    (samples / "demo.json").write_text("{}", encoding="utf-8")
    exe = exe_dir / "app.exe"
    exe.write_bytes(b"mz")

    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(exe))
    monkeypatch.setattr("sys._MEIPASS", str(internal), raising=False)

    old_cwd = Path.cwd()
    try:
        root = ensure_runtime_layout()
        assert root == exe_dir.resolve()
        assert (exe_dir / "config.yaml").is_file()
        assert (exe_dir / "samples" / "demo.json").is_file()
        assert (exe_dir / "logs").is_dir()
        assert (exe_dir / "runtime" / "reports").is_dir()

        (exe_dir / "config.yaml").write_text("kept\n", encoding="utf-8")
        ensure_runtime_layout()
        assert (exe_dir / "config.yaml").read_text(encoding="utf-8") == "kept\n"
    finally:
        os.chdir(old_cwd)
