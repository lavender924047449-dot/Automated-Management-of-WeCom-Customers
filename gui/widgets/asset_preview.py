from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.preview import PreviewService


class AssetPreviewPanel(QWidget):
    """素材详情预览面板（Step 5：多类型分派预览）。"""

    textVariantsChanged = Signal(str, list)

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

        self._variant_title = QLabel("文字变体")
        self._variant_hint = QLabel("发送时随机抽 1 条")
        self._variant_list = QListWidget()
        self._variant_input = QLineEdit()
        self._variant_input.setPlaceholderText("新增变体内容")
        self._btn_add_variant = QPushButton("+ 添加变体")
        self._btn_remove_variant = QPushButton("删除选中变体")
        variant_actions = QHBoxLayout()
        variant_actions.addWidget(self._variant_input, 1)
        variant_actions.addWidget(self._btn_add_variant)
        variant_actions.addWidget(self._btn_remove_variant)

        root.addWidget(self._title)
        root.addWidget(self._meta)
        root.addWidget(self._stack, 1)
        root.addWidget(self._variant_title)
        root.addWidget(self._variant_hint)
        root.addWidget(self._variant_list, 1)
        root.addLayout(variant_actions)

        self._btn_add_variant.clicked.connect(self._add_variant)
        self._btn_remove_variant.clicked.connect(self._remove_variant)
        self._voice_btn.clicked.connect(self._toggle_voice_play)
        self._video_btn.clicked.connect(self._toggle_video_play)
        self._link_btn.clicked.connect(self._open_link)
        self._init_media()
        self._toggle_variant_editor(False)
        self._stack.setCurrentIndex(0)

    def set_asset(self, payload: Mapping[str, object] | None) -> None:
        if not payload:
            self._title.setText("素材预览")
            self._meta.clear()
            self._variant_list.clear()
            self._variant_input.clear()
            self._current_asset_id = ""
            self._current_type = ""
            self._current_link_url = ""
            self._toggle_variant_editor(False)
            self._stack.setCurrentIndex(0)
            return
        asset_type = str(payload.get("asset_type", "unknown"))
        self._current_type = asset_type
        self._current_asset_id = str(payload.get("item_id", ""))
        name = str(payload.get("name", "")).strip() or "未命名"
        updated_at = str(payload.get("updated_at", "")).strip()
        lines = [
            f"类型：{asset_type}",
            f"名称：{name}",
            f"更新时间：{updated_at or '-'}",
        ]
        self._title.setText(f"素材预览 - {name}")
        self._meta.setPlainText("\n".join(lines))
        self._variant_list.clear()
        self._variant_input.clear()
        self._render_type_panel(payload)

    def _toggle_variant_editor(self, enabled: bool) -> None:
        self._variant_title.setVisible(enabled)
        self._variant_hint.setVisible(enabled)
        self._variant_list.setVisible(enabled)
        self._variant_input.setVisible(enabled)
        self._btn_add_variant.setVisible(enabled)
        self._btn_remove_variant.setVisible(enabled)

    def _add_variant(self) -> None:
        if self._current_type != "text":
            return
        text = self._variant_input.text().strip()
        if not text:
            return
        self._variant_list.addItem(QListWidgetItem(text))
        self._variant_input.clear()
        self._emit_variants()

    def _remove_variant(self) -> None:
        if self._current_type != "text":
            return
        row = self._variant_list.currentRow()
        if row < 0:
            return
        self._variant_list.takeItem(row)
        self._emit_variants()

    def _emit_variants(self) -> None:
        if self._current_type != "text" or not self._current_asset_id:
            return
        variants = [self._variant_list.item(i).text().strip() for i in range(self._variant_list.count())]
        variants = [x for x in variants if x]
        if not variants:
            return
        self.textVariantsChanged.emit(self._current_asset_id, variants)

    def _render_type_panel(self, payload: Mapping[str, object]) -> None:
        asset_type = str(payload.get("asset_type", ""))
        variants = [str(x).strip() for x in payload.get("variants", []) if str(x).strip()]
        if asset_type == "voice":
            self._stack.setCurrentWidget(self._voice_panel)
            self._toggle_variant_editor(False)
            path = self._preview_service_provider().pull_preview(payload)
            playable = self._set_media_source(self._voice_player, path)
            self._voice_btn.setEnabled(playable)
            self._voice_btn.setText("播放")
            self._voice_hint.setText(f"本地文件：{path}" if playable else "当前语音暂不可直接播放")
            return
        if asset_type == "image":
            self._stack.setCurrentWidget(self._image_panel)
            self._toggle_variant_editor(False)
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
            self._toggle_variant_editor(False)
            path = self._preview_service_provider().pull_preview(payload)
            playable = self._set_media_source(self._video_player, path)
            self._video_btn.setEnabled(playable)
            self._video_btn.setText("播放")
            self._video_hint.setText(f"本地文件：{path}" if playable else "视频预览不可用")
            return
        if asset_type == "text":
            self._stack.setCurrentWidget(self._text_panel)
            self._text_panel.setPlainText("\n\n".join(variants) if variants else "暂无变体")
            for variant in variants:
                self._variant_list.addItem(QListWidgetItem(variant))
            self._toggle_variant_editor(True)
            return
        if asset_type == "oa":
            self._stack.setCurrentWidget(self._oa_panel)
            self._toggle_variant_editor(False)
            self._oa_title.setText(str(payload.get("oa_title", "")).strip() or str(payload.get("name", "")).strip())
            self._oa_desc.setText(str(payload.get("oa_desc", "")).strip() or "无描述")
            self._oa_source.setText(f"来源：{str(payload.get('oa_source', '')).strip() or '-'}")
            url = str(payload.get("oa_url", "")).strip()
            self._oa_url.setText(f"<a href='{url}'>{url}</a>" if url else "-")
            return
        if asset_type == "link":
            self._stack.setCurrentWidget(self._link_panel)
            self._toggle_variant_editor(False)
            self._current_link_url = str(payload.get("url", "")).strip()
            self._link_url.setText(
                f"<a href='{self._current_link_url}'>{self._current_link_url}</a>"
                if self._current_link_url
                else "-"
            )
            self._link_btn.setEnabled(bool(self._current_link_url))
            return
        self._current_link_url = ""
        self._stack.setCurrentIndex(0)
        self._toggle_variant_editor(False)

    @staticmethod
    def _set_media_source(player: QMediaPlayer | None, path: str) -> bool:
        if player is None:
            return False
        fp = Path(path) if path else None
        if fp is None or not fp.exists():
            player.setSource(QUrl())
            return False
        player.setSource(QUrl.fromLocalFile(str(fp)))
        return True

    def _toggle_voice_play(self) -> None:
        if self._voice_player is None:
            return
        if self._voice_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._voice_player.pause()
            self._voice_btn.setText("播放")
        else:
            self._voice_player.play()
            self._voice_btn.setText("暂停")

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
            self._voice_player.setAudioOutput(self._voice_audio)
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
