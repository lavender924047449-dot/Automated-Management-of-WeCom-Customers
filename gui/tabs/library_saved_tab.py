from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.broadcast import image_templates as image_tpl_mod
from app.broadcast import oa_templates as oa_tpl_mod
from app.broadcast import video_templates as video_tpl_mod
from app.broadcast import wxa_templates as wxa_tpl_mod
from app.broadcast.dto import AssetRowDTO
from app.broadcast.image_templates import list_templates as list_image_templates
from app.broadcast.image_templates import load_template as load_image_template
from app.broadcast.library import TextTemplate, VoiceLibrary, VoiceLibraryEntry
from app.broadcast.oa_templates import list_templates as list_oa_templates
from app.broadcast.oa_templates import load_template as load_oa_template
from app.broadcast.preview import PreviewService
from app.broadcast.video_templates import list_templates as list_video_templates
from app.broadcast.video_templates import load_template as load_video_template
from app.broadcast.wxa_templates import list_templates as list_wxa_templates
from app.broadcast.wxa_templates import load_template as load_wxa_template
from gui.i18n import ASSET_TYPE_FILTER_OPTIONS, asset_type_from_filter, asset_type_to_zh
from gui.widgets.asset_preview import AssetPreviewPanel
from gui.widgets.pending_template_dialog import CardCopyDialog


@dataclass
class AssetRow:
    asset_type: str
    item_id: str
    name: str
    tags: list[str]
    usage_count: int
    archived: bool
    updated_at: str
    source: str
    is_unnamed: bool = False
    variants: list[str] | None = None
    payload: dict[str, object] | None = None


