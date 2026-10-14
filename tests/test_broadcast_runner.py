"""Step 4.3: BroadcastRunner 测试。"""

from __future__ import annotations

import io
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from app.broadcast.library import VoiceLibrary
from app.broadcast.plan import BroadcastPlan
from app.broadcast.report_store import ReportStore
from app.broadcast.runner import BroadcastRunner, SubprocessSendClient


class _MockSendClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int | None]] = []
        self.fail_types: set[str] = set()
        self.ready = True

    def ensure_ready(self) -> bool:
        return self.ready

    def send_voice(self, *, conv_local: str, silk_path: str, voice_time: int) -> bool:
        self.calls.append(("voice", conv_local, silk_path, voice_time))
        return "voice" not in self.fail_types

    def send_text(self, *, conv_local: str, body: str) -> bool:
        self.calls.append(("text", conv_local, body, None))
        return "text" not in self.fail_types

    def send_at(self, *, conv_local: str, body: str) -> bool:
        self.calls.append(("at", conv_local, body, None))
        return "at" not in self.fail_types

    def send_notice(self, *, conv_local: str, body: str) -> bool:
        self.calls.append(("notice", conv_local, body, None))
        return "notice" not in self.fail_types

    def send_image(self, *, conv_local: str, image_path: str) -> bool:
        self.calls.append(("image", conv_local, image_path, None))
        return "image" not in self.fail_types

    def send_oa(self, *, conv_local: str, template: str) -> bool:
        self.calls.append(("oa", conv_local, template, None))
        return "oa" not in self.fail_types

    def send_wxa(self, *, conv_local: str, template: str) -> bool:
        self.calls.append(("wxa", conv_local, template, None))
        return "wxa" not in self.fail_types

    def send_video(
        self,
        *,
        conv_local: str,
        video_path: str,
        thumb_path: str = "",
        duration: int | None = None,
    ) -> bool:
        payload = f"{video_path}|{thumb_path}"
        self.calls.append(("video", conv_local, payload, duration))
        return "video" not in self.fail_types


def _make_plan(**overrides) -> BroadcastPlan:
    data = {
        "contacts": ["海鸟与鱼", "张三"],
        "sequence": [
            {"type": "voice", "voice_id": "voice-1"},
            {"type": "text", "body": "你好"},
            {"type": "oa", "template_id": "oa-1"},
            {"type": "image", "path": "/sdcard/Test-Voice/voice_lib/a.png"},
            {"type": "video", "path": "/sdcard/Test-Voice/voice_lib/a.mp4", "thumb_path": "/sdcard/Test-Voice/voice_lib/a.jpg", "duration": 8},
        ],
        "step_gaps": [0, 0, 0, 0],
        "contact_gap": 3.0,
    }
    data.update(overrides)
    return BroadcastPlan.model_validate(data)


def _prepare_voice_index(tmp_path: Path) -> Path:
    voice_file = tmp_path / "demo.silk"
    voice_file.write_bytes(b"\x02#!SILK_V3\n\x02\x00\x00\x00")
    idx = tmp_path / "voice_lib_index.json"
    lib = VoiceLibrary(idx)
    row = lib.add(file=str(voice_file), duration=3, note="n")
    row.id = "voice-1"
    lib.save()
    return idx


