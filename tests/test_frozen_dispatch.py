from __future__ import annotations

from app.bootstrap.frozen_dispatch import maybe_dispatch_frozen_subcommand


def test_maybe_dispatch_skips_normal_args():
    assert maybe_dispatch_frozen_subcommand(["prog", "--help"]) is None
    assert maybe_dispatch_frozen_subcommand(["prog"]) is None


def test_maybe_dispatch_forwards_send_engine_args(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_main(argv: list[str]) -> int:
        captured["argv"] = list(argv)
        return 7

    monkeypatch.setattr("runtime.wework_re_android.send_engine.main", _fake_main)
    ret = maybe_dispatch_frozen_subcommand(["prog", "__send_engine__", "text", "--conv-local", "1001"])
    assert ret == 7
    assert captured["argv"] == ["text", "--conv-local", "1001"]
