"""待确认模板命名对话框（A 方案）：探针抓到的 OA/WXA 卡片 → 弹窗让用户命名/保存/丢弃。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from app.security import get_secure_store

_SECURE_STORE = get_secure_store()


class CardCopyDialog(QDialog):
    """编辑公众号/小程序卡片的标题与摘要。

    FTA 采集常常拿不到卡片标题（服务端下发的富文本里 title 为空），
    导致卡片发出后对方看到"无标题"。此对话框让用户手动补齐 `title` /
    `desc`，写回模板 JSON 后，`send_engine` 的 `buildLinkMessageFromTemplate`
    会读取 `title` 字段渲染卡片标题。
    """

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        kind: str = "oa",  # 'oa' | 'wxa'
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._payload = payload
        self._kind = kind
        label = {"oa": "公众号图文卡片", "wxa": "小程序"}.get(kind, kind)
        self.setWindowTitle(f"编辑{label}文案")
        self.setModal(True)
        self.resize(520, 320)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        tip = QLabel(
            "采集到的卡片缺少标题时，对方会看到\"无标题\"卡片。\n"
            "请补齐标题（必填）；\"摘要\"对应卡片标题下方那一行"
            "（通常是公众号名/来源，如\"少盐OnYoung\"），留空则不显示该行。"
        )
        tip.setStyleSheet("color: #6b7280; font-size: 11px;")
        tip.setWordWrap(True)
        root.addWidget(tip)

        form = QFormLayout()
        self._title_edit = QLineEdit()
        self._title_edit.setText(str(self._payload.get("title", "")).strip())
        self._title_edit.setPlaceholderText("卡片标题，例如：本周新品推荐")
        form.addRow("标题 *", self._title_edit)

        self._desc_edit = QTextEdit()
        desc = str(self._payload.get("desc", "") or self._payload.get("digest", "")).strip()
        self._desc_edit.setPlainText(desc)
        self._desc_edit.setPlaceholderText("标题下方的来源/公众号名，例如：少盐OnYoung")
        self._desc_edit.setMaximumHeight(90)
        form.addRow("摘要", self._desc_edit)

        url = str(self._payload.get("url", "")).strip()
        if url:
            url_label = QLabel(url)
            url_label.setWordWrap(True)
            url_label.setStyleSheet("color: #6b7280;")
            form.addRow("链接", url_label)
        thumb = str(self._payload.get("thumburl", "")).strip()
        if thumb:
            thumb_label = QLabel(thumb)
            thumb_label.setWordWrap(True)
            thumb_label.setStyleSheet("color: #6b7280;")
            form.addRow("封面", thumb_label)
        root.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _on_save(self) -> None:
        if not self._title_edit.text().strip():
            QMessageBox.warning(self, "缺少标题", "请填写卡片标题后再保存。")
            return
        self.accept()

    def entered_title(self) -> str:
        return self._title_edit.text().strip()

    def entered_desc(self) -> str:
        return self._desc_edit.toPlainText().strip()


class PendingTemplateDialog(QDialog):
    """展示 pending 模板字段并让用户命名。返回 accepted/rejected/deferred。"""

    RESULT_SAVE = 1
    RESULT_DISCARD = 2
    RESULT_DEFER = 0  # 稍后处理，保留 pending 文件

    def __init__(
        self,
        pending_path: Path,
        payload: dict[str, Any],
        *,
        kind: str = "oa",  # 'oa' | 'wxa' | 'image' | 'video'
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._path = pending_path
        self._payload = payload
        self._kind = kind
        self._decision = self.RESULT_DEFER
        title_prefix = {
            "oa": "公众号图文卡片",
            "wxa": "小程序",
            "image": "图片素材",
            "video": "视频素材",
        }.get(kind, kind)
        self.setWindowTitle(f"新捕获的{title_prefix} · 请命名后保存")
        self.setModal(True)
        self.resize(560, 460)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        header = QLabel(f"文件：{self._path}")
        header.setStyleSheet("color: #6b7280; font-size: 11px;")
        header.setWordWrap(True)
        root.addWidget(header)

        # 字段预览
        form = QFormLayout()
        title = str(self._payload.get("title", "")).strip()
        desc = str(self._payload.get("desc", "") or self._payload.get("digest", "")).strip()
        url = str(self._payload.get("url", "")).strip()
        source = str(self._payload.get("sourcename", "")).strip()
        thumb = str(self._payload.get("thumburl", "")).strip()
        appid = str(self._payload.get("appid", "")).strip()
        media_path = str(self._payload.get("path", "") or self._payload.get("video_path", "")).strip()
        thumb_path = str(self._payload.get("thumb_path", "")).strip()
        duration = str(self._payload.get("duration", "")).strip()

        if self._kind in {"image", "video"}:
            form.addRow("路径", QLabel(media_path or "(无)"))
            if thumb_path:
                form.addRow("封面路径", QLabel(thumb_path))
            if duration:
                form.addRow("时长", QLabel(duration))
        else:
            form.addRow("标题", QLabel(title or "(无)"))
            form.addRow("摘要", QLabel(desc or "(无)"))
            if url:
                form.addRow("链接", QLabel(url))
            if source:
                form.addRow("来源", QLabel(source))
            if thumb:
                form.addRow("封面", QLabel(thumb))
            if appid:
                form.addRow("appid", QLabel(appid))
        root.addLayout(form)

        # 用户输入：名称 + 标签
        input_form = QFormLayout()
        self._name_edit = QLineEdit()
        default_name = str(self._payload.get("name", "")).strip() or title or self._path.stem
        self._name_edit.setText(default_name)
        self._name_edit.setPlaceholderText("给这条素材起个便于识别的名字")
        input_form.addRow("素材名 *", self._name_edit)

        self._tags_edit = QLineEdit()
        existing_tags = self._payload.get("tags") or []
        if isinstance(existing_tags, list):
            self._tags_edit.setText("、".join(str(t).strip() for t in existing_tags if str(t).strip()))
        self._tags_edit.setPlaceholderText("标签用中文顿号或英文逗号分隔（可选）")
        input_form.addRow("标签", self._tags_edit)
        root.addLayout(input_form)

        # 完整 payload 可展开查看（只读）
        self._raw = QTextEdit()
        import json
        self._raw.setReadOnly(True)
        self._raw.setPlainText(json.dumps(self._payload, ensure_ascii=False, indent=2))
        self._raw.setMaximumHeight(140)
        root.addWidget(QLabel("完整字段（只读）："))
        root.addWidget(self._raw, 1)

        # 按钮：保存 / 丢弃 / 稍后
        btns = QDialogButtonBox()
        self._btn_save = QPushButton("保存")
        self._btn_save.setDefault(True)
        self._btn_discard = QPushButton("丢弃")
        self._btn_defer = QPushButton("稍后再定")
        btns.addButton(self._btn_save, QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton(self._btn_discard, QDialogButtonBox.ButtonRole.DestructiveRole)
        btns.addButton(self._btn_defer, QDialogButtonBox.ButtonRole.RejectRole)
        self._btn_save.clicked.connect(self._on_save)
        self._btn_discard.clicked.connect(self._on_discard)
        self._btn_defer.clicked.connect(self._on_defer)
        root.addWidget(btns)

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少名称", "请为素材命名后再保存。")
            return
        self._decision = self.RESULT_SAVE
        self.accept()

    def _on_discard(self) -> None:
        ans = QMessageBox.question(
            self,
            "确认丢弃",
            "丢弃后 pending 文件会被删除，无法恢复。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._decision = self.RESULT_DISCARD
        self.accept()

    def _on_defer(self) -> None:
        self._decision = self.RESULT_DEFER
        self.reject()

    # 对外访问
    def decision(self) -> int:
        return self._decision

    def entered_name(self) -> str:
        return self._name_edit.text().strip()

    def entered_tags(self) -> list[str]:
        raw = self._tags_edit.text().replace("、", ",")
        return [t.strip() for t in raw.split(",") if t.strip()]

    @staticmethod
    def load_payload(pending_path: Path) -> dict[str, Any]:
        payload = _SECURE_STORE.load_json(pending_path, default={})
        if not isinstance(payload, dict):
            return {}
        return payload


__all__ = ["PendingTemplateDialog", "CardCopyDialog"]
