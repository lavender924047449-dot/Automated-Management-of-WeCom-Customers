"""adb 序列号在 TCP 口掉线后的选择。"""

from __future__ import annotations

from app.bootstrap.runtime_bootstrap import ensure_adb_serial, parse_adb_devices


def test_parse_adb_devices_ignores_header_and_keeps_state() -> None:
    text = """
List of devices attached
127.0.0.1:16384        device product:Draco model:DCO_AL00
emulator-5554          offline
"""
    assert parse_adb_devices(text) == [
        ("127.0.0.1:16384", "device"),
        ("emulator-5554", "offline"),
    ]


def test_ensure_prefers_configured_serial_when_online() -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], _timeout: float) -> tuple[int, str]:
        calls.append(cmd)
        return 0, "List of devices attached\n127.0.0.1:16384\tdevice\nemulator-5554\tdevice\n"

    serial = ensure_adb_serial("adb", "127.0.0.1:16384", run_fn=run, attempts=1)
    assert serial == "127.0.0.1:16384"
    assert calls == [["adb", "devices"]]


def test_ensure_connects_when_only_emulator_transport_is_online() -> None:
    seen = {"connected": False}

    def run(cmd: list[str], _timeout: float) -> tuple[int, str]:
        if cmd[1:] == ["devices"]:
            if seen["connected"]:
                return 0, "List of devices attached\n127.0.0.1:16384\tdevice\nemulator-5554\tdevice\n"
            return 0, "List of devices attached\nemulator-5554\tdevice\n"
        if cmd[1:] == ["connect", "127.0.0.1:16384"]:
            seen["connected"] = True
            return 0, "connected to 127.0.0.1:16384"
        raise AssertionError(cmd)

    serial = ensure_adb_serial("adb", "127.0.0.1:16384", run_fn=run, attempts=1)
    assert serial == "127.0.0.1:16384"
    assert seen["connected"] is True


def test_ensure_falls_back_to_emulator_when_tcp_port_stays_down() -> None:
    def run(cmd: list[str], _timeout: float) -> tuple[int, str]:
        if cmd[1:] == ["devices"]:
            return 0, "List of devices attached\nemulator-5554\tdevice\n"
        if cmd[1:] == ["connect", "127.0.0.1:16384"]:
            return 0, "cannot connect to 127.0.0.1:16384"
        raise AssertionError(cmd)

    serial = ensure_adb_serial("adb", "127.0.0.1:16384", run_fn=run, attempts=1)
    assert serial == "emulator-5554"


def test_ensure_returns_empty_when_nothing_is_online() -> None:
    def run(cmd: list[str], _timeout: float) -> tuple[int, str]:
        if cmd[1:] == ["devices"]:
            return 0, "List of devices attached\n"
        if cmd[1:] == ["connect", "127.0.0.1:16384"]:
            return 1, "cannot connect"
        raise AssertionError(cmd)

    assert ensure_adb_serial("adb", "127.0.0.1:16384", run_fn=run, attempts=1) == ""
