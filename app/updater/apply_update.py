"""把 GitHub Release 上的 program-update.zip 应用到目录分发安装。

压缩包根目录只允许两样东西：

- ``程序/``：界面和发送代码，整目录替换
- ``pyproject.toml``：版本号

``data``、``vendor``、``python`` 和 ``启动.exe`` 不动。

主程序还在运行时不能安全改掉自己的代码目录，而且进程绑了
「父死子亡」Job Object。下载完成后会另起一个脱离该 job 的 pythonw，
等主程序退出，再替换文件并重新打开 ``启动.exe``。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.updater.version_check import _normalize_version

PROGRAM_DIR_NAME = "程序"
LAUNCHER_NAME = "启动.exe"
STAGING_DIR_NAME = ".update-staging"
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
_ALLOWED_TOP_LEVEL = {PROGRAM_DIR_NAME, "pyproject.toml"}


class UpdateError(Exception):
    """更新包无法下载或无法安全覆盖。"""


def portable_install_root() -> Path | None:
    """目录分发安装根目录。开发态和 PyInstaller 单文件包返回 None。"""

    if bool(getattr(sys, "frozen", False)):
        return None
    home_env = os.environ.get("TESTVOICE_HOME", "").strip()
    if not home_env:
        return None
    home = Path(home_env).expanduser().resolve()
    pythonw = home / "python" / "pythonw.exe"
    program = home / PROGRAM_DIR_NAME
    if pythonw.is_file() and program.is_dir():
        return home
    return None


def _url_allowed(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com"):
        return True
    return host == "githubusercontent.com" or host.endswith(".githubusercontent.com")


class _GuardedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not _url_allowed(newurl):
            raise UpdateError(f"更新地址不在允许的范围内：{newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_update(
    url: str,
    dest: Path,
    *,
    timeout_seconds: float = 120,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """下载更新包。只接受 GitHub 及其发布文件域名。"""

    if not _url_allowed(url):
        raise UpdateError(f"更新地址不在允许的范围内：{url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = Request(
        url,
        headers={
            "User-Agent": "test-voice-updater",
            "Accept": "application/octet-stream",
        },
    )
    opener = build_opener(_GuardedRedirect)
    done = 0
    try:
        with opener.open(request, timeout=float(timeout_seconds)) as resp, tmp.open("wb") as handle:
            total = int(resp.headers.get("Content-Length") or 0)
            if total > MAX_DOWNLOAD_BYTES:
                raise UpdateError("更新包过大")
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_DOWNLOAD_BYTES:
                    raise UpdateError("更新包过大")
                handle.write(chunk)
                if on_progress is not None:
                    on_progress(done, total)
        if done == 0:
            raise UpdateError("下载到的更新包是空的")
        tmp.replace(dest)
    except UpdateError:
        tmp.unlink(missing_ok=True)
        raise
    except HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"下载更新失败：HTTP {exc.code}") from exc
    except URLError as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"下载更新失败：{exc.reason}") from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"下载更新失败：{exc}") from exc


def _member_parts(name: str) -> tuple[str, ...]:
    text = str(name or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise UpdateError(f"更新包路径不安全：{name}")
    parts = tuple(part for part in text.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise UpdateError(f"更新包路径不安全：{name}")
    return parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def _check_archive(zf: zipfile.ZipFile, *, expected_version: str) -> None:
    infos = zf.infolist()
    uncompressed = sum(info.file_size for info in infos)
    if uncompressed > MAX_UNCOMPRESSED_BYTES:
        raise UpdateError("更新包解压后过大")
    saw_program_file = False
    saw_pyproject = False
    for info in infos:
        if _is_symlink(info):
            raise UpdateError(f"更新包包含链接：{info.filename}")
        parts = _member_parts(info.filename)
        if parts[0] not in _ALLOWED_TOP_LEVEL:
            raise UpdateError(f"更新包包含不允许的内容：{parts[0]}")
        is_dir = info.is_dir() or info.filename.endswith("/")
        if parts == ("pyproject.toml",) and not is_dir:
            saw_pyproject = True
        if parts[0] == PROGRAM_DIR_NAME and not is_dir:
            saw_program_file = True
    if not saw_program_file:
        raise UpdateError("更新包里没有「程序」目录")
    if not saw_pyproject:
        raise UpdateError("更新包缺少 pyproject.toml")
    version = _normalize_version(read_pyproject_version_from_zip(zf))
    expected = _normalize_version(expected_version)
    if not version or version != expected:
        raise UpdateError(
            f"更新包版本是 {version or '未知'}，与发布版本 {expected or '未知'} 不一致"
        )


def read_pyproject_version_from_zip(zf: zipfile.ZipFile) -> str:
    try:
        raw = zf.read("pyproject.toml")
    except KeyError as exc:
        raise UpdateError("更新包缺少 pyproject.toml") from exc
    text = raw.decode("utf-8", errors="ignore")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', text)
    return str(match.group(1)).strip() if match else ""


def validate_update_archive(zip_path: Path, *, expected_version: str) -> None:
    if not zip_path.is_file():
        raise UpdateError("找不到更新包")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _check_archive(zf, expected_version=expected_version)
    except zipfile.BadZipFile as exc:
        raise UpdateError("更新包已损坏") from exc


def extract_update_archive(zip_path: Path, dest: Path, *, expected_version: str) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _check_archive(zf, expected_version=expected_version)
            for info in zf.infolist():
                parts = _member_parts(info.filename)
                target = dest.joinpath(*parts)
                if info.is_dir() or info.filename.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _rename_with_retry(src: Path, dst: Path, *, attempts: int = 8) -> None:
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            src.rename(dst)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.4 * (index + 1))
    raise UpdateError(f"无法移动 {src.name}：{last_error}")


def apply_extracted_update(install_root: Path, extracted: Path) -> None:
    """用解压结果替换「程序」和 pyproject.toml。失败时把旧「程序」移回去。"""

    program_src = extracted / PROGRAM_DIR_NAME
    pyproject_src = extracted / "pyproject.toml"
    if not program_src.is_dir():
        raise UpdateError("更新包里没有「程序」目录")
    if not pyproject_src.is_file():
        raise UpdateError("更新包缺少 pyproject.toml")

    target = install_root / PROGRAM_DIR_NAME
    staging = install_root / STAGING_DIR_NAME
    staging.mkdir(parents=True, exist_ok=True)
    backup = staging / f"{PROGRAM_DIR_NAME}.bak"
    if backup.exists():
        shutil.rmtree(backup)

    moved = False
    try:
        if target.exists():
            _rename_with_retry(target, backup)
            moved = True
        shutil.copytree(program_src, target)
        shutil.copy2(pyproject_src, install_root / "pyproject.toml")
    except Exception:
        if moved and backup.exists():
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if not target.exists():
                backup.rename(target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def wait_for_exit(pid: int, timeout_seconds: float = 120) -> bool:
    if pid <= 0:
        return True
    if sys.platform == "win32":
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(synchronize, False, int(pid))
        if not handle:
            return True
        wait_timeout = 0x00000102
        result = kernel32.WaitForSingleObject(handle, int(timeout_seconds * 1000))
        kernel32.CloseHandle(handle)
        return result != wait_timeout
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def _log(install_root: Path, message: str) -> None:
    try:
        log_dir = install_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (log_dir / "update.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        return


def _notify(title: str, text: str) -> None:
    if sys.platform != "win32":
        return
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)


def spawn_update_helper(install_root: Path, zip_path: Path, *, expected_version: str) -> int:
    """启动脱离 Job Object 的更新进程。返回该进程 pid。"""

    pythonw = install_root / "python" / "pythonw.exe"
    launcher = install_root / LAUNCHER_NAME
    if not pythonw.is_file():
        raise UpdateError("未找到 python\\pythonw.exe")
    if not launcher.is_file():
        raise UpdateError("未找到启动.exe")
    if not zip_path.is_file():
        raise UpdateError("找不到更新包")
    command = [
        str(pythonw),
        "-m",
        "app.updater.apply_update",
        "--wait-pid",
        str(os.getpid()),
        "--home",
        str(install_root),
        "--zip",
        str(zip_path),
        "--launcher",
        str(launcher),
        "--expected-version",
        expected_version,
    ]
    env = os.environ.copy()
    env["TESTVOICE_HOME"] = str(install_root)
    kwargs: dict[str, object] = {
        "cwd": str(install_root),
        "env": env,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_BREAKAWAY_FROM_JOB
    process = subprocess.Popen(command, **kwargs)  # noqa: S603 - fixed local interpreter and module
    return int(process.pid)


def _launch(launcher: Path, install_root: Path) -> None:
    if not launcher.is_file():
        raise UpdateError("文件已更新，但找不到启动.exe，请手动重新打开。")
    subprocess.Popen(  # noqa: S603 - launcher path comes from the install root
        [str(launcher)],
        cwd=str(install_root),
        close_fds=True,
    )


def _run_helper(args: argparse.Namespace) -> None:
    home = Path(args.home).expanduser().resolve()
    archive = Path(args.zip).expanduser().resolve()
    launcher = Path(args.launcher).expanduser().resolve()
    _log(home, f"等待进程 {args.wait_pid} 退出")
    if not wait_for_exit(int(args.wait_pid)):
        raise UpdateError("等待程序退出超时，没有覆盖文件。")
    if int(args.wait_pid) > 0:
        time.sleep(0.8)
    extracted = home / STAGING_DIR_NAME / "extracted"
    _log(home, f"解压 {archive}")
    extract_update_archive(archive, extracted, expected_version=str(args.expected_version))
    _log(home, "覆盖程序目录")
    apply_extracted_update(home, extracted)
    _log(home, "重新打开")
    try:
        _launch(launcher, home)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError(f"文件已更新，但重新打开失败，请手动双击启动.exe。\n{exc}") from exc
    shutil.rmtree(extracted, ignore_errors=True)
    archive.unlink(missing_ok=True)
    _log(home, "更新完成")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="等待主程序退出后覆盖「程序」并重新打开")
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    home = Path(args.home)
    try:
        _run_helper(args)
    except Exception as exc:
        _log(home, f"更新失败: {exc}")
        _notify("更新失败", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
