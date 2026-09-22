"""Step 17: 打包后运行时自检（adb/frida-server）。"""

from __future__ import annotations

import os
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


def bootstrap_android_runtime(adb_serial: str = DEFAULT_ADB_SERIAL) -> list[str]:
    """尝试 connect / adb root / push / start / forward；失败返回告警字符串列表。"""
    warnings: list[str] = []
    adb = resolve_adb_binary()

    try:
        code, out = _run([adb, "connect", adb_serial], timeout_sec=10.0)
    except Exception as exc:  # pragma: no cover - 依赖外部环境
        return [f"adb connect 执行失败: {exc}"]
    if code != 0:
        warnings.append(f"adb connect 失败: {out or 'unknown error'}")
        return warnings

    # 提权到 root（MuMu 12 支持 `adb root`；失败仅告警，后续 push/frida 仍可能奏效）。
    try:
        _run([adb, "-s", adb_serial, "root"], timeout_sec=8.0)
    except Exception:
        pass
    # `adb root` 会断开一次连接，重新 connect 保证后续命令可用。
    try:
        _run([adb, "connect", adb_serial], timeout_sec=6.0)
    except Exception:
        pass

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


__all__ = ["bootstrap_android_runtime", "resolve_adb_binary", "ADB_ENV_KEY", "DEFAULT_ADB_SERIAL"]
