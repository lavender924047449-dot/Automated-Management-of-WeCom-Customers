"""Windows Job Object：把当前进程和所有子孙进程绑一起，父死子亡。

背景
-----
本程序会 spawn 大量子进程（frozen 下走 ``__runmod__`` 分发的 exe 实例、adb.exe、
QProcess wxa 采集、CaptureClient 的 Popen 等）。Windows 默认没有「父死子亡」，
主 exe 关掉后，只要还有子进程持有资源（比如打开着日志文件），dist 目录就
删不掉，托盘退出也感觉「没退干净」。

方案
-----
启动时创建一个 Job Object，把当前进程 assign 进去，并设置
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``：一旦 job handle 关闭（主进程死了），
kernel 立即 kill 所有在 job 里的进程。因为子进程默认继承 job，所有
subprocess.run / QProcess spawn 出来的进程都会被囊括。

同时设置 ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``。普通子进程仍然死在父进程后面；
目录分发的更新器会带 ``CREATE_BREAKAWAY_FROM_JOB`` 脱离这个 job，这样主程序
退出后它还能覆盖「程序」并重新打开。

非 Windows / frozen=False / 已在别的 job 里 —— 全部静默跳过，不影响 dev。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# 让 loguru 可选（导入时不能崩，即便日志系统还没起来）
try:
    from loguru import logger
except Exception:  # pragma: no cover
    logger = None  # type: ignore[assignment]


_JOB_HANDLE: int | None = None  # 保活：句柄不能被 gc


# ---- WinAPI 常量 ----
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def bind_kill_on_close() -> bool:
    """把当前进程放入一个「父死子亡」Job Object。幂等且失败静默。

    返回 True 表示已绑定（或已经绑定过），False 表示未绑定（非 Windows 或失败）。
    """
    global _JOB_HANDLE
    if sys.platform != "win32":
        return False
    if _JOB_HANDLE is not None:
        return True

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # CreateJobObjectW(NULL, NULL) → 匿名 job
        create_job = kernel32.CreateJobObjectW
        create_job.restype = wintypes.HANDLE
        create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        h_job = create_job(None, None)
        if not h_job:
            _log("warning", "CreateJobObjectW 失败 err={}", ctypes.get_last_error())
            return False

        # 配置 KILL_ON_JOB_CLOSE
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )

        set_info = kernel32.SetInformationJobObject
        set_info.restype = wintypes.BOOL
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        ok = set_info(
            h_job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _log("warning", "SetInformationJobObject 失败 err={}", ctypes.get_last_error())
            kernel32.CloseHandle(h_job)
            return False

        # 把当前进程 assign 进去
        assign = kernel32.AssignProcessToJobObject
        assign.restype = wintypes.BOOL
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        cur_proc = kernel32.GetCurrentProcess()
        ok = assign(h_job, cur_proc)
        if not ok:
            err = ctypes.get_last_error()
            # ERROR_ACCESS_DENIED (5) 常见于「进程已经在别的 job 里且不允许嵌套」
            # 现代 Windows 10+ 支持嵌套 job（Win8+），这里失败大多是 sandbox / CI。
            _log("warning", "AssignProcessToJobObject 失败 err={}", err)
            kernel32.CloseHandle(h_job)
            return False

        _JOB_HANDLE = h_job  # 持有句柄；进程死了 kernel 自动 close → kill 全家
        _log("info", "Job Object bound (kill-on-close), handle=0x{:x}", h_job)
        return True
    except Exception as exc:  # pragma: no cover
        _log("warning", "bind_kill_on_close 异常: {}", exc)
        return False


def _log(level: str, fmt: str, *args) -> None:
    if logger is None:
        return
    try:
        getattr(logger, level)(fmt, *args)
    except Exception:
        pass


__all__ = ["bind_kill_on_close"]
