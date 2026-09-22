from __future__ import annotations

import runtime.wework_re_android.frida_session as fs


class _FakeScript:
    def __init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False


class _FakeSession:
    def __init__(self, fail_heartbeat: bool = False) -> None:
        self.fail_heartbeat = fail_heartbeat
        self.detached = False

    def create_script(self, _code: str):
        if self.fail_heartbeat:
            raise RuntimeError("hb failed")
        return _FakeScript()

    def detach(self) -> None:
        self.detached = True


class _FakeDevice:
    def __init__(self, fake_session: _FakeSession) -> None:
        self.fake_session = fake_session

    def attach(self, _pid: int):
        return self.fake_session


class _FakeDeviceManager:
    def __init__(self, fake_session: _FakeSession) -> None:
        self.fake_session = fake_session

    def add_remote_device(self, _remote: str):
        return _FakeDevice(self.fake_session)


class _FakeFrida:
    def __init__(self, fake_session: _FakeSession) -> None:
        self.fake_session = fake_session

    def get_device_manager(self):
        return _FakeDeviceManager(self.fake_session)


def test_attach_and_heartbeat_ok(monkeypatch):
    fake_session = _FakeSession(fail_heartbeat=False)
    monkeypatch.setattr(fs, "import_frida", lambda: _FakeFrida(fake_session))

    session = fs.FridaSession(
        frida_remote="127.0.0.1:27042",
        process_name="com.tencent.wework",
        pid_provider=lambda *_: 1234,
    )
    session.attach(adb_serial="127.0.0.1:16384")
    assert session.heartbeat() is True
    assert session.state.attached is True
    session.detach()
    assert fake_session.detached is True


def test_reconnect_retries_until_success(monkeypatch):
    first = _FakeSession(fail_heartbeat=True)
    second = _FakeSession(fail_heartbeat=False)
    seq = [first, second]

    class _SeqFrida:
        def get_device_manager(self):
            current = seq[0] if len(seq) == 1 else seq.pop(0)
            return _FakeDeviceManager(current)

    monkeypatch.setattr(fs, "import_frida", lambda: _SeqFrida())
    s = fs.FridaSession(
        frida_remote="127.0.0.1:27042",
        process_name="com.tencent.wework",
        pid_provider=lambda *_: 1001,
    )
    assert s.reconnect(adb_serial="127.0.0.1:16384", retries=3) is True
