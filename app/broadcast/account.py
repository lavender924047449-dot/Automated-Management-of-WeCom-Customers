"""Step 12: 多账号身份识别与数据根路径。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.broadcast.paths import DEFAULT_ACCOUNT_ID, data_root_for, normalize_account_id
from app.config import resolve_path

DEFAULT_ACCOUNT_ENV = "TESTVOICE_ACCOUNT_ID"
DEFAULT_IDENTITY_PROBE_MODULE = "runtime.wework_re_android.probe_account_identity"
LAST_ACTIVE_ACCOUNT_PATH = Path("data") / "last_active_account.json"
ACCOUNT_ALIAS_PATH = Path("data") / "account_aliases.json"
DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
WEWORK_PREFS_DIR = "/data/data/com.tencent.wework/shared_prefs"
WEWORK_USER_INFO_XML = f"{WEWORK_PREFS_DIR}/user_info.xml"
WEWORK_FILECACHE_DIR = "/storage/emulated/0/Android/data/com.tencent.wework/files/filecache"
_VID_PREFIX_RE = re.compile(r"^(\d{13,})_")
_PROMOTE_MAIL_RE = re.compile(r"promote_mail_(\d{13,})")


def _resolve_adb_binary() -> str:
    """优先使用 bootstrap 记录的 adb 路径。"""
    return os.environ.get("TESTVOICE_ADB_BINARY", "") or "adb"


def _adb_shell(
    shell_cmd: str,
    *,
    adb_serial: str,
    run_fn,
    timeout_seconds: float,
) -> str:
    adb = _resolve_adb_binary()
    cmd = [adb, "-s", adb_serial, "shell", shell_cmd]
    try:
        proc = run_fn(cmd, capture_output=True, text=True, check=False, timeout=timeout_seconds)
    except Exception:
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return proc.stdout or ""


def _pick_most_common(counter: dict[str, int]) -> str:
    if not counter:
        return ""
    return max(counter.items(), key=lambda kv: kv[1])[0]


def read_vid_from_wework_prefs(
    *,
    adb_serial: str = DEFAULT_ADB_SERIAL,
    run_fn=subprocess.run,
    timeout_seconds: float = 6.0,
) -> str:
    """通过 adb 读取当前登录 vid，不依赖 Frida。

    依次尝试：
      1. ``shared_prefs`` 里 ``<vid>_*.xml`` 文件名（老安装常见）。
      2. ``user_info.xml`` 里的 ``promote_mail_<vid>``（新装企微 5.0.10 常见）。
      3. ``filecache/<vid>`` 目录名。
    只要 adb 有 root 读这些路径即可。
    """
    kwargs = {
        "adb_serial": adb_serial,
        "run_fn": run_fn,
        "timeout_seconds": timeout_seconds,
    }

    counter: dict[str, int] = {}
    for name in _adb_shell(f"ls {WEWORK_PREFS_DIR} 2>/dev/null", **kwargs).splitlines():
        match = _VID_PREFIX_RE.match(name.strip())
        if not match:
            continue
        vid = match.group(1)
        counter[vid] = counter.get(vid, 0) + 1
    picked = _pick_most_common(counter)
    if picked:
        return picked

    xml = _adb_shell(f"cat {WEWORK_USER_INFO_XML} 2>/dev/null", **kwargs)
    mail_hits = _PROMOTE_MAIL_RE.findall(xml)
    if mail_hits:
        return mail_hits[0]

    cache_counter: dict[str, int] = {}
    for name in _adb_shell(f"ls {WEWORK_FILECACHE_DIR} 2>/dev/null", **kwargs).splitlines():
        vid = name.strip().rstrip("/")
        if re.fullmatch(r"\d{13,}", vid):
            cache_counter[vid] = cache_counter.get(vid, 0) + 1
    return _pick_most_common(cache_counter)


def get_current_account_id(
    *,
    python_executable: str | None = None,
    probe_module: str = DEFAULT_IDENTITY_PROBE_MODULE,
    timeout_seconds: float = 8.0,
    run_fn=subprocess.run,
    adb_serial: str = DEFAULT_ADB_SERIAL,
    use_env: bool = True,
    allow_frida_fallback: bool = True,
) -> str:
    """读取当前登录账号 id。

    解析顺序：
      1. 环境变量 ``TESTVOICE_ACCOUNT_ID``（GUI 手动切换 / 测试注入）——
         传 ``use_env=False`` 可跳过，用于 GUI"主动重新探测"场景，避免
         被 :func:`switch_account` 早前写入的缓存短路。
      2. adb 读取企微 shared_prefs 文件名前缀（最稳定，无需 Frida）。
      3. 回退调用 Frida 探针 ``probe_account_identity --once``。
    全部失败返回 ``DEFAULT_ACCOUNT_ID``。
    """

    if use_env:
        env_value = os.environ.get(DEFAULT_ACCOUNT_ENV, "").strip()
        if env_value and normalize_account_id(env_value) != DEFAULT_ACCOUNT_ID:
            # 只在缓存到真实 vid 时短路；缓存是 "default" 时视为未初始化，继续走探测。
            return normalize_account_id(env_value)

    # 主路径：adb 直读 shared_prefs
    vid = read_vid_from_wework_prefs(adb_serial=adb_serial, run_fn=run_fn)
    if vid:
        return normalize_account_id(vid)

    if not allow_frida_fallback:
        return DEFAULT_ACCOUNT_ID

    # 兜底：Frida 探针
    identity = probe_account_identity(
        python_executable=python_executable,
        probe_module=probe_module,
        timeout_seconds=timeout_seconds,
        run_fn=run_fn,
    )
    return identity["account_id"] or DEFAULT_ACCOUNT_ID


def probe_account_identity(
    *,
    account_id: str = "",
    python_executable: str | None = None,
    probe_module: str = DEFAULT_IDENTITY_PROBE_MODULE,
    timeout_seconds: float = 8.0,
    run_fn=subprocess.run,
) -> dict[str, str]:
    """调用 Frida 探针，读取当前账号与可能的个人名。"""
    # 走 build_python_module_cmd：frozen 下会转换成 `<exe> __runmod__ <module>` 走
    # frozen_dispatch，避免拉起新 GUI 实例。python_executable 参数仅在 dev/测试
    # 中生效；frozen 下 sys.executable 就是 exe 本身，无法用外部 python 覆盖。
    from app.bootstrap.frozen_dispatch import build_python_module_cmd

    extra_args = ["--once", "--timeout-seconds", str(timeout_seconds)]
    normalized = normalize_account_id(account_id)
    if normalized and normalized != DEFAULT_ACCOUNT_ID:
        extra_args.extend(["--account-id", normalized])

    if python_executable and not bool(getattr(sys, "frozen", False)):
        cmd = [python_executable, "-m", probe_module, *extra_args]
    else:
        cmd = build_python_module_cmd(probe_module, *extra_args)

    def _decode_stdout(raw: str | bytes | None) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        # Windows 下子进程 stdout 可能是 GBK/GB18030（取决于控制台 codepage），
        # 这里按 UTF-8 优先，失败后回退到中文编码，避免出现 U+FFFD 污染昵称。
        for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    try:
        proc = run_fn(
            cmd,
            capture_output=True,
            text=False,
            check=False,
        )
    except Exception:
        return {"account_id": DEFAULT_ACCOUNT_ID, "display_name": ""}
    if proc.returncode not in (0, 2):
        return {"account_id": DEFAULT_ACCOUNT_ID, "display_name": ""}

    # 约定 probe 最后一行输出 JSON：{"account_id":"...", "vid":"...", "gid":"...", "display_name":"..."}
    stdout_text = _decode_stdout(getattr(proc, "stdout", None))
    lines = [x.strip() for x in stdout_text.splitlines() if x.strip()]
    if not lines:
        return {"account_id": DEFAULT_ACCOUNT_ID, "display_name": ""}
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"account_id": DEFAULT_ACCOUNT_ID, "display_name": ""}
    if not isinstance(payload, dict):
        return {"account_id": DEFAULT_ACCOUNT_ID, "display_name": ""}
    candidate = str(payload.get("account_id", "")).strip()
    if not candidate:
        candidate = str(payload.get("vid", "")).strip() or str(payload.get("gid", "")).strip()
    account_id = normalize_account_id(candidate)
    display_name = str(payload.get("display_name", "")).strip()
    return {"account_id": account_id, "display_name": display_name}


def resolve_account_id(account_id: str | None, **kwargs: Any) -> str:
    """优先使用显式参数，否则读取当前登录账号。"""

    value = str(account_id or "").strip()
    if value:
        return normalize_account_id(value)
    return get_current_account_id(**kwargs)


def list_accounts() -> list[str]:
    """列出现有账号目录（至少包含 default）。"""

    data_root = resolve_path(Path("data"))
    accounts: list[str] = []
    if data_root.exists():
        for child in sorted(data_root.iterdir()):
            if child.is_dir():
                accounts.append(normalize_account_id(child.name))
    if DEFAULT_ACCOUNT_ID not in accounts:
        accounts.insert(0, DEFAULT_ACCOUNT_ID)
    seen: set[str] = set()
    uniq: list[str] = []
    for account_id in accounts:
        if account_id in seen:
            continue
        seen.add(account_id)
        uniq.append(account_id)
    return uniq


def _is_sane_alias(name: str) -> bool:
    """忽略已损坏的缓存别名（常见于 proto 八进制转义解码错误）。"""
    text = str(name or "").strip()
    if not text or "\ufffd" in text:
        return False
    return any(ch.isprintable() and not ch.isspace() for ch in text)


def load_account_aliases() -> dict[str, str]:
    target = resolve_path(ACCOUNT_ALIAS_PATH)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in payload.items():
        account_id = normalize_account_id(key)
        name = str(value).strip()
        if account_id == DEFAULT_ACCOUNT_ID or not account_id or not name:
            continue
        if not _is_sane_alias(name):
            continue
        out[account_id] = name
    return out


def save_account_alias(account_id: str, display_name: str) -> Path | None:
    normalized = normalize_account_id(account_id)
    name = str(display_name).strip()
    if not normalized or normalized == DEFAULT_ACCOUNT_ID or not name:
        return None
    if not _is_sane_alias(name):
        return None
    aliases = load_account_aliases()
    aliases[normalized] = name
    target = resolve_path(ACCOUNT_ALIAS_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def refresh_account_display_name(
    account_id: str,
    *,
    python_executable: str | None = None,
    probe_module: str = DEFAULT_IDENTITY_PROBE_MODULE,
    timeout_seconds: float = 8.0,
    run_fn=subprocess.run,
) -> str:
    """刷新并缓存指定账号的个人显示名（优先 Frida 探针）。"""
    normalized = normalize_account_id(account_id)
    if not normalized or normalized == DEFAULT_ACCOUNT_ID:
        return ""

    aliases = load_account_aliases()
    cached = str(aliases.get(normalized, "")).strip()
    if cached:
        return cached

    identity = probe_account_identity(
        account_id=normalized,
        python_executable=python_executable,
        probe_module=probe_module,
        timeout_seconds=timeout_seconds,
        run_fn=run_fn,
    )
    display_name = str(identity.get("display_name", "")).strip()
    if display_name:
        save_account_alias(normalized, display_name)
        return display_name
    return ""


def format_account_label(account_id: str, aliases: dict[str, str] | None = None) -> str:
    normalized = normalize_account_id(account_id)
    if not normalized:
        return DEFAULT_ACCOUNT_ID
    name_map = aliases if aliases is not None else load_account_aliases()
    display_name = str(name_map.get(normalized, "")).strip()
    if display_name:
        return f"{normalized}（{display_name}）"
    return normalized


def switch_account(account_id: str, *, ensure_data_root: bool = True) -> str:
    """切换 GUI 当前账号上下文（环境变量）。"""

    normalized = normalize_account_id(account_id)
    os.environ[DEFAULT_ACCOUNT_ENV] = normalized
    if ensure_data_root:
        data_root_for(normalized).mkdir(parents=True, exist_ok=True)
    return normalized


def load_last_active_account() -> str:
    target = resolve_path(LAST_ACTIVE_ACCOUNT_PATH)
    if not target.exists():
        return ""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return normalize_account_id(payload.get("account_id", ""))


def save_last_active_account(account_id: str) -> Path:
    normalized = normalize_account_id(account_id)
    target = resolve_path(LAST_ACTIVE_ACCOUNT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"account_id": normalized}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


__all__ = [
    "ACCOUNT_ALIAS_PATH",
    "DEFAULT_ACCOUNT_ENV",
    "data_root_for",
    "format_account_label",
    "get_current_account_id",
    "list_accounts",
    "load_account_aliases",
    "read_vid_from_wework_prefs",
    "resolve_account_id",
    "probe_account_identity",
    "refresh_account_display_name",
    "save_account_alias",
    "load_last_active_account",
    "save_last_active_account",
    "switch_account",
]
