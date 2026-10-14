"""Step 4: 发送客户端 facade。"""

from __future__ import annotations

from app.broadcast.runner import SubprocessSendClient


class DryRunClient:
    """演练模式客户端：所有发送都返回成功。"""

    def ensure_ready(self) -> bool:
        return True

    def send_voice(self, *, conv_local: str, silk_path: str, voice_time: int) -> bool:
        return True

    def send_text(self, *, conv_local: str, body: str) -> bool:
        return True

    def send_at(self, *, conv_local: str, body: str) -> bool:
        return True

    def send_notice(self, *, conv_local: str, body: str) -> bool:
        return True

    def send_image(self, *, conv_local: str, image_path: str) -> bool:
        return True

    def send_oa(self, *, conv_local: str, template: str) -> bool:
        return True

    def send_wxa(self, *, conv_local: str, template: str) -> bool:
        return True

    def send_video(
        self,
        *,
        conv_local: str,
        video_path: str,
        thumb_path: str = "",
        duration: int | None = None,
    ) -> bool:
        return True


__all__ = ["DryRunClient", "SubprocessSendClient"]
