"""F2.4：CaptureClient 单测。

覆盖点（对应 PLAN_FTA_CAPTURE_REFACTOR.md 手动清单 §FB）：

* 合法 jsonl 序列 → 全量 dispatch 到 on_event；
* 多行同时到 / 半行拼接 / 空行 / 无效 JSON 的健壮性；
* stderr 尾巴限长；
* stop() 触发 terminate → 超时后 kill 的兜底路径；
* exit code 通过 on_exit 回调。

策略：mock 一个 ``FakePopen`` 替代真实 subprocess，避免依赖 frida / adb。
"""

from __future__ import annotations

import io
import threading
import time
from typing import Any

import pytest

from app.broadcast.capture_client import CaptureClient


class _FakePopen:
    """轻量 Popen mock：stdout/stderr 用 StringIO，terminate/kill 可控。"""

    def __init__(
        self,
        stdout_text: str = "",
        stderr_text: str = "",
        *,
        exit_code: int = 0,
        terminate_effect: str = "exit",  # "exit" | "hang"
    ) -> None:
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.stdin = None
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._terminate_effect = terminate_effect
        self._done = threading.Event()
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = 0
        self._lock = threading.Lock()

    # subprocess.Popen 兼容接口 ---------------------------------------
    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._done.wait(timeout):
            return self.returncode  # type: ignore[return-value]
        raise __import__("subprocess").TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def terminate(self) -> None:
        with self._lock:
            self.terminate_called = True
            if self._terminate_effect == "exit":
                self._finish(self._exit_code)

    def kill(self) -> None:
        with self._lock:
            self.kill_called = True
            self._finish(self._exit_code if self._exit_code != 0 else -9)

    def _finish(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self._done.set()

    def finish_naturally(self, code: int = 0) -> None:
        """测试辅助：模拟子进程自然退出。"""
        with self._lock:
            self._finish(code)


def _make_client(popen: _FakePopen, **kwargs: Any) -> tuple[CaptureClient, list[dict[str, Any]], list[int]]:
    events: list[dict[str, Any]] = []
    exit_codes: list[int] = []

    def on_event(payload: dict[str, Any]) -> None:
        events.append(payload)

    def on_exit(code: int) -> None:
        exit_codes.append(code)

    client = CaptureClient(on_event=on_event, on_exit=on_exit, **kwargs)
    client.start(popen_factory=lambda *a, **kw: popen)
    return client, events, exit_codes


def _wait_until(pred, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ---------------------------------------------------------------------
# 事件路由
# ---------------------------------------------------------------------
def test_jsonl_events_are_dispatched_in_order() -> None:
    lines = [
        '{"sv":1,"t":"ready","ts":1.0,"attached_pid":123}',
        '{"sv":1,"t":"attached","ts":1.1,"pid":123,"dry_copy":false}',
        '{"sv":1,"t":"captured","ts":2.0,"kind":"voice","id":"abc"}',
        '{"sv":1,"t":"stopped","ts":3.0,"reason":"timeout"}',
    ]
    popen = _FakePopen(stdout_text="\n".join(lines) + "\n")
    client, events, exit_codes = _make_client(popen)
    popen.finish_naturally(0)

    assert _wait_until(lambda: len(events) == 4)
    assert [e["t"] for e in events] == ["ready", "attached", "captured", "stopped"]
    assert events[2]["kind"] == "voice"

    assert _wait_until(lambda: exit_codes == [0])
    assert client.stats.events_ok == 4
    assert client.stats.events_bad_json == 0


def test_invalid_json_lines_are_dropped_but_not_fatal() -> None:
    payload = (
        '{"sv":1,"t":"ready","ts":1.0}\n'
        "this is not json\n"
        "\n"  # 空行忽略
        '{"sv":1,"t":"attached","ts":1.1,"pid":5}\n'
        "[1,2,3]\n"  # 合法 JSON 但非 object → 丢弃
    )
    popen = _FakePopen(stdout_text=payload)
    client, events, _ = _make_client(popen)
    popen.finish_naturally(0)

    assert _wait_until(lambda: len(events) == 2)
    assert [e["t"] for e in events] == ["ready", "attached"]
    assert client.stats.events_bad_json == 2


def test_partial_line_is_buffered_until_newline() -> None:
    """半行不应被解析；后续 chunk 拼上 '\\n' 才会 dispatch。"""

    class _ChunkedStream:
        """一段一段返回数据，模拟 pipe 边界抖动。"""

        def __init__(self, chunks: list[str]) -> None:
            self._chunks = list(chunks)
            self._buf = ""

        def readline(self) -> str:
            if not self._chunks and not self._buf:
                return ""
            while "\n" not in self._buf and self._chunks:
                self._buf += self._chunks.pop(0)
            if "\n" in self._buf:
                idx = self._buf.index("\n") + 1
                out, self._buf = self._buf[:idx], self._buf[idx:]
                return out
            # 没换行且没数据了：返回剩余（EOF）
            out, self._buf = self._buf, ""
            return out

    popen = _FakePopen()
    popen.stdout = _ChunkedStream(  # type: ignore[assignment]
        [
            '{"sv":1,"t":"rea',
            'dy","ts":1.0}\n{"sv":1,"t":"att',
            'ached","ts":1.1,"pid":9}\n',
        ]
    )
    client, events, _ = _make_client(popen)
    popen.finish_naturally(0)

    assert _wait_until(lambda: len(events) == 2)
    assert [e["t"] for e in events] == ["ready", "attached"]


def test_stderr_tail_is_truncated() -> None:
    stderr_lines = "\n".join(f"[fta_lite] line {i}" for i in range(50))
    popen = _FakePopen(stderr_text=stderr_lines + "\n")
    client, _, _ = _make_client(popen, stderr_tail_size=5)
    popen.finish_naturally(0)

    assert _wait_until(lambda: len(client.stats.stderr_tail) == 5)
    assert client.stats.stderr_tail[-1] == "[fta_lite] line 49"


# ---------------------------------------------------------------------
# stop 语义
# ---------------------------------------------------------------------
def test_stop_sends_sigterm_when_process_alive() -> None:
    popen = _FakePopen(stdout_text="", terminate_effect="exit", exit_code=0)
    client, _, exit_codes = _make_client(popen)

    rc = client.stop(grace_seconds=1.0)

    assert popen.terminate_called is True
    assert popen.kill_called is False
    assert rc == 0
    assert _wait_until(lambda: exit_codes == [0])


def test_stop_falls_back_to_kill_after_grace_period() -> None:
    popen = _FakePopen(stdout_text="", terminate_effect="hang", exit_code=137)
    client, _, exit_codes = _make_client(popen)

    rc = client.stop(grace_seconds=0.05)

    assert popen.terminate_called is True
    assert popen.kill_called is True
    assert rc == 137
    assert _wait_until(lambda: exit_codes == [137])


def test_double_start_raises() -> None:
    popen = _FakePopen(stdout_text="")
    client, _, _ = _make_client(popen)

    with pytest.raises(RuntimeError):
        client.start(popen_factory=lambda *a, **kw: popen)

    popen.finish_naturally(0)


def test_is_running_reflects_process_state() -> None:
    popen = _FakePopen(stdout_text="")
    client, _, _ = _make_client(popen)
    assert client.is_running() is True
    popen.finish_naturally(0)
    assert _wait_until(lambda: not client.is_running())
