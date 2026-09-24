"""Step 19: Frida 会话封装（attach/heartbeat/reconnect）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from runtime.wework_re_android._frida_env import import_frida

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"


@dataclass(slots=True)
class FridaSessionState:
    attached: bool = False
    last_error: str = ""
    pid: int | None = None


class FridaSession:
    def __init__(
        self,
        *,
        frida_remote: str = DEFAULT_FRIDA_REMOTE,
        process_name: str = DEFAULT_PROCESS_NAME,
        pid_provider: Callable[[str, str], int | None],
    ) -> None:
        self.frida_remote = frida_remote
        self.process_name = process_name
        self.pid_provider = pid_provider
        self.state = FridaSessionState()
        self._frida = import_frida()
        self._session = None

    @property
    def session(self) -> Any:
        return self._session

    def attach(self, *, adb_serial: str = DEFAULT_ADB_SERIAL) -> Any:
        self.detach()
        pid = self.pid_provider(adb_serial, self.process_name)
        if pid is None:
            self.state = FridaSessionState(attached=False, last_error="process not found", pid=None)
            raise RuntimeError(f"process not found: {self.process_name}")
        try:
            device = self._frida.get_device_manager().add_remote_device(self.frida_remote)
            session = device.attach(pid)
        except Exception as exc:
            self.state = FridaSessionState(attached=False, last_error=str(exc), pid=pid)
            raise RuntimeError(f"frida attach failed: {exc}") from exc
        self._session = session
        self.state = FridaSessionState(attached=True, last_error="", pid=pid)
        return session

    def heartbeat(self) -> bool:
        if self._session is None:
            self.state.attached = False
            self.state.last_error = "session not attached"
            return False
        try:
            script = self._session.create_script(
                """
Java.perform(function () {
    send("hb_ok");
});
"""
            )
            script.load()
            script.unload()
            self.state.attached = True
            self.state.last_error = ""
            return True
        except Exception as exc:
            self.state.attached = False
            self.state.last_error = str(exc)
            return False

    def reconnect(
        self,
        *,
        adb_serial: str = DEFAULT_ADB_SERIAL,
        retries: int = 3,
        retry_gap_seconds: float = 1.0,
        on_retry: Callable[[int, str], None] | None = None,
    ) -> bool:
        import time

        total = max(1, int(retries))
        for i in range(1, total + 1):
            try:
                self.attach(adb_serial=adb_serial)
                if self.heartbeat():
                    return True
            except Exception as exc:
                self.state.last_error = str(exc)
            if on_retry is not None:
                on_retry(i, self.state.last_error)
            if i < total and retry_gap_seconds > 0:
                time.sleep(retry_gap_seconds)
        return False

    def detach(self) -> None:
        sess = self._session
        self._session = None
        if sess is None:
            return
        try:
            sess.detach()
        except Exception:
            pass
        self.state.attached = False

    def run_script(self, js_code: str, *, adb_serial: str = DEFAULT_ADB_SERIAL, wait_seconds: float = 4.0) -> int:
        import time

        try:
            session = self.attach(adb_serial=adb_serial)
        except Exception as exc:
            print(f"[frida_session] attach failed: {exc}")
            return 1
        try:
            script = session.create_script(js_code)
            script.load()
            time.sleep(max(0.0, float(wait_seconds)))
            return 0
        except Exception as exc:
            self.state.last_error = str(exc)
            print(f"[frida_session] run_script failed: {exc}")
            return 2
        finally:
            self.detach()


__all__ = ["FridaSession", "FridaSessionState", "DEFAULT_ADB_SERIAL", "DEFAULT_FRIDA_REMOTE", "DEFAULT_PROCESS_NAME"]