class LibrarySavedTab(QWidget):
    """已保存素材：类型过滤、搜索、行内编辑与归档管理。"""

    libraryChanged = Signal(list)
    libraryAssetsChanged = Signal(list)

    def __init__(self, *, preview_service_provider: Callable[[], PreviewService]) -> None:
        super().__init__()
        self._account_id = "default"
        self._preview_service_provider = preview_service_provider
        self._library = VoiceLibrary.load(account_id=self._account_id)
        self._rows: list[AssetRow] = []
        self._row_map: dict[int, AssetRow] = {}
        self._updating = False
        self._build_ui()
        self.reload_from_store()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self._type_filter = QComboBox()
        self._type_filter.addItems(ASSET_TYPE_FILTER_OPTIONS)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索：名称 / 编码")
        self._show_archived = QCheckBox("显示已归档")
        self._btn_edit_card = QPushButton("编辑卡片标题…")
        self._btn_edit_card.setToolTip("为选中的公众号/小程序卡片补齐标题与摘要（对方收到无标题时用）")
        self._btn_edit_card.setEnabled(False)
        self._btn_refresh = QPushButton("刷新")
        bar.addWidget(QLabel("类型"))
        bar.addWidget(self._type_filter)
        bar.addWidget(self._search, 1)
        bar.addWidget(self._show_archived)
        bar.addWidget(self._btn_edit_card)
        bar.addWidget(self._btn_refresh)
        root.addLayout(bar)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["类型", "名字", "使用次数", "归档态", "更新时间"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # 改名改走 F2 / 右键「重命名」；双击「名字」列专用于打开详情浮层。
        self._table.setEditTriggers(QTableWidget.EditTrigger.EditKeyPressed)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        root.addWidget(self._table, 1)

        # 详情面板：默认隐藏的右半宽浮层，双击名字弹出、点击面板外收起。
        self._preview = AssetPreviewPanel(preview_service_provider=self._preview_service_provider)
        self._preview.setParent(self)
        # QWidget 子类必须开启 WA_StyledBackground，样式表里的背景色才会真正绘制，
        # 否则浮层背景透明、会透出下方表格内容（表现为详情页“断开”）。
        self._preview.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._preview.setAutoFillBackground(True)
        self._preview.setStyleSheet(
            "AssetPreviewPanel { background-color: palette(base); border-left: 1px solid palette(mid); }"
        )
        self._preview.hide()
        QApplication.instance().installEventFilter(self)

        self._type_filter.currentTextChanged.connect(self._render_table)
        self._search.textChanged.connect(self._render_table)
        self._show_archived.toggled.connect(self._render_table)
        self._btn_refresh.clicked.connect(self.reload_from_store)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.itemSelectionChanged.connect(self._sync_preview)
        self._table.itemSelectionChanged.connect(self._sync_edit_card_button)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self._btn_edit_card.clicked.connect(self._on_edit_card_clicked)
        self._preview.textVariantsChanged.connect(self._on_text_variants_changed)

    # ------------------------------------------------------------------
    # 详情浮层：双击名字弹出（占右半宽），点击面板外区域收起
    # ------------------------------------------------------------------
    def _on_cell_double_clicked(self, _row: int, _column: int) -> None:
        self._sync_preview()
        self._show_preview_overlay()

    def _show_preview_overlay(self) -> None:
        geo = self._table.geometry()
        half = max(1, geo.width() // 2)
        self._preview.setGeometry(geo.left() + geo.width() - half, geo.top(), half, geo.height())
        self._preview.show()
        self._preview.raise_()

    def _hide_preview_overlay(self) -> None:
        if self._preview.isVisible():
            self._preview.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._preview.isVisible():
            self._show_preview_overlay()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.MouseButtonPress and self._preview.isVisible():
            global_pos = event.globalPosition().toPoint()
            top_left = self._preview.mapToGlobal(QPoint(0, 0))
            rect = QRect(top_left, self._preview.size())
            if not rect.contains(global_pos):
                self._hide_preview_overlay()
        return super().eventFilter(obj, event)

    def set_account(self, account_id: str) -> None:
        self._account_id = account_id.strip() or "default"
        self._library = VoiceLibrary.load(account_id=self._account_id)
        self.reload_from_store()

    def active_voice_entries(self) -> list[VoiceLibraryEntry]:
        return self._library.list_active()

    def active_assets(self) -> list[AssetRowDTO]:
        out: list[AssetRowDTO] = []
        for row in self._rows:
            if row.archived or row.is_unnamed:
                continue
            out.append(
                AssetRowDTO.model_validate(
                    {
                        "asset_type": row.asset_type,
                        "item_id": row.item_id,
                        "name": row.name,
                        "tags": list(row.tags),
                        **(row.payload or {}),
                    }
                )
            )
        return out

    def unnamed_count(self) -> int:
        count = 0
        for item in self._library.list():
            if not item.note.strip() and not item.label.strip():
                count += 1
        return count

    def run_unnamed_gc(self, ttl: int = 1800) -> int:
        removed = self._library.gc_unnamed(ttl)
        if removed > 0:
            self._library.save()
        return removed

    def reload_from_store(self) -> None:
        self._library = VoiceLibrary.load(account_id=self._account_id)
        voice_rows = [self._to_voice_row(item) for item in self._library.list()]
        text_rows = [self._to_text_row(item) for item in self._library.list_text_templates(include_archived=True)]
        oa_rows = self._load_oa_rows(self._account_id)
        wxa_rows = self._load_wxa_rows(self._account_id)
        image_rows = self._load_image_rows(self._account_id)
        video_rows = self._load_video_rows(self._account_id)
        # F3.2：未命名语音显示 `未命名 #NNN`，双击“名字”列即命名（复用 note 编辑通道）
        merged = [*voice_rows, *text_rows, *oa_rows, *wxa_rows, *image_rows, *video_rows]
        self._assign_unnamed_placeholders(merged)
        self._rows = merged
        self._render_table()
        self.libraryChanged.emit(self.active_voice_entries())
        self.libraryAssetsChanged.emit(self.active_assets())

    def _render_table(self) -> None:
        self._updating = True
        try:
            rows = self._filter_rows()
            self._row_map.clear()
            self._table.setRowCount(len(rows))
            for idx, row in enumerate(rows):
                self._row_map[idx] = row
                self._table.setItem(idx, 0, self._readonly_item(asset_type_to_zh(row.asset_type)))
                self._table.setItem(idx, 1, QTableWidgetItem(row.name))
                self._table.setItem(idx, 2, self._readonly_item(str(row.usage_count)))
                self._table.setItem(idx, 3, self._readonly_item("已归档" if row.archived else "启用中"))
                self._table.setItem(idx, 4, self._readonly_item(row.updated_at))
            self._sync_preview()
        finally:
            self._updating = False

    def _filter_rows(self) -> list[AssetRow]:
        selected_type = asset_type_from_filter(self._type_filter.currentText())
        keyword = self._search.text().strip().lower()
        include_archived = self._show_archived.isChecked()
        out: list[AssetRow] = []
        for row in self._rows:
            if selected_type and row.asset_type != selected_type:
                continue
            if not include_archived and row.archived:
                continue
            if keyword:
                corpus = f"{row.name} {row.item_id}".lower()
                if keyword not in corpus:
                    continue
            out.append(row)
        return out

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        row_idx = item.row()
        row = self._row_map.get(row_idx)
        if row is None or item.column() != 1:
            return
        try:
            if row.source == "voice":
                self._update_voice_item(row, item.text())
                self._library.save()
            elif row.source == "text":
                self._update_text_item(row, item.text())
                self._library.save()
            elif row.source in {"image", "video", "oa", "wxa"}:
                # F3.5：image/video/oa/wxa 命名 = load → set name/去 unnamed → save
                self._update_template_item(row, item.text())
        except Exception as exc:
            logger.warning("保存素材改名失败 source={} item_id={} err={}", row.source, row.item_id, exc)
            QMessageBox.warning(self, "保存失败", "保存失败，请稍后重试。")
        self.reload_from_store()

    def _on_context_menu(self, pos) -> None:
        idx = self._table.indexAt(pos)
        if not idx.isValid():
            return
        row = self._row_map.get(idx.row())
        if row is None:
            return
        menu = QMenu(self)
        act_rename = menu.addAction("重命名")
        act_archive = None
        act_unarchive = None
        act_edit_card = None
        if row.source in {"voice", "text"}:
            act_archive = menu.addAction("归档")
            act_unarchive = menu.addAction("取消归档")
        if row.source in {"oa", "wxa"}:
            act_edit_card = menu.addAction("编辑卡片文案…")
        act_delete = menu.addAction("删除")
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_rename:
            name_item = self._table.item(idx.row(), 1)
            if name_item is not None:
                self._table.editItem(name_item)
            return
        try:
            if act_archive is not None and chosen == act_archive:
                self._set_archived(row, True)
            elif act_unarchive is not None and chosen == act_unarchive:
                self._set_archived(row, False)
            elif act_edit_card is not None and chosen == act_edit_card:
                self._edit_card_copy(row)
                return
            elif chosen == act_delete:
                self._delete_row(row)
            self._library.save()
        except Exception as exc:
            logger.warning("素材操作失败 action_context={} item_id={} err={}", row.source, row.item_id, exc)
            QMessageBox.warning(self, "操作失败", "操作失败，请稍后重试。")
        self.reload_from_store()

    def _selected_row(self) -> "AssetRow | None":
        sel = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not sel:
            return None
        return self._row_map.get(sel[0].row())

    def _sync_edit_card_button(self) -> None:
        row = self._selected_row()
        self._btn_edit_card.setEnabled(bool(row and row.source in {"oa", "wxa"}))

    def _on_edit_card_clicked(self) -> None:
        row = self._selected_row()
        if row is None or row.source not in {"oa", "wxa"}:
            QMessageBox.information(self, "请选择卡片", "请先在列表里选中一条公众号或小程序卡片。")
            return
        self._edit_card_copy(row)

    def _edit_card_copy(self, row: AssetRow) -> None:
        """编辑 oa/wxa 卡片的标题与摘要，写回模板 JSON。"""
        module = {"oa": oa_tpl_mod, "wxa": wxa_tpl_mod}.get(row.source)
        if module is None:
            return
        try:
            payload = module.load_template(row.item_id, account_id=self._account_id)
        except Exception as exc:
            logger.warning("加载卡片模板失败 source={} item_id={} err={}", row.source, row.item_id, exc)
            QMessageBox.warning(self, "加载失败", "读取卡片内容失败，请稍后重试。")
            return
        dlg = CardCopyDialog(payload, kind=row.source, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload["title"] = dlg.entered_title()
        payload["desc"] = dlg.entered_desc()
        try:
            module.save_template(
                payload, template_id=row.item_id, account_id=self._account_id, pending=False
            )
        except Exception as exc:
            logger.warning("保存卡片模板失败 source={} item_id={} err={}", row.source, row.item_id, exc)
            QMessageBox.warning(self, "保存失败", "保存失败，请稍后重试。")
            return
        self.reload_from_store()

    def _sync_preview(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            self._preview.set_asset(None)
            return
        row = self._row_map.get(rows[0].row())
        if row is None:
            self._preview.set_asset(None)
            return
        self._preview.set_asset(
            {
                "asset_type": row.asset_type,
                "name": row.name,
                "tags": row.tags,
                "updated_at": row.updated_at,
                "item_id": row.item_id,
                "variants": row.variants or [],
                **(row.payload or {}),
            }
        )

    def _on_text_variants_changed(self, template_id: str, variants: list[str]) -> None:
        try:
            self._library.update_text_template(template_id, variants=variants)
            self._library.save()
            self.reload_from_store()
        except Exception as exc:
            logger.warning("更新文字变体失败 template_id={} err={}", template_id, exc)
            QMessageBox.warning(self, "更新变体失败", "更新失败，请稍后重试。")

    def _update_voice_item(self, row: AssetRow, value: str) -> None:
        text = value.strip()
        # F3.2：拒绝把占位串 `未命名 #NNN · ...` 落成 note；等价于用户没改
        if text.startswith("未命名 #"):
            return
        self._library.update_note(row.item_id, text)

    def _update_template_item(self, row: AssetRow, value: str) -> None:
        """F3.5：image / video / oa / wxa 的行内改名 → 覆盖写模板 JSON。"""
        text = value.strip()
        if text.startswith("未命名 #"):
            return  # 拒绝把占位串落到 store（用户没实际改）
        module_map = {
            "image": image_tpl_mod,
            "video": video_tpl_mod,
            "oa": oa_tpl_mod,
            "wxa": wxa_tpl_mod,
        }
        module = module_map.get(row.source)
        if module is None:
            return
        payload = module.load_template(row.item_id, account_id=self._account_id)
        payload["name"] = text
        payload["unnamed"] = False
        module.save_template(payload, template_id=row.item_id, account_id=self._account_id, pending=False)

    def _update_text_item(self, row: AssetRow, value: str) -> None:
        self._library.update_text_template(row.item_id, name=value.strip())

    def _set_archived(self, row: AssetRow, archived: bool) -> None:
        if row.source == "voice":
            if archived:
                self._library.archive(row.item_id)
            else:
                self._library.unarchive(row.item_id)
            return
        if row.source == "text":
            if archived:
                self._library.archive_text_template(row.item_id)
            else:
                self._library.unarchive_text_template(row.item_id)

    def _delete_row(self, row: AssetRow) -> None:
        if row.source == "voice":
            self._library.delete(row.item_id)
            return
        if row.source == "text":
            self._library.delete_text_template(row.item_id)
            return
        template_path = str((row.payload or {}).get("template_path", "")).strip()
        if template_path:
            Path(template_path).unlink(missing_ok=True)

    @staticmethod
    def _to_voice_row(item: VoiceLibraryEntry) -> AssetRow:
        name = item.note.strip() or item.label.strip() or item.file
        tags = [item.label] if item.label else []
        return AssetRow(
            asset_type="voice",
            item_id=item.id,
            name=name,
            tags=tags,
            usage_count=item.usage_count,
            archived=item.is_archived,
            updated_at=item.captured_at,
            source="voice",
            is_unnamed=(not item.note.strip() and not item.label.strip()),
            variants=None,
            payload={
                "file": item.file,
                "duration": item.duration,
                "path": item.file if item.file.startswith("/") or ":" in item.file else "",
            },
        )

    @staticmethod
    def _to_text_row(item: TextTemplate) -> AssetRow:
        # kind="link" 的 TextTemplate 在 GUI 里单独归类为「小程序链接」（asset_type="link"），
        # 数据存储仍然是 TextTemplate；发送时按 text 通道走（url 就是唯一 variant）。
        is_link = getattr(item, "kind", "text") == "link"
        asset_type = "link" if is_link else "text"
        payload: dict[str, object] = {}
        if is_link:
            payload["url"] = item.variants[0] if item.variants else ""
        return AssetRow(
            asset_type=asset_type,
            item_id=item.id,
            name=item.name,
            tags=list(item.tags),
            usage_count=0,
            archived=item.is_archived,
            updated_at=item.updated_at,
            source="text",
            is_unnamed=False,
            variants=list(item.variants),
            payload=payload,
        )

    @staticmethod
    def _load_oa_rows(account_id: str) -> list[AssetRow]:
        rows: list[AssetRow] = []
        for fp in list_oa_templates(account_id):
            try:
                payload = load_oa_template(str(fp), account_id=account_id)
            except Exception:
                continue
            template_id = str(payload.get("template_id", "")).strip() or fp.stem
            name = str(payload.get("name", "")).strip() or str(payload.get("title", "")).strip() or template_id
            tags = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()]
            rows.append(
                AssetRow(
                    asset_type="oa",
                    item_id=template_id,
                    name=name,
                    tags=tags,
                    usage_count=0,
                    archived=False,
                    updated_at=str(payload.get("captured_at", "")).strip() or "",
                    source="oa",
                    is_unnamed=bool(payload.get("unnamed")),
                    payload={
                        "template_path": str(fp),
                        "oa_title": str(payload.get("title", "")).strip(),
                        "oa_desc": str(payload.get("desc", "")).strip() or str(payload.get("digest", "")).strip(),
                        "oa_source": str(payload.get("sourcename", "")).strip(),
                        "oa_url": str(payload.get("url", "")).strip(),
                    },
                )
            )
        return rows

    @staticmethod
    def _load_wxa_rows(account_id: str) -> list[AssetRow]:
        rows: list[AssetRow] = []
        for fp in list_wxa_templates(account_id):
            try:
                payload = load_wxa_template(str(fp), account_id=account_id)
            except Exception:
                continue
            template_id = str(payload.get("template_id", "")).strip() or fp.stem
            name = str(payload.get("name", "")).strip() or template_id
            tags = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()]
            rows.append(
                AssetRow(
                    asset_type="link",
                    item_id=template_id,
                    name=name,
                    tags=tags,
                    usage_count=0,
                    archived=False,
                    updated_at=str(payload.get("captured_at", "")).strip() or "",
                    source="wxa",
                    is_unnamed=bool(payload.get("unnamed")),
                    payload={
                        "template_path": str(fp),
                        "url": str(payload.get("url", "")).strip(),
                        "thumb_path": str(payload.get("thumb_path", "")).strip(),
                    },
                )
            )
        return rows

    @staticmethod
    def _load_image_rows(account_id: str) -> list[AssetRow]:
        rows: list[AssetRow] = []
        for fp in list_image_templates(account_id):
            try:
                payload = load_image_template(str(fp), account_id=account_id)
            except Exception:
                continue
            template_id = str(payload.get("template_id", "")).strip() or fp.stem
            name = str(payload.get("name", "")).strip() or template_id
            path = str(payload.get("path", "") or payload.get("file_path", "")).strip()
            tags = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()]
            rows.append(
                AssetRow(
                    asset_type="image",
                    item_id=template_id,
                    name=name,
                    tags=tags,
                    usage_count=0,
                    archived=False,
                    updated_at=str(payload.get("captured_at", "")).strip() or "",
                    source="image",
                    is_unnamed=bool(payload.get("unnamed")),
                    payload={"template_path": str(fp), "path": path},
                )
            )
        return rows

    @staticmethod
    def _load_video_rows(account_id: str) -> list[AssetRow]:
        rows: list[AssetRow] = []
        for fp in list_video_templates(account_id):
            try:
                payload = load_video_template(str(fp), account_id=account_id)
            except Exception:
                continue
            template_id = str(payload.get("template_id", "")).strip() or fp.stem
            name = str(payload.get("name", "")).strip() or template_id
            path = str(payload.get("path", "") or payload.get("video_path", "")).strip()
            thumb_path = str(payload.get("thumb_path", "")).strip()
            duration = payload.get("duration")
            tags = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()]
            rows.append(
                AssetRow(
                    asset_type="video",
                    item_id=template_id,
                    name=name,
                    tags=tags,
                    usage_count=0,
                    archived=False,
                    updated_at=str(payload.get("captured_at", "")).strip() or "",
                    source="video",
                    is_unnamed=bool(payload.get("unnamed")),
                    payload={
                        "template_path": str(fp),
                        "path": path,
                        "thumb_path": thumb_path,
                        "duration": int(duration) if isinstance(duration, int | float) else 0,
                    },
                )
            )
        return rows

    @staticmethod
    def _assign_unnamed_placeholders(rows: list[AssetRow]) -> None:
        """给未命名条目按 captured_at 倒序编号成 `未命名 #NNN · Xs · MM-DD HH:MM`。

        决策 F9：占位编号是**展示逻辑**，不落数据（避免设备重启后编号错乱）。
        - voice：`note` + `label` 均空 → 未命名（F3.2）
        - image/video/oa/wxa（F3.5）：payload["unnamed"] == True → 未命名
        每类型**独立编号**（voice #NNN 与 image #NNN 互不干扰）；双击命名后
        下次 reload 该行 is_unnamed 变 False → 自动脱离占位序列。
        """
        for kind in ("voice", "image", "video", "oa", "wxa"):
            group = [row for row in rows if row.is_unnamed and row.source == kind]
            # captured_at 是 ISO 秒精度字符串，字典序 = 时间序；倒序 => 最新 #001
            group.sort(key=lambda r: r.updated_at, reverse=True)
            for idx, row in enumerate(group, start=1):
                ts = LibrarySavedTab._short_ts(row.updated_at)
                parts = [f"未命名 #{idx:03d}"]
                if kind in ("voice", "video"):
                    duration = int((row.payload or {}).get("duration", 0) or 0)
                    if duration > 0:
                        parts.append(f"{duration}s")
                elif kind == "oa":
                    title = str((row.payload or {}).get("oa_title", "")).strip()
                    if title:
                        parts.append(title[:20])
                if ts:
                    parts.append(ts)
                row.name = " · ".join(parts)

    @staticmethod
    def _short_ts(iso_ts: str) -> str:
        # `2026-09-18T11:30:23` → `09-18 11:30`；宽松匹配，异常直接返回空串
        text = iso_ts.strip()
        if len(text) < 16 or text[4] != "-":
            return ""
        return f"{text[5:10]} {text[11:16]}"

    @staticmethod
    def _readonly_item(value: str) -> QTableWidgetItem:
        item = QTableWidgetItem(value)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item
