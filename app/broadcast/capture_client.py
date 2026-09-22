"""F2.3：FTA 采集子进程客户端。

对 :mod:`runtime.wework_re_android.fta_capture_lite` 做 subprocess 起停 +
stdout jsonl 事件路由。GUI（`library_ingest_tab.py`）不直接管子进程，而是通过
本类订阅事件，与 `runner.py` 的 subprocess 模式保持一致。

事件契约详见：``runtime/wework_re_android/notes/fta_capture_protocol.md``。

设计要点
--------
* **框架无关**：只依赖标准库；Qt 侧收到 `on_event` 回调后自行 emit signal。
* **单线程读 stdout**：daemon 后台线程按行读取，遇到半行则缓存等下次拼接
  （契约 §4.1）；`json.JSONDecodeError` 只 warning 不 fatal（契约 §4.2）；
  未知 ``t`` 交给回调层自行决定（契约 §4.3）。
* **stderr 独立线程**：诊断日志（``[fta_lite] hooked ...``）走 stderr，不混入
  事件流；client 侧默认转发到 Python logging 里，方便定位。
* **stop 语义**：SIGTERM → 3s → SIGKILL 兜底（契约 §4.5）。Windows 上
  ``subprocess.Popen.terminate`` 等价于 ``TerminateProcess``，行为一致。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, IO

logger = logging.getLogger(__name__)

# fta_capture_lite 模块导入路径；集中定义方便 F3.x 迭代时统一改动。
_LITE_MODULE = "runtime.wework_re_android.fta_capture_lite"

# stop() 从 SIGTERM 到 SIGKILL 的宽限时间；契约 §4.5 = 3s。
_STOP_GRACE_SECONDS = 3.0


EventCallback = Callable[[dict[str, Any]], None]
ExitCallback = Callable[[int], None]
StderrCallback = Callable[[str], None]


@dataclass
class CaptureClientStats:
    """诊断统计（供 GUI 或测试观察，非契约事件）。"""

    events_ok: int = 0
    events_bad_json: int = 0
    exit_code: int | None = None
    stderr_tail: list[str] = field(default_factory=list)


class CaptureClient:
    """启动 / 停止 FTA 采集子进程，并把 stdout jsonl 事件转成回调。

    Parameters
    ----------
    on_event:
        每收到一条合法 jsonl 事件时被调用；参数是解析后的 dict（含 ``t`` /
        ``sv`` / ``ts`` 等）。回调应尽量 O(μs) 返回，重活丢线程池。
    on_exit:
        子进程退出时被调用一次，参数是 ``returncode``。GUI 侧可据此复位按钮
        状态或弹错。
    on_stderr:
        可选：每行 stderr 都会被调用。默认走 :mod:`logging` INFO；显式传入
        可覆盖（例如把最后 20 行贴到 GUI 弹窗）。
    python_executable:
        默认走当前解释器；打包成 exe 时可覆盖。
    """

    def __init__(
        self,
        *,
        on_event: EventCallback,
        on_exit: ExitCallback | None = None,
        on_stderr: StderrCallback | None = None,
        python_executable: str | None = None,
        module: str = _LITE_MODULE,
        stderr_tail_size: int = 20,
    ) -> None:
        self._on_event = on_event
        self._on_exit = on_exit
        self._on_stderr = on_stderr
        self._python_executable = python_executable or sys.executable
        self._module = module
        self._stderr_tail_size = max(0, int(stderr_tail_size))

        self._proc: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stats = CaptureClientStats()

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def start(
        self,
        *,
        account_id: str = "",
        timeout_seconds: float = 0.0,
        fta_remote_id: str | None = None,
        index_path: str | Path | None = None,
        extra_args: list[str] | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        cwd: str | Path | None = None,
    ) -> subprocess.Popen[str]:
        """spawn 采集子进程并开始尾读 stdout。

        重复调用 start 之前必须先 :meth:`stop`，否则抛 ``RuntimeError``——
        避免同一账号同时挂两个 hook 把企微搞崩（决策 F5/F11）。

        Parameters
        ----------
        popen_factory:
            仅测试使用；生产走默认 :class:`subprocess.Popen`。
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("CaptureClient already running; call stop() first")
            self._stats = CaptureClientStats()

            # frozen 下 self._python_executable == exe 本身，`-m <module>` 会拉起
            # 新 GUI 实例；改走 frozen_dispatch 的 __runmod__ 通道。dev 保持 `-m`。
            # 注意 `-u`（unbuffered）在 frozen 下不需要——bootloader 已经强制无缓冲，
            # 且 `__runmod__` 是 in-process 调用而不是外部 python，不用透传该 flag。
            from app.bootstrap.frozen_dispatch import build_python_module_cmd

            module_args: list[str] = [
                "--emit-jsonl",
                "--account-id",
                account_id,
                "--timeout-seconds",
                str(float(timeout_seconds)),
            ]
            if fta_remote_id:
                module_args.extend(["--fta-remote-id", fta_remote_id])
            if index_path:
                module_args.extend(["--index-path", str(index_path)])
            if extra_args:
                module_args.extend(str(x) for x in extra_args)

            if bool(getattr(sys, "frozen", False)):
                args: list[str] = build_python_module_cmd(self._module, *module_args)
            else:
                args = [self._python_executable, "-u", "-m", self._module, *module_args]

            factory = popen_factory or subprocess.Popen
            # text=True + line-buffered 与 lite 侧 `reconfigure(line_buffering=True)`
            # 配对，跨 pipe 也能稳定拿到 '\n' 分割。errors='replace' 防止个别
            # 非 utf-8 字节把整条 pipe 拉崩（logcat/frida 偶尔混入 gbk）。
            proc = factory(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd) if cwd is not None else None,
            )
            self._proc = proc

            self._stdout_thread = threading.Thread(
                target=self._pump_stdout,
                args=(proc.stdout,),
                name="capture-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._pump_stderr,
                args=(proc.stderr,),
                name="capture-stderr",
                daemon=True,
            )
            self._wait_thread = threading.Thread(
                target=self._wait_and_notify,
                name="capture-wait",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
            self._wait_thread.start()
        return proc

    def stop(self, *, grace_seconds: float = _STOP_GRACE_SECONDS) -> int | None:
        """SIGTERM → 宽限期 → SIGKILL；返回 exit code（未启动则 None）。"""
        proc = self._proc
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception as exc:  # noqa: BLE001
                logger.warning("terminate() failed: %s", exc)
            try:
                proc.wait(timeout=max(0.0, float(grace_seconds)))
            except subprocess.TimeoutExpired:
                logger.warning("capture subprocess did not exit in %.1fs; killing", grace_seconds)
                try:
                    proc.kill()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("kill() failed: %s", exc)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    logger.error("capture subprocess still alive after kill()")
        # 让读线程有机会消费掉 pipe 里剩余的数据后再收尾
        for th in (self._stdout_thread, self._stderr_thread):
            if th is not None and th.is_alive():
                th.join(timeout=1.0)
        return proc.returncode

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def stats(self) -> CaptureClientStats:
        return self._stats

    @property
    def process(self) -> subprocess.Popen[str] | None:
        return self._proc

    # ------------------------------------------------------------------
    # 内部：pump 线程
    # ------------------------------------------------------------------
    def _pump_stdout(self, stream: IO[str] | None) -> None:
        """按行读 stdout，解析 jsonl 事件。半行会被 `readline` 自动缓冲。"""
        if stream is None:
            return
        buf = ""
        try:
            while True:
                chunk = stream.readline()
                if not chunk:
                    # EOF —— 消费 buf 里可能残留的最后一行
                    if buf.strip():
                        self._dispatch_line(buf)
                        buf = ""
                    break
                buf += chunk
                # readline 在 text 模式下正常按 '\n' 切；但网络/pipe 边界情况
                # 下偶尔会返回一段不含 '\n' 的数据（例如 Windows 上跨 buffer），
                # 因此这里做二次拆分。
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._dispatch_line(line)
        except Exception as exc:  # noqa: BLE001
            logger.exception("stdout pump crashed: %s", exc)

    def _dispatch_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self._stats.events_bad_json += 1
            logger.warning("drop invalid jsonl line: %s (err=%s)", text[:200], exc)
            return
        if not isinstance(payload, dict):
            self._stats.events_bad_json += 1
            logger.warning("drop non-object jsonl payload: %r", payload)
            return
        self._stats.events_ok += 1
        try:
            self._on_event(payload)
        except Exception as exc:  # noqa: BLE001
            # 消费方回调抛异常不影响后续事件流；仅告警。
            logger.exception("on_event callback raised: %s", exc)

    def _pump_stderr(self, stream: IO[str] | None) -> None:
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if self._stderr_tail_size > 0:
                    tail = self._stats.stderr_tail
                    tail.append(line)
                    if len(tail) > self._stderr_tail_size:
                        del tail[: len(tail) - self._stderr_tail_size]
                if self._on_stderr is not None:
                    try:
                        self._on_stderr(line)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("on_stderr callback raised: %s", exc)
                else:
                    logger.info("[capture:stderr] %s", line)
        except Exception as exc:  # noqa: BLE001
            logger.exception("stderr pump crashed: %s", exc)

    def _wait_and_notify(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.wait()
        finally:
            self._stats.exit_code = proc.returncode
            if self._on_exit is not None:
                try:
                    self._on_exit(int(proc.returncode or 0))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("on_exit callback raised: %s", exc)


__all__ = ["CaptureClient", "CaptureClientStats"]
