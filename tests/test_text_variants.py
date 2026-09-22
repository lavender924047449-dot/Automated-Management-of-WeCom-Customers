"""Step 13: 文字变体与归档过滤测试。"""

from __future__ import annotations

from pathlib import Path

from app.broadcast.library import VoiceLibrary
from app.broadcast.plan import BroadcastPlan
from app.broadcast.runner import BroadcastRunner


class _TextOnlySendClient:
    def __init__(self) -> None:
        self.bodies: list[str] = []

    def send_voice(self, *, conv_local: str, silk_path: str, voice_time: int) -> bool:
        raise AssertionError("本测试不应发送语音")

    def send_text(self, *, conv_local: str, body: str) -> bool:
        self.bodies.append(body)
        return True

    def send_image(self, *, conv_local: str, image_path: str) -> bool:
        raise AssertionError("本测试不应发送图片")

    def send_oa(self, *, conv_local: str, template: str) -> bool:
        raise AssertionError("本测试不应发送公众号卡片")

    def send_video(
        self,
        *,
        conv_local: str,
        video_path: str,
        thumb_path: str = "",
        duration: int | None = None,
    ) -> bool:
        raise AssertionError("本测试不应发送视频")


def _make_plan(template_id: str) -> BroadcastPlan:
    return BroadcastPlan.model_validate(
        {
            "contacts": ["A", "B", "C", "D", "E", "F"],
            "sequence": [{"type": "text", "template_id": template_id, "variant_pick": "random"}],
            "msg_gap_seconds": [0, 0],
            "contact_gap_seconds": [0, 0],
            "perturb": False,
        }
    )


def test_variant_pick_random_distribution_like(tmp_path: Path):
    lib = VoiceLibrary(tmp_path / "voice_lib_index.json")
    lib.add_text_template(
        template_id="tpl-rand",
        name="随机模板",
        variants=["v0", "v1", "v2"],
        tags=[],
    )
    lib.save()

    picks = iter([0, 1, 2, 1, 0, 2])
    def fake_rand(lo: float, hi: float) -> float:
        if lo == 0 and hi == 0:
            return 0
        return next(picks)
    client = _TextOnlySendClient()
    runner = BroadcastRunner(
        plan=_make_plan("tpl-rand"),
        resolver=lambda name: name,
        send_client=client,
        voice_index_path=tmp_path / "voice_lib_index.json",
        rand_fn=fake_rand,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert [r.variant_used for r in summary.records] == [
        "tpl-rand#0",
        "tpl-rand#1",
        "tpl-rand#2",
        "tpl-rand#1",
        "tpl-rand#0",
        "tpl-rand#2",
    ]
    assert client.bodies == ["v0", "v1", "v2", "v1", "v0", "v2"]


def test_archived_template_not_selectable(tmp_path: Path):
    lib = VoiceLibrary(tmp_path / "voice_lib_index.json")
    tpl = lib.add_text_template(
        template_id="tpl-archived",
        name="归档模板",
        variants=["x"],
        tags=[],
    )
    lib.archive_text_template(tpl.id)
    lib.save()

    client = _TextOnlySendClient()
    runner = BroadcastRunner(
        plan=_make_plan("tpl-archived"),
        resolver=lambda _name: "1001",
        send_client=client,
        voice_index_path=tmp_path / "voice_lib_index.json",
        rand_fn=lambda _a, _b: 0,
        sleep_fn=lambda _s: None,
    )
    summary = runner.run()
    assert summary.records[0].status == "failed"
    assert "text template 不存在或已归档" in summary.records[0].error
    assert client.bodies == []
