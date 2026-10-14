"""Step 17: 打包后运行时自检（adb/frida-server）。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from app.config import bundled_root, project_root
from app.bootstrap.win_subprocess import run_hidden

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
ADB_ENV_KEY = "TESTVOICE_ADB_BINARY"
FRIDA_SERVER_LOCAL_REL = "vendor/frida-server-16.7.19-android-x86_64"
ADB_LOCAL_REL = "vendor/adb.exe"
FRIDA_SERVER_REMOTE_PATH = "/data/local/tmp/frida-server"


def _run(cmd: list[str], timeout_sec: float = 15.0) -> tuple[int, str]:
    proc = run_hidden(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_sec,
    )
    merged = f"{proc.stdout}\n{proc.stderr}".strip()
    return proc.returncode, merged


def _resolve_vendor_file(rel_path: str) -> Path | None:
    rel = Path(rel_path)
    for root in (project_root(), bundled_root()):
        cand = (root / rel).resolve()
        if cand.is_file():
            return cand
    return None


def resolve_adb_binary() -> str:
    adb_file = _resolve_vendor_file(ADB_LOCAL_REL)
    if adb_file is not None:
        os.environ[ADB_ENV_KEY] = str(adb_file)
        return str(adb_file)
    os.environ.pop(ADB_ENV_KEY, None)
    return "adb"


_AdbRunner = Callable[[list[str], float], tuple[int, str]]
_ONLINE_STATES = {"device"}
_KNOWN_STATES = {"device", "offline", "unauthorized", "authorizing"}


def parse_adb_devices(text: str) -> list[tuple[str, str]]:
    """解析 ``adb devices`` 输出，返回 ``(serial, state)``。"""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state not in _KNOWN_STATES:
            continue
        rows.append((serial, state))
    return rows


def ensure_adb_serial(
    adb: str,
    preferred: str,
    *,
    run_fn: _AdbRunner | None = None,
    attempts: int = 3,
    pause_seconds: float = 0.4,
) -> str:
    """返回当前在线的 adb 序列号。

    MuMu 的 TCP 口（默认 ``127.0.0.1:16384``）在 ``adb root`` 或 adb server
    被关掉之后会从设备列表消失，只剩下 ``emulator-5554``。采集如果仍按旧
    序列号执行 ``adb -s``，会直接报 ``device not found`` 并退出。

    优先使用 ``preferred``：不在线就先 ``adb connect``。仍然没有时，改用
    其它已经处于 device 状态的序列号。
    """
    runner = run_fn or (lambda cmd, timeout: _run(cmd, timeout_sec=timeout))
    preferred = preferred.strip()
    total = max(1, int(attempts))

    def online() -> list[str]:
        _code, out = runner([adb, "devices"], 8.0)
        return [serial for serial, state in parse_adb_devices(out) if state in _ONLINE_STATES]

    for index in range(total):
        devices = online()
        if preferred and preferred in devices:
            return preferred
        if preferred:
            runner([adb, "connect", preferred], 8.0)
            devices = online()
            if preferred in devices:
                return preferred
        fallbacks = [serial for serial in devices if serial != preferred]
        emulators = [serial for serial in fallbacks if serial.startswith("emulator-")]
        if emulators:
            return emulators[0]
        if fallbacks:
            return fallbacks[0]
        if index + 1 < total and pause_seconds > 0:
            time.sleep(pause_seconds)
    return ""


def ensure_frida_forward(
    adb: str,
    adb_serial: str,
    *,
    local_port: int = 27042,
    run_fn: _AdbRunner | None = None,
) -> tuple[int, str]:
    """把本机 frida 端口转到当前在线设备。设备重连后旧 forward 会失效。"""
    runner = run_fn or (lambda cmd, timeout: _run(cmd, timeout_sec=timeout))
    port = str(int(local_port))
    return runner([adb, "-s", adb_serial, "forward", f"tcp:{port}", f"tcp:{port}"], 6.0)


def bootstrap_android_runtime(adb_serial: str = DEFAULT_ADB_SERIAL) -> list[str]:
    """尝试 connect / adb root / push / start / forward；失败返回告警字符串列表。"""
    warnings: list[str] = []
    adb = resolve_adb_binary()

    try:
        serial = ensure_adb_serial(adb, adb_serial)
    except Exception as exc:  # pragma: no cover - 依赖外部环境
        return [f"adb connect 执行失败: {exc}"]
    if not serial:
        warnings.append(f"adb 未发现在线设备（期望 {adb_serial}）")
        return warnings
    if serial != adb_serial:
        warnings.append(f"adb 序列号 {adb_serial} 不在线，改用 {serial}")
    adb_serial = serial

    # 提权到 root（MuMu 12 支持 `adb root`；失败仅告警，后续 push/frida 仍可能奏效）。
    # root 会重启 adbd，TCP 序列号经常因此掉线，下面会再确认一次。
    try:
        _run([adb, "-s", adb_serial, "root"], timeout_sec=8.0)
    except Exception:
        pass
    try:
        serial = ensure_adb_serial(adb, adb_serial, attempts=4, pause_seconds=0.5)
    except Exception:
        serial = ""
    if not serial:
        warnings.append("adb root 后设备掉线")
        return warnings
    if serial != adb_serial:
        warnings.append(f"adb root 后改用序列号 {serial}")
        adb_serial = serial

    # 打通本机 → 设备的 frida-server 端口转发，`add_remote_device("127.0.0.1:27042")` 依赖它。
    code, out = _run([adb, "-s", adb_serial, "forward", "tcp:27042", "tcp:27042"], timeout_sec=6.0)
    if code != 0:
        warnings.append(f"adb forward tcp:27042 失败: {out or 'unknown error'}")

    frida_file = _resolve_vendor_file(FRIDA_SERVER_LOCAL_REL)
    if frida_file is None:
        warnings.append(f"未找到 {FRIDA_SERVER_LOCAL_REL}，跳过 push；改为直接尝试启动设备上已有 frida-server")
    else:
        code, out = _run(
            [adb, "-s", adb_serial, "push", str(frida_file), FRIDA_SERVER_REMOTE_PATH],
            timeout_sec=20.0,
        )
        if code != 0:
            warnings.append(f"frida-server push 失败: {out or 'unknown error'}")

    # 无论 push 与否，都尝试杀掉旧 frida-server 并以当前 adb 身份（若前面 adb root 成功则为 root）拉起。
    code, out = _run(
        [
            adb,
            "-s",
            adb_serial,
            "shell",
            (
                f"chmod 755 {FRIDA_SERVER_REMOTE_PATH} 2>/dev/null; "
                "killall -9 frida-server 2>/dev/null; "
                f"nohup {FRIDA_SERVER_REMOTE_PATH} >/data/local/tmp/frida.log 2>&1 &"
            ),
        ],
        timeout_sec=12.0,
    )
    if code != 0:
        warnings.append(f"frida-server 启动命令失败: {out or 'unknown error'}")
    return warnings


__all__ = [
    "bootstrap_android_runtime",
    "resolve_adb_binary",
    "ensure_adb_serial",
    "ensure_frida_forward",
    "parse_adb_devices",
    "ADB_ENV_KEY",
    "DEFAULT_ADB_SERIAL",
]
