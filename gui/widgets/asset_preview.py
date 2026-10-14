from __future__ import annotations

import os
import sys
import wave
from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.preview import PreviewService
from gui.i18n import asset_type_to_zh


class AssetPreviewPanel(QWidget):
    """素材详情预览面板（Step 5：多类型分派预览）。"""

    def __init__(self, *, preview_service_provider: Callable[[], PreviewService]) -> None:
        super().__init__()
        self._current_asset_id = ""
        self._current_type = ""
        self._current_link_url = ""
        self._preview_service_provider = preview_service_provider
        self._media_enabled = os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen"
        root = QVBoxLayout(self)
        self._title = QLabel("素材预览")
        self._title.setStyleSheet("font-weight: 600;")
        self._meta = QTextEdit()
        self._meta.setReadOnly(True)
        self._meta.setMaximumHeight(96)
        self._meta.setPlaceholderText("请选择左侧素材查看详情")

        self._stack = QStackedWidget()

        self._empty = QLabel("请选择素材查看详情")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self._empty)

        self._voice_panel = QWidget()
        voice_layout = QVBoxLayout(self._voice_panel)
        self._voice_player: QMediaPlayer | None = None
        self._voice_audio: QAudioOutput | None = None
        self._voice_source_path = ""
        self._voice_playback_failed = False
        self._voice_winsound = False
        self._voice_stop_timer = QTimer(self)
        self._voice_stop_timer.setSingleShot(True)
        self._voice_stop_timer.timeout.connect(self._finish_winsound)
        self._voice_hint = QLabel("未检测到可播放文件（silk 解码依赖后端 preview.py）")
        self._voice_btn = QPushButton("播放")
        self._voice_btn.setEnabled(False)
        voice_layout.addWidget(self._voice_hint)
        voice_layout.addWidget(self._voice_btn)
        voice_layout.addStretch(1)
        self._stack.addWidget(self._voice_panel)

        self._image_panel = QWidget()
        image_layout = QVBoxLayout(self._image_panel)
        self._image_view = QLabel("暂无图片")
        self._image_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_view.setMinimumHeight(220)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._image_view)
        image_layout.addWidget(scroll, 1)
        self._stack.addWidget(self._image_panel)

        self._video_panel = QWidget()
        video_layout = QVBoxLayout(self._video_panel)
        self._video_player: QMediaPlayer | None = None
        self._video_audio: QAudioOutput | None = None
        self._video_view = QVideoWidget()
        self._video_hint = QLabel("未检测到可播放视频文件")
        self._video_btn = QPushButton("播放")
        self._video_btn.setEnabled(False)
        video_layout.addWidget(self._video_hint)
        video_layout.addWidget(self._video_view, 1)
        video_layout.addWidget(self._video_btn)
        self._stack.addWidget(self._video_panel)

        self._text_panel = QTextEdit()
        self._text_panel.setReadOnly(True)
        self._stack.addWidget(self._text_panel)

        self._oa_panel = QFrame()
        self._oa_panel.setFrameShape(QFrame.Shape.StyledPanel)
        oa_layout = QVBoxLayout(self._oa_panel)
        self._oa_title = QLabel("")
        self._oa_title.setStyleSheet("font-weight: 600;")
        self._oa_desc = QLabel("")
        self._oa_desc.setWordWrap(True)
        self._oa_source = QLabel("")
        self._oa_url = QLabel("")
        self._oa_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._oa_url.setOpenExternalLinks(True)
        oa_layout.addWidget(self._oa_title)
        oa_layout.addWidget(self._oa_desc)
        oa_layout.addWidget(self._oa_source)
        oa_layout.addWidget(self._oa_url)
        oa_layout.addStretch(1)
        self._stack.addWidget(self._oa_panel)

        self._link_panel = QWidget()
        link_layout = QVBoxLayout(self._link_panel)
        self._link_url = QLabel("")
        self._link_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._link_url.setOpenExternalLinks(True)
        self._link_btn = QPushButton("打开链接")
        link_layout.addWidget(self._link_url)
        link_layout.addWidget(self._link_btn)
        link_layout.addStretch(1)
        self._stack.addWidget(self._link_panel)

        root.addWidget(self._title)
        root.addWidget(self._meta)
        root.addWidget(self._stack, 1)

        self._voice_btn.clicked.connect(self._toggle_voice_play)
        self._video_btn.clicked.connect(self._toggle_video_play)
        self._link_btn.clicked.connect(self._open_link)
        self._init_media()
        self._stack.setCurrentIndex(0)

    def set_asset(self, payload: Mapping[str, object] | None) -> None:
        if not payload:
            self._title.setText("素材预览")
            self._meta.clear()
            self._current_asset_id = ""
            self._current_type = ""
            self._current_link_url = ""
            self._stop_voice()
            self._voice_source_path = ""
            self._voice_playback_failed = False
            self._stack.setCurrentIndex(0)
            return
        # Step 4: extra 里的字段也当顶级字段用，方便下游 _render_type_panel
        # 不需要关心字段存放在 extra 还是顶级。
        extra = payload.get("extra")
        if isinstance(extra, dict) and extra:
            merged: dict[str, object] = {}
            merged.update(extra)
            merged.update(payload)
            payload = merged
        self._stop_voice()
        asset_type = str(payload.get("asset_type", "unknown"))
        self._current_type = asset_type
        self._current_asset_id = str(payload.get("item_id", ""))
        name = str(payload.get("name", "")).strip() or "未命名"
        updated_at = str(payload.get("updated_at", "")).strip()
        lines = [
            f"类型：{asset_type_to_zh(asset_type)}",
            f"名称：{name}",
            f"更新时间：{updated_at or '-'}",
        ]
        self._title.setText(f"素材预览 - {name}")
        self._meta.setPlainText("\n".join(lines))
        self._render_type_panel(payload)

    def _render_type_panel(self, payload: Mapping[str, object]) -> None:
        asset_type = str(payload.get("asset_type", ""))
        variants = [str(x).strip() for x in payload.get("variants", []) if str(x).strip()]
        if asset_type == "voice":
            self._stack.setCurrentWidget(self._voice_panel)
            path = self._preview_service_provider().pull_preview(payload)
            self._voice_source_path = str(Path(path).resolve()) if path and Path(path).is_file() else ""
            self._voice_playback_failed = False
            playable = self._set_media_source(self._voice_player, path)
            if self._voice_source_path and self._voice_player is None:
                self._voice_playback_failed = True
                self._voice_btn.setEnabled(True)
                self._voice_btn.setText("用系统播放器打开")
                self._voice_hint.setText("内置播放器不可用；可以改用系统播放器。")
            else:
                self._voice_btn.setEnabled(playable)
                self._voice_btn.setText("播放")
                self._voice_hint.setText(f"本地文件：{path}" if playable else "当前语音暂不可直接播放")
            return
        if asset_type == "image":
            self._stack.setCurrentWidget(self._image_panel)
            path = self._preview_service_provider().pull_preview(payload)
            pixmap = QPixmap(path) if path else QPixmap()
            if not pixmap.isNull():
                self._image_view.setPixmap(
                    pixmap.scaled(640, 480, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
            else:
                self._image_view.setPixmap(QPixmap())
                self._image_view.setText("图片预览不可用")
            return
        if asset_type == "video":
            self._stack.setCurrentWidget(self._video_panel)
            path = self._preview_service_provider().pull_preview(payload)
            playable = self._set_media_source(self._video_player, path)
            self._video_btn.setEnabled(playable)
            self._video_btn.setText("播放")
            self._video_hint.setText(f"本地文件：{path}" if playable else "视频预览不可用")
            return
        if asset_type in {"text", "at", "notice"}:
            self._stack.setCurrentWidget(self._text_panel)
            self._text_panel.setPlainText("\n\n".join(variants) if variants else "暂无正文")
            return
        if asset_type in {"oa", "doc", "channel"}:
            labels = {"oa": "公众号", "doc": "企微文档", "channel": "视频号"}
            self._stack.setCurrentWidget(self._oa_panel)
            self._oa_title.setText(str(payload.get("oa_title", "")).strip() or str(payload.get("name", "")).strip())
            self._oa_desc.setText(str(payload.get("oa_desc", "")).strip() or "无描述")
            source = str(payload.get("oa_source", "")).strip() or "-"
            self._oa_source.setText(f"{labels.get(asset_type, '来源')}：{source}")
            url = str(payload.get("oa_url", "")).strip()
            self._oa_url.setText(f"<a href='{url}'>{url}</a>" if url else "-")
            return
        if asset_type == "wxa":
            self._stack.setCurrentWidget(self._oa_panel)
            self._oa_title.setText(
                str(payload.get("wxa_title", "")).strip() or str(payload.get("name", "")).strip()
            )
            app_name = str(payload.get("wxa_app", "")).strip()
            self._oa_desc.setText(app_name or "小程序")
            user = str(payload.get("wxa_user", "")).strip()
            self._oa_source.setText(f"小程序：{app_name or '-'}")
            self._oa_url.setText(user or "-")
            return
        if asset_type == "link":
            url = str(payload.get("url", "")).strip()
            variants = payload.get("variants") if isinstance(payload.get("variants"), list) else []
            text = "\n\n".join(str(item) for item in variants if str(item).strip()) or url or "暂无正文"
            self._stack.setCurrentWidget(self._text_panel)
            self._text_panel.setPlainText(text)
            return
        self._current_link_url = ""
        self._stack.setCurrentIndex(0)

    @staticmethod
    def _set_media_source(player: QMediaPlayer | None, path: str) -> bool:
        if player is None:
            return False
        fp = Path(path) if path else None
        if fp is None or not fp.exists():
            player.stop()
            player.setSource(QUrl())
            return False
        player.stop()
        player.setSource(QUrl.fromLocalFile(str(fp.resolve())))
        return fp.suffix.lower() == ".wav" or fp.stat().st_size > 0

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._stop_voice()
        super().hideEvent(event)

    def _toggle_voice_play(self) -> None:
        if self._voice_winsound:
            self._stop_voice()
            return
        if self._voice_playback_failed and not self._voice_source_path.lower().endswith(".wav"):
            if self._voice_source_path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(self._voice_source_path))
            return
        if self._voice_source_path.lower().endswith(".wav") and self._start_winsound(self._voice_source_path):
            return
        if self._voice_player is None:
            if self._voice_source_path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(self._voice_source_path))
            return
        if self._voice_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._voice_player.pause()
            self._voice_btn.setText("播放")
            return
        self._voice_player.play()
        self._voice_btn.setText("暂停")

    def _start_winsound(self, path: str) -> bool:
        if sys.platform != "win32" or not path.lower().endswith(".wav"):
            return False
        try:
            header = Path(path).read_bytes()[:12]
        except OSError:
            return False
        if not header.startswith(b"RIFF") or b"WAVE" not in header:
            return False
        try:
            import winsound

            duration_ms = _wav_duration_ms(path) or 30_000
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            return False
        self._voice_winsound = True
        self._voice_btn.setText("停止")
        self._voice_hint.setText(f"正在播放：{path}")
        self._voice_stop_timer.start(duration_ms + 150)
        return True

    def _finish_winsound(self) -> None:
        self._voice_winsound = False
        self._voice_btn.setText("播放")

    def _stop_voice(self) -> None:
        self._voice_stop_timer.stop()
        if self._voice_winsound and sys.platform == "win32":
            try:
                import winsound

                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        self._voice_winsound = False
        if self._voice_player is not None:
            self._voice_player.stop()
        if self._voice_source_path:
            self._voice_btn.setText("播放")

    def _on_voice_state_changed(self, state) -> None:
        if self._voice_playback_failed:
            return
        if state != QMediaPlayer.PlaybackState.PlayingState:
            self._voice_btn.setText("播放")

    def _on_voice_error(self, _error, message: str = "") -> None:
        self._voice_playback_failed = True
        self._voice_btn.setText("用系统播放器打开")
        self._voice_btn.setEnabled(bool(self._voice_source_path))
        self._voice_hint.setText(
            f"内置播放器无法播放：{message or '音频格式或系统解码器不支持'}；可改用系统播放器。"
        )

    def _toggle_video_play(self) -> None:
        if self._video_player is None:
            return
        if self._video_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._video_player.pause()
            self._video_btn.setText("播放")
        else:
            self._video_player.play()
            self._video_btn.setText("暂停")

    def _open_link(self) -> None:
        if self._current_link_url:
            QDesktopServices.openUrl(QUrl(self._current_link_url))

    def _init_media(self) -> None:
        if not self._media_enabled:
            self._voice_hint.setText("当前环境关闭多媒体预览（offscreen）")
            self._video_hint.setText("当前环境关闭多媒体预览（offscreen）")
            return
        try:
            self._voice_player = QMediaPlayer(self)
            self._voice_audio = QAudioOutput(self)
            self._voice_audio.setVolume(1.0)
            self._voice_player.setAudioOutput(self._voice_audio)
            self._voice_player.playbackStateChanged.connect(self._on_voice_state_changed)
            self._voice_player.errorOccurred.connect(self._on_voice_error)
            self._voice_playback_failed = False
        except Exception:
            self._voice_player = None
            self._voice_audio = None
        try:
            self._video_player = QMediaPlayer(self)
            self._video_audio = QAudioOutput(self)
            self._video_player.setAudioOutput(self._video_audio)
            self._video_player.setVideoOutput(self._video_view)
        except Exception:
            self._video_player = None
            self._video_audio = None

def _wav_duration_ms(path: str) -> int:
    try:
        with wave.open(path, "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
        if rate <= 0 or frames <= 0:
            return 0
        return max(1, int(frames * 1000 / rate))
    except (wave.Error, OSError, EOFError):
        return 0
