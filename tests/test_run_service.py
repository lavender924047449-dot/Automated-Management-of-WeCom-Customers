from __future__ import annotations

from app.broadcast.plan import BroadcastPlan
from app.broadcast.run_service import RunService


def _make_plan() -> BroadcastPlan:
    return BroadcastPlan.model_validate(
        {
            "contacts": [{"conv_local": "1001", "display_name": "海鸟与鱼"}],
            "sequence": [{"type": "text", "body": "hello"}],
            "perturb": False,
        }
    )


def test_run_service_start_new_run_uses_generated_runner(monkeypatch):
    class _FakeSummary:
        def __init__(self, run_uuid: str) -> None:
            self.run_uuid = run_uuid

    class _FakeRunner:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.run_uuid = ""

        def run(self):
            return _FakeSummary(self.run_uuid)

        def resume(self, run_uuid: str, *, only_failed: bool = False):
            return _FakeSummary(run_uuid)

    class _Resolver:
        def resolve(self, _name: str) -> str:
            return "1001"

    monkeypatch.setattr("app.broadcast.run_service.BroadcastRunner", _FakeRunner)
    monkeypatch.setattr("app.broadcast.run_service.ConversationResolver", lambda account_id: _Resolver())

    svc = RunService(account_id="acct_a")
    summary = svc.start(_make_plan(), run_uuid="run_1")
    assert summary.run_uuid == "run_1"


def test_run_service_resume_path(monkeypatch):
    class _FakeSummary:
        def __init__(self, run_uuid: str) -> None:
            self.run_uuid = run_uuid

    class _FakeRunner:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.run_uuid = ""

        def run(self):
            raise AssertionError("resume 场景不应调用 run")

        def resume(self, run_uuid: str, *, only_failed: bool = False):
            assert only_failed is True
            return _FakeSummary(run_uuid)

    class _Resolver:
        def resolve(self, _name: str) -> str:
            return "1001"

    monkeypatch.setattr("app.broadcast.run_service.BroadcastRunner", _FakeRunner)
    monkeypatch.setattr("app.broadcast.run_service.ConversationResolver", lambda account_id: _Resolver())

    svc = RunService(account_id="acct_a")
    summary = svc.start(_make_plan(), resume_uuid="old_run", only_failed=True)
    assert summary.run_uuid == "old_run"
