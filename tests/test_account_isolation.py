from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import app.broadcast.paths as paths
from app.broadcast.account import get_current_account_id, resolve_account_id, save_account_alias, switch_account
from app.broadcast.library import VoiceLibrary
from app.broadcast.paths import account_voice_index_path


def test_account_voice_index_isolated_by_account():
    a_path = account_voice_index_path("vid_A")
    b_path = account_voice_index_path("vid_B")
    # 这里不依赖工程真实目录，只验证不同 account_id 路径必然不同。
    assert str(a_path).replace("\\", "/").endswith("data/vid_A/voice_lib/voice_lib_index.json")
    assert str(b_path).replace("\\", "/").endswith("data/vid_B/voice_lib/voice_lib_index.json")
    assert a_path != b_path


def test_voice_library_different_accounts_do_not_mix(tmp_path: Path):
    a_index = tmp_path / "data" / "vid_A" / "voice_lib" / "voice_lib_index.json"
    b_index = tmp_path / "data" / "vid_B" / "voice_lib" / "voice_lib_index.json"

    lib_a = VoiceLibrary(index_path=a_index, account_id="vid_A")
    lib_a.add(file="a.silk", duration=3, note="A")
    lib_a.save()

    lib_b = VoiceLibrary(index_path=b_index, account_id="vid_B")
    lib_b.add(file="b.silk", duration=4, note="B")
    lib_b.save()

    rows_a = VoiceLibrary.load(index_path=a_index, account_id="vid_A").list()
    rows_b = VoiceLibrary.load(index_path=b_index, account_id="vid_B").list()
    assert [x.file for x in rows_a] == ["a.silk"]
    assert [x.file for x in rows_b] == ["b.silk"]


def test_default_account_uses_legacy_voice_index_when_exists(tmp_path: Path, monkeypatch):
    legacy = tmp_path / "runtime" / "voice_lib_index.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("[]", encoding="utf-8")

    def fake_resolve_path(p: str | Path) -> Path:
        target = Path(p)
        if target == Path("runtime") / "voice_lib_index.json":
            return legacy
        return tmp_path / target

    monkeypatch.setattr(paths, "resolve_path", fake_resolve_path)
    p = paths.account_voice_index_path("default")
    assert p == legacy


def test_probe_extract_display_name_from_corp_list():
    from runtime.wework_re_android.probe_account_identity import _extract_display_name

    pairs = [
        {
            "key": "GetCorpInfoList",
            "value": (
                "corps <\n"
                "  staff_info <\n"
                '    name: "\\u8303\\u65b0\\u6021"\n'
                '    real_name: "\\u8303\\u65b0\\u6021"\n'
                "  >\n"
                "  vid: 1688855042791155\n"
                "  vid_has_logined: true\n"
                ">"
            ),
            "source": "GrandProfileService",
        }
    ]
    assert _extract_display_name(pairs, vid="1688855042791155") == "范新怡"


def test_refresh_account_display_name_uses_probe_and_cache(tmp_path: Path, monkeypatch):
    import app.broadcast.account as account_mod

    monkeypatch.setattr(account_mod, "resolve_path", lambda p: tmp_path / Path(p))
    monkeypatch.setattr(
        account_mod,
        "probe_account_identity",
        lambda **_kwargs: {"account_id": "1688855042791155", "display_name": "范新怡"},
    )

    name = account_mod.refresh_account_display_name("1688855042791155")
    assert name == "范新怡"
    assert account_mod.load_account_aliases()["1688855042791155"] == "范新怡"
    assert account_mod.format_account_label("1688855042791155") == "1688855042791155（范新怡）"


def test_get_current_account_id_uses_probe_json():
    payload = {"account_id": "1234567890", "vid": "1234567890", "gid": "20001"}

    def fake_run(_cmd, **_kwargs):
        return CompletedProcess(args=["python"], returncode=0, stdout=json.dumps(payload) + "\n", stderr="")

    assert get_current_account_id(run_fn=fake_run) == "1234567890"
    assert resolve_account_id("manual_A") == "manual_A"


def test_probe_account_identity_decodes_gbk_stdout():
    import app.broadcast.account as account_mod

    payload = {"account_id": "1688855042791155", "display_name": "范新怡"}
    stdout_bytes = (json.dumps(payload, ensure_ascii=False) + "\n").encode("gbk")

    def fake_run(_cmd, **_kwargs):
        return CompletedProcess(args=["python"], returncode=0, stdout=stdout_bytes, stderr=b"")

    result = account_mod.probe_account_identity(account_id="1688855042791155", run_fn=fake_run)
    assert result["account_id"] == "1688855042791155"
    assert result["display_name"] == "范新怡"


def test_switch_account_can_skip_data_root_creation(tmp_path: Path, monkeypatch):
    def fake_resolve_path(p: str | Path) -> Path:
        return tmp_path / Path(p)

    monkeypatch.setattr(paths, "resolve_path", fake_resolve_path)
    monkeypatch.setattr("app.broadcast.account.data_root_for", paths.data_root_for)
    account_id = switch_account("vid_init", ensure_data_root=False)
    assert account_id == "vid_init"
    assert not (tmp_path / "data" / "vid_init").exists()


def test_decode_proto_escaped_gb18030() -> None:
    from runtime.wework_re_android.probe_account_identity import _decode_proto_escaped

    encoded = "范新怡".encode("gb18030")
    escaped = "".join(f"\\{b:03o}" for b in encoded)
    assert _decode_proto_escaped(escaped) == "范新怡"


def test_save_account_alias_rejects_dirty(tmp_path: Path, monkeypatch) -> None:
    import app.broadcast.account as account_mod

    monkeypatch.setattr(account_mod, "resolve_path", lambda p: tmp_path / Path(p))
    assert save_account_alias("1688855042791155", "\ufffd\ufffd") is None
    assert account_mod.load_account_aliases() == {}


def test_read_vid_prefers_latest_entry() -> None:
    import app.broadcast.account as account_mod

    def fake_run(_cmd, **_kwargs):
        class _Proc:
            returncode = 0
            stdout = "1111111111111_a.xml\n2222222222222_b.xml\n"

        return _Proc()

    assert account_mod.read_vid_from_wework_prefs(run_fn=fake_run) == "1111111111111"