def test_runner_serial_flow_and_blocked(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    sleeps: list[float] = []

    def resolver(name: str) -> str:
        return {"海鸟与鱼": "1001", "张三": "1002"}[name]

    client = _MockSendClient()
    runner = BroadcastRunner(
        plan=_make_plan(),
        resolver=resolver,
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda lo, hi: 0,
        sleep_fn=lambda s: sleeps.append(s),
    )
    summary = runner.run()

    counts = summary.counts()
    assert counts["sent"] == 10  # 2 contacts * (voice,text,oa,image,video)
    assert counts.get("blocked", 0) == 0
    assert len(summary.records) == 10
    assert [x[0] for x in client.calls] == ["voice", "text", "oa", "image", "video", "voice", "text", "oa", "image", "video"]
    # step_gaps=[0,0,0,0] → clamp 到 3.0，每联系人 4 次消息间隔 + 1 次联系人间隔
    # = 2*4 step gaps + 1 contact gap = 9 次 sleep
    assert len(sleeps) == 9
    assert all(s >= 3.0 for s in sleeps)
    assert summary.run_uuid


def test_runner_resolve_failed_does_not_block_others(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    client = _MockSendClient()

    def resolver(name: str) -> str:
        if name == "海鸟与鱼":
            raise KeyError("not found")
        return "1002"

    runner = BroadcastRunner(
        plan=_make_plan(sequence=[{"type": "text", "body": "hi"}]),
        resolver=resolver,
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert [r.status for r in summary.records] == ["resolve_failed", "sent"]
    assert [x[0] for x in client.calls] == ["text"]


def test_runner_uses_conv_local_from_plan_contact_without_resolve(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    client = _MockSendClient()
    plan = _make_plan(
        contacts=[
            {"conv_local": "1001", "display_name": "海鸟与鱼"},
        ],
        sequence=[{"type": "text", "body": "hi"}],
    )

    def resolver(_name: str) -> str:
        raise AssertionError("当 plan 已包含 conv_local 时不应再调用 resolver")

    runner = BroadcastRunner(
        plan=plan,
        resolver=resolver,
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert [r.status for r in summary.records] == ["sent"]
    assert client.calls == [("text", "1001", "hi", None)]


def test_runner_sends_wxa_template(tmp_path: Path, monkeypatch):
    index_path = _prepare_voice_index(tmp_path)
    monkeypatch.setattr(
        "app.broadcast.runner.load_wxa_template",
        lambda template_id, account_id=None: {"username": "gh_demo@app", "pagepath": "pages/index", "template_id": template_id},
    )
    client = _MockSendClient()
    plan = _make_plan(
        contacts=[{"conv_local": "1001", "display_name": "海鸟与鱼"}],
        sequence=[{"type": "wxa", "template_id": "wxa-1"}],
    )
    runner = BroadcastRunner(
        plan=plan,
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert [r.status for r in summary.records] == ["sent"]
    assert client.calls == [("wxa", "1001", "wxa-1", None)]


def test_runner_failure_continues(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    client = _MockSendClient()
    client.fail_types.add("text")

    runner = BroadcastRunner(
        plan=_make_plan(contacts=["海鸟与鱼"], sequence=[{"type": "text", "body": "a"}, {"type": "image", "path": "/sdcard/x.png"}]),
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
        retry_delays_seconds=(),
    )
    summary = runner.run()
    assert [r.status for r in summary.records] == ["failed", "sent"]
    assert [x[0] for x in client.calls] == ["text", "image"]


def test_runner_sends_at_and_notice(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    client = _MockSendClient()
    plan = _make_plan(
        contacts=[{"conv_local": "1001", "display_name": "测试群000"}],
        sequence=[
            {"type": "at", "body": "大家好"},
            {"type": "notice", "body": "本周公告"},
        ],
        step_gaps=[0],
    )
    runner = BroadcastRunner(
        plan=plan,
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert [r.status for r in summary.records] == ["sent", "sent"]
    assert client.calls == [
        ("at", "1001", "大家好", None),
        ("notice", "1001", "本周公告", None),
    ]


def test_subprocess_client_at_body_with_leading_dashes(monkeypatch):
    captured: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured.append(list(cmd))
            self.returncode = 0
            self.stdout = io.StringIO("action=at return=true")
            self.stderr = io.StringIO("")

        def poll(self):
            return self.returncode

        def wait(self, **_kw):
            pass

        def kill(self):
            pass

    monkeypatch.setattr("app.broadcast.runner.subprocess.Popen", FakePopen)
    client = SubprocessSendClient(account_id="vid_A")
    ok = client.send_at(conv_local="1001", body="--测试at")
    assert ok is True
    assert captured
    assert any(arg == "--body=--测试at" for arg in captured[0])
    assert "at" in captured[0]


def test_subprocess_client_text_body_with_leading_dashes(monkeypatch):
    captured: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured.append(list(cmd))
            self.returncode = 0
            self.stdout = io.StringIO("action=text return=true")
            self.stderr = io.StringIO("")
        def poll(self):
            return self.returncode
        def wait(self, **_kw):
            pass
        def kill(self):
            pass

    monkeypatch.setattr("app.broadcast.runner.subprocess.Popen", FakePopen)
    client = SubprocessSendClient(account_id="vid_A")
    ok = client.send_text(conv_local="1001", body="--测试文字")
    assert ok is True
    assert captured
    assert any(arg.startswith("--body=") and arg == "--body=--测试文字" for arg in captured[0])


def test_subprocess_client_passes_account_id(monkeypatch):
    captured: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured.append(list(cmd))
            self.returncode = 0
            self.stdout = io.StringIO("action=text return=true")
            self.stderr = io.StringIO("")
        def poll(self):
            return self.returncode
        def wait(self, **_kw):
            pass
        def kill(self):
            pass

    monkeypatch.setattr("app.broadcast.runner.subprocess.Popen", FakePopen)
    client = SubprocessSendClient(python_executable="python", module="runtime.wework_re_android.send_engine", account_id="vid_A")
    ok = client.send_text(conv_local="1001", body="hello")
    assert ok is True
    assert captured, "subprocess.Popen 应该被调用"
    assert "--account-id" in captured[0]
    idx = captured[0].index("--account-id")
    assert captured[0][idx + 1] == "vid_A"


def test_subprocess_client_ensure_ready_uses_frida_session(monkeypatch):
    class _FakeFridaSession:
        def __init__(self, **_kwargs):
            self.session = None
            self.retries = []

        def heartbeat(self):
            return False

        def reconnect(self, *, adb_serial: str, retries: int):
            self.retries.append((adb_serial, retries))
            self.session = object()
            return True

    monkeypatch.setattr("runtime.wework_re_android.frida_session.FridaSession", _FakeFridaSession)
    client = SubprocessSendClient(account_id="vid_A")
    assert client.ensure_ready() is True
    assert client._frida_session.retries == [("127.0.0.1:16384", 3)]


def test_runner_skips_preflight_for_subprocess_send_client(tmp_path: Path):
    class _Client(SubprocessSendClient):
        def __init__(self) -> None:
            pass

        def ensure_ready(self) -> bool:
            raise AssertionError("runner should not call SubprocessSendClient.ensure_ready")

        def send_text(self, *, conv_local: str, body: str) -> bool:
            assert conv_local == "1001"
            assert body == "hi"
            return True

    index_path = _prepare_voice_index(tmp_path)
    runner = BroadcastRunner(
        plan=_make_plan(contacts=["海鸟与鱼"], sequence=[{"type": "text", "body": "hi"}]),
        resolver=lambda _n: "1001",
        send_client=_Client(),
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )

    summary = runner.run()

    assert summary.records[0].status == "sent"


def test_text_template_variant_pick_records_variant_used(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    lib = VoiceLibrary.load(index_path)
    lib.add_text_template(
        template_id="tpl-1",
        name="开场",
        variants=["你好A", "你好B", "你好C"],
        tags=["首触达"],
    )
    lib.save()
    client = _MockSendClient()
    plan = _make_plan(
        contacts=["海鸟与鱼"],
        sequence=[
            {"type": "text", "template_id": "tpl-1", "variant_pick": "index:1"},
            {"type": "text", "template_id": "tpl-1", "variant_pick": "random"},
        ],
    )
    runner = BroadcastRunner(
        plan=plan,
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 2,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert [x[2] for x in client.calls] == ["你好B", "你好C"]
    assert [r.variant_used for r in summary.records] == ["tpl-1#1", "tpl-1#2"]


def test_archived_voice_is_filtered_from_runner(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    lib = VoiceLibrary.load(index_path)
    lib.archive("voice-1")
    lib.save()
    client = _MockSendClient()
    runner = BroadcastRunner(
        plan=_make_plan(contacts=["海鸟与鱼"], sequence=[{"type": "voice", "voice_id": "voice-1"}]),
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert summary.records[0].status == "failed"
    assert "voice_id 不存在" in summary.records[0].error


def test_runner_retry_policy_and_report_rows(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    sleeps: list[float] = []
    report_store = ReportStore(reports_dir=tmp_path / "reports")
    client = _MockSendClient()
    client.fail_types.add("text")
    runner = BroadcastRunner(
        plan=_make_plan(contacts=["海鸟与鱼"], sequence=[{"type": "text", "body": "a"}]),
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda sec: sleeps.append(sec),
        run_uuid="run_retry",
        report_store=report_store,
    )
    summary = runner.run()
    assert summary.records[0].status == "failed"
    assert [x[0] for x in client.calls] == ["text"]
    assert sum(sleeps) < 1.0

    rows = [
        x
        for x in report_store.load_report("run_retry").rows
        if str(x.get("type", "")).strip() != "plan_meta"
    ]
    statuses = [x["status"] for x in rows]
    assert statuses == ["before_send", "failed"]


def test_runner_fails_fast_when_frida_session_unavailable(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    client = _MockSendClient()
    client.ready = False
    runner = BroadcastRunner(
        plan=_make_plan(contacts=["海鸟与鱼"], sequence=[{"type": "text", "body": "a"}]),
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(RuntimeError, match="Frida 会话不可用"):
        runner.run()
    rows = runner.report_store.load_report(runner.run_uuid).rows
    assert rows
    assert rows[-1]["status"] == "failed"
    assert "Frida 会话不可用" in str(rows[-1].get("error", ""))


def test_runner_resume_from_next_step(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    report_store = ReportStore(reports_dir=tmp_path / "reports")
    run_uuid = "resume_case"
    report_store.append(
        run_uuid,
        {
            "step_index": 0,
            "sequence_index": 0,
            "contact": "海鸟与鱼",
            "conv_local": "1001",
            "type": "text",
            "status": "sent",
            "error": "",
            "attempt": 1,
        },
    )
    client = _MockSendClient()
    plan = _make_plan(
        contacts=["海鸟与鱼"],
        sequence=[
            {"type": "text", "body": "a"},
            {"type": "image", "path": "/sdcard/x.png"},
        ],
    )
    runner = BroadcastRunner(
        plan=plan,
        resolver=lambda _n: "1001",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
        report_store=report_store,
        run_uuid=run_uuid,
    )
    summary = runner.resume(run_uuid)
    assert len(summary.records) == 1
    assert summary.records[0].step_type == "image"
    assert [x[0] for x in client.calls] == ["image"]


def test_runner_resume_only_failed_steps(tmp_path: Path):
    index_path = _prepare_voice_index(tmp_path)
    report_store = ReportStore(reports_dir=tmp_path / "reports")
    run_uuid = "resume_failed_case"
    report_store.append(
        run_uuid,
        {
            "step_index": 0,
            "sequence_index": 0,
            "contact": "海鸟与鱼",
            "conv_local": "1001",
            "type": "text",
            "status": "sent",
            "error": "",
            "attempt": 1,
        },
    )
    report_store.append(
        run_uuid,
        {
            "step_index": 1,
            "sequence_index": 1,
            "contact": "海鸟与鱼",
            "conv_local": "1001",
            "type": "image",
            "status": "failed",
            "error": "send returned false",
            "attempt": 1,
        },
    )
    report_store.append(
        run_uuid,
        {
            "step_index": 2,
            "sequence_index": 0,
            "contact": "张三",
            "conv_local": "",
            "type": "text",
            "status": "resolve_failed",
            "error": "contact not found",
            "attempt": 0,
        },
    )
    plan = _make_plan(
        contacts=["海鸟与鱼", "张三"],
        sequence=[
            {"type": "text", "body": "a"},
            {"type": "image", "path": "/sdcard/x.png"},
        ],
    )
    client = _MockSendClient()
    runner = BroadcastRunner(
        plan=plan,
        resolver=lambda n: "1001" if n == "海鸟与鱼" else "1002",
        send_client=client,
        voice_index_path=index_path,
        account_id="test_account",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
        retry_delays_seconds=(),
        report_store=report_store,
        run_uuid=run_uuid,
    )
    summary = runner.resume(run_uuid, only_failed=True)
    assert len(summary.records) == 2
    assert [(r.contact, r.sequence_index) for r in summary.records] == [("海鸟与鱼", 1), ("张三", 0)]
    assert [x[0] for x in client.calls] == ["image", "text"]

