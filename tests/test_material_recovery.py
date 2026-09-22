from __future__ import annotations

from pathlib import Path

import app.broadcast.account as account_mod
import app.broadcast.material_recovery as recovery_mod
import app.broadcast.paths as paths_mod
from app.security import get_secure_store


def test_hydrate_materials_copies_from_rich_source(tmp_path: Path, monkeypatch) -> None:
    def fake_resolve_path(p: str | Path) -> Path:
        target = Path(p)
        return target if target.is_absolute() else (tmp_path / target)

    monkeypatch.setattr(paths_mod, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(account_mod, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(recovery_mod, "data_root_for", paths_mod.data_root_for)
    monkeypatch.setattr(recovery_mod, "list_accounts", lambda: ["acct_a", "acct_b"])

    secure = get_secure_store()
    src_root = paths_mod.data_root_for("acct_a")
    dst_root = paths_mod.data_root_for("acct_b")
    (src_root / "image_templates").mkdir(parents=True, exist_ok=True)
    secure.save_json(src_root / "image_templates" / "img1.json", {"name": "img1"}, encrypt=True)
    (src_root / "voice_lib").mkdir(parents=True, exist_ok=True)
    secure.save_json(
        src_root / "voice_lib" / "voice_lib_index.json",
        [{"id": "1", "file": "a.silk", "duration": 3, "captured_at": "2026-01-01T00:00:00"}],
        encrypt=True,
    )

    result = recovery_mod.hydrate_materials("acct_b", preferred_source="acct_a")
    assert result["source"] == "acct_a"
    assert (dst_root / "image_templates" / "img1.json").exists()
    raw = secure.load_json(dst_root / "voice_lib" / "voice_lib_index.json", default=[])
    assert isinstance(raw, list) and len(raw) == 1
