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
    QFileDialog,
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
from app.broadcast.asset_bundles import canon_asset_type, delete_bundle, get_bundle, list_bundles, save_bundle
from app.broadcast.asset_names import asset_name_conflict, bundle_name_conflict
from app.broadcast.dto import AssetRowDTO
from app.broadcast.image_templates import list_templates as list_image_templates
from app.broadcast.image_templates import load_template as load_image_template
from app.broadcast.library import TextTemplate, VoiceLibrary, VoiceLibraryEntry
from app.broadcast.link_cards import LINK_CARD_KINDS, card_kind_of
from app.broadcast.oa_templates import list_templates as list_oa_templates
from app.broadcast.oa_templates import load_template as load_oa_template
from app.broadcast.preview import PreviewService
from app.broadcast.video_templates import list_templates as list_video_templates
from app.broadcast.video_templates import load_template as load_video_template
from app.broadcast.wxa_templates import list_templates as list_wxa_templates
from app.broadcast.wxa_templates import load_template as load_wxa_template
from gui.copy_support import add_copy_action, prepare_context_selection
from gui.i18n import ASSET_TYPE_FILTER_OPTIONS, asset_type_from_filter, asset_type_to_zh
from gui.widgets.asset_preview import AssetPreviewPanel
from gui.widgets.pending_template_dialog import CardCopyDialog


@dataclass
class AssetRow:
    asset_type: str
    item_id: str
    name: str
    tags: list[str]
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
    # Step 4: 主模块删除后通知子模块同步移除同一 item_id
    itemsDeleted = Signal(list)  # list of (asset_type, item_id) tuples

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
        self._search.setPlaceholderText("搜索：名字")
        self._show_archived = QCheckBox("显示已归档")
        self._btn_edit_card = QPushButton("编辑卡片标题…")
        self._btn_edit_card.setToolTip("为选中的公众号、企微文档、视频号或小程序补齐标题与摘要")
        self._btn_edit_card.setEnabled(False)
        self._btn_refresh = QPushButton("刷新")
        self._btn_export = QPushButton("导出素材库")
        self._btn_export.setToolTip("将当前账号的全部素材打包导出为 zip")
        self._btn_import = QPushButton("导入素材库")
        self._btn_import.setToolTip("从 zip 文件导入素材（支持冲突策略选择）")
        bar.addWidget(QLabel("类型"))
        bar.addWidget(self._type_filter)
        bar.addWidget(self._search, 1)
        bar.addWidget(self._show_archived)
        bar.addWidget(self._btn_edit_card)
        bar.addWidget(self._btn_export)
        bar.addWidget(self._btn_import)
        bar.addWidget(self._btn_refresh)
        root.addLayout(bar)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["类型", "名字", "归档态", "录入时间"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)  # Step 4: 多选
        # 改名改走 F2 / 右键「重命名」；双击「名字」列就地重命名，双击其他列预览。
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
        self._btn_export.clicked.connect(self._on_export_clicked)
        self._btn_import.clicked.connect(self._on_import_clicked)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.itemSelectionChanged.connect(self._sync_preview)
        self._table.itemSelectionChanged.connect(self._sync_edit_card_button)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self._btn_edit_card.clicked.connect(self._on_edit_card_clicked)

    # ------------------------------------------------------------------
    # 详情浮层：双击名字弹出（占右半宽），点击面板外区域收起
    # ------------------------------------------------------------------
    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        asset = self._row_map.get(row)
        if asset is not None and asset.asset_type == "bundle":
            self._open_bundle_readonly(asset.item_id)
            return
        # 双击名字列就地重命名；双击其他列查看详情
        if column == 1:
            item = self._table.item(row, 1)
            if item:
                self._table.editItem(item)
        else:
            self._show_row_detail(row)

    def _show_row_detail(self, row_index: int) -> None:
        if self._table.currentRow() != row_index:
            self._table.setCurrentCell(row_index, 0)
        self._sync_preview()
        self._show_preview_overlay()

    def _open_bundle_readonly(self, bundle_id: str) -> None:
        from gui.tabs.library_bundle_tab import BundleDetailDialog

        bundle = get_bundle(bundle_id, account_id=self._account_id)
        if bundle is None:
            QMessageBox.information(self, "素材包不存在", "这个素材包已经不在素材库里。")
            return
        asset_map: dict[tuple[str, str], dict] = {}
        for row in self._rows:
            if row.is_unnamed or row.asset_type == "bundle":
                continue
            payload = dict(row.payload or {})
            payload["asset_type"] = row.asset_type
            payload["item_id"] = row.item_id
            payload["name"] = row.name
            payload["variants"] = list(row.variants or [])
            asset_map[(row.asset_type.strip().lower(), row.item_id.strip())] = payload
        dlg = BundleDetailDialog(
            bundle,
            asset_map,
            self,
            preview_service_provider=self._preview_service_provider,
            account_id=self._account_id,
            readonly=True,
        )
        dlg.exec()

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

    def asset_identity_keys(self) -> set[tuple[str, str]]:
        """当前素材库里仍存在的素材（含归档、未命名）。不含素材包本身。"""
        keys: set[tuple[str, str]] = set()
        for row in self._rows:
            if row.source == "bundle" or row.asset_type == "bundle":
                continue
            kind = canon_asset_type(row.asset_type)
            item_id = row.item_id.strip()
            if kind and item_id:
                keys.add((kind, item_id))
            # 旧素材包把小程序卡片记成 link，刷新时不要把它当成丢失素材清掉。
            if row.source == "wxa" and item_id:
                keys.add(("wxa", item_id))
                keys.add(("link", item_id))
        return keys

    def active_assets(self) -> list[AssetRowDTO]:
        out: list[AssetRowDTO] = []
        for row in self._rows:
            if row.archived or row.is_unnamed:
                continue
            payload = dict(row.payload or {})
            # Step 4: 把预览所需的额外字段统一放进 extra，
            # 避免 Pydantic 静默丢掉 oa_title/url/variants 等非模型字段。
            known_keys = {"path", "thumb_path"}
            extra_data: dict = {}
            leftover: dict = {}
            for k, v in payload.items():
                if k in known_keys:
                    leftover[k] = v
                else:
                    extra_data[k] = v
            # 保留 variants 列表
            if row.variants:
                extra_data["variants"] = row.variants
            if row.updated_at:
                extra_data["entered_at"] = row.updated_at
            out.append(
                AssetRowDTO(
                    asset_type=row.asset_type,
                    item_id=row.item_id,
                    name=row.name,
                    tags=list(row.tags),
                    path=str(leftover.get("path", "")).strip(),
                    thumb_path=str(leftover.get("thumb_path", "")).strip(),
                    extra=extra_data,
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
        bundle_rows = self._load_bundle_rows(self._account_id)
        # F3.2：未命名语音显示 `未命名 #NNN`，双击“名字”列即命名（复用 note 编辑通道）
        merged = [*voice_rows, *text_rows, *oa_rows, *wxa_rows, *image_rows, *video_rows, *bundle_rows]
        self._assign_unnamed_placeholders(merged)
        merged.sort(key=lambda row: row.updated_at or "", reverse=True)
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
                self._table.setItem(idx, 2, self._readonly_item("已归档" if row.archived else "启用中"))
                self._table.setItem(idx, 3, self._readonly_item(row.updated_at))
            self._sync_preview()
        finally:
            self._updating = False

    def _filter_rows(self) -> list[AssetRow]:
        selected_type = asset_type_from_filter(self._type_filter.currentText())
        keyword = self._search.text().strip().lower()
        include_archived = self._show_archived.isChecked()
        out: list[AssetRow] = []
        for row in self._rows:
            if row.is_unnamed:
                continue
            if selected_type and row.asset_type != selected_type:
                continue
            if not include_archived and row.archived:
                continue
            if keyword:
                corpus = f"{row.name} {row.item_id}".lower()
                if keyword not in corpus:
                    continue
            out.append(row)
        out.sort(key=lambda row: row.updated_at or "", reverse=True)
        return out

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        row_idx = item.row()
        row = self._row_map.get(row_idx)
        if row is None or item.column() != 1:
            return
        new_name = item.text().strip()
        if row.source == "bundle":
            conflict = bundle_name_conflict(self._account_id, new_name, exclude_id=row.item_id)
        else:
            conflict = asset_name_conflict(
                self._account_id,
                row.asset_type,
                new_name,
                exclude_item_id=row.item_id,
            )
        if conflict:
            QMessageBox.warning(self, "名称重复", conflict)
            self.reload_from_store()
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
            elif row.source == "bundle":
                self._update_bundle_name(row, item.text())
        except Exception as exc:
            logger.warning("保存素材改名失败 source={} item_id={} err={}", row.source, row.item_id, exc)
            QMessageBox.warning(self, "保存失败", "保存失败，请稍后重试。")
        self.reload_from_store()

    def _on_context_menu(self, pos) -> None:
        idx = self._table.indexAt(pos)
        if not idx.isValid():
            return
        prepare_context_selection(self._table, idx)
        row = self._row_map.get(idx.row())
        if row is None:
            return
        selected_rows = self._selected_asset_rows() or [row]
        multi = len(selected_rows) > 1

        menu = QMenu(self)
        act_copy = add_copy_action(menu, self._table)
        act_detail = menu.addAction("查看详情")
        # 多选时重命名只改当前行
        act_rename = menu.addAction("重命名")
        act_archive = None
        act_unarchive = None
        act_edit_card = None
        if not multi and row.source in {"voice", "text"}:
            act_archive = menu.addAction("归档")
            act_unarchive = menu.addAction("取消归档")
        if not multi and row.source in {*LINK_CARD_KINDS, "wxa"}:
            act_edit_card = menu.addAction("编辑卡片文案…")
        delete_label = f"删除（{len(selected_rows)} 条）" if multi else "删除"
        act_delete = menu.addAction(delete_label)
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is None or chosen == act_copy:
            return
        if chosen == act_detail:
            if row.asset_type == "bundle":
                self._open_bundle_readonly(row.item_id)
            else:
                self._show_row_detail(idx.row())
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
                deleted_items: list[tuple[str, str, str]] = []
                for r in selected_rows:
                    path = str((r.payload or {}).get("path", "")).strip()
                    self._delete_row(r)
                    deleted_items.append((r.asset_type, r.item_id, path))
                    if r.source == "wxa":
                        deleted_items.append(("link", r.item_id, ""))
                if deleted_items:
                    # 先落盘，再通知素材包刷新。否则刷新会从磁盘把刚删的语音/文字读回来。
                    self._library.save()
                    self.itemsDeleted.emit(deleted_items)
            self._library.save()
        except Exception as exc:
            logger.warning("素材操作失败 action_context={} item_id={} err={}", row.source, row.item_id, exc)
            QMessageBox.warning(self, "操作失败", "操作失败，请稍后重试。")
        self.reload_from_store()

    def _selected_asset_rows(self) -> list[AssetRow]:
        seen: set[int] = set()
        rows: list[AssetRow] = []
        for index in self._table.selectedIndexes():
            if not index.isValid() or index.row() in seen:
                continue
            seen.add(index.row())
            row = self._row_map.get(index.row())
            if row is not None:
                rows.append(row)
        return rows

    def _selected_row(self) -> "AssetRow | None":
        row_index = self._table.currentRow()
        if row_index < 0:
            indexes = self._table.selectedIndexes()
            row_index = indexes[0].row() if indexes else -1
        if row_index < 0:
            return None
        return self._row_map.get(row_index)

    def _sync_edit_card_button(self) -> None:
        row = self._selected_row()
        self._btn_edit_card.setEnabled(bool(row and row.source in {*LINK_CARD_KINDS, "wxa"}))

    def _on_edit_card_clicked(self) -> None:
        row = self._selected_row()
        if row is None or row.source not in {*LINK_CARD_KINDS, "wxa"}:
            QMessageBox.information(self, "请选择卡片", "请先在列表里选中一条公众号、企微文档、视频号或小程序。")
            return
        self._edit_card_copy(row)

    def _edit_card_copy(self, row: AssetRow) -> None:
        """编辑 oa/wxa 卡片的标题与摘要，写回模板 JSON。"""
        module = {"oa": oa_tpl_mod, "doc": oa_tpl_mod, "channel": oa_tpl_mod, "wxa": wxa_tpl_mod}.get(row.source)
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
        row = self._selected_row()
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

    def _on_export_clicked(self) -> None:
        """导出当前账号素材库为 zip。"""
        from datetime import datetime
        from app.broadcast.library_export import export_library
        default_name = f"素材库导出_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "导出素材库", default_name, "ZIP 文件 (*.zip)")
        if not path:
            return
        try:
            export_library(self._account_id, Path(path))
            QMessageBox.information(self, "导出成功", f"素材库已导出至：\n{path}")
        except Exception as exc:
            logger.warning("导出素材库失败 err={}", exc)
            QMessageBox.warning(self, "导出失败", f"导出失败：{exc}")

    def _on_import_clicked(self) -> None:
        """从 zip 导入素材库。没有重复文件时直接写入；有重复时只问一次覆盖或跳过。"""
        from app.broadcast.library_export import import_library, list_import_conflicts
        path, _ = QFileDialog.getOpenFileName(self, "导入素材库", "", "ZIP 文件 (*.zip)")
        if not path:
            return
        try:
            conflicts = list_import_conflicts(self._account_id, Path(path))
        except Exception as exc:
            logger.warning("检查导入冲突失败 err={}", exc)
            QMessageBox.warning(self, "导入失败", f"导入失败：{exc}")
            return
        policy = "overwrite"
        if conflicts:
            policy = self._ask_import_conflict_policy(len(conflicts))
            if not policy:
                return
        try:
            result = import_library(self._account_id, Path(path), conflict_policy=policy)
            msg = f"导入完成：写入 {result.written} 个文件，跳过 {result.skipped} 个。"
            if result.errors:
                msg += f"\n{len(result.errors)} 个错误。"
            QMessageBox.information(self, "导入完成", msg)
            self.reload_from_store()
        except Exception as exc:
            logger.warning("导入素材库失败 err={}", exc)
            QMessageBox.warning(self, "导入失败", f"导入失败：{exc}")

    def _ask_import_conflict_policy(self, count: int) -> str:
        """已有素材时只问一次。关闭窗口则取消导入。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("导入素材库")
        box.setText(f"有{count}个文件已存在，覆盖还是跳过？")
        overwrite_btn = box.addButton("覆盖", QMessageBox.ButtonRole.AcceptRole)
        skip_btn = box.addButton("跳过", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(skip_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite_btn:
            return "overwrite"
        if clicked is skip_btn:
            return "skip"
        return ""

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
            "doc": oa_tpl_mod,
            "channel": oa_tpl_mod,
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

    def _update_bundle_name(self, row: AssetRow, value: str) -> None:
        """重命名素材包。"""
        text = value.strip()
        if not text:
            return
        from app.broadcast.asset_bundles import get_bundle as _get_bundle
        bundle = _get_bundle(row.item_id, account_id=self._account_id)
        if bundle is None:
            return
        updated = bundle.model_copy(update={"name": text})
        save_bundle(updated, account_id=self._account_id)

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
        if row.source == "bundle":
            delete_bundle(row.item_id, account_id=self._account_id)
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
        # 数据仍可能是 kind=link 的旧文字模板，界面统一归到文字。
        return AssetRow(
            asset_type="text",
            item_id=item.id,
            name=item.name,
            tags=list(item.tags),
            archived=item.is_archived,
            updated_at=item.created_at,
            source="text",
            is_unnamed=bool(item.unnamed),
            variants=list(item.variants),
            payload={},
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
            kind = card_kind_of(payload)
            rows.append(
                AssetRow(
                    asset_type=kind,
                    item_id=template_id,
                    name=name,
                    tags=tags,
                    archived=False,
                    updated_at=str(payload.get("captured_at", "")).strip() or "",
                    source=kind,
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
            name = str(payload.get("name", "")).strip() or str(payload.get("title", "")).strip() or template_id
            tags = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()]
            rows.append(
                AssetRow(
                    asset_type="wxa",
                    item_id=template_id,
                    name=name,
                    tags=tags,
                    archived=False,
                    updated_at=str(payload.get("captured_at", "")).strip() or "",
                    source="wxa",
                    is_unnamed=bool(payload.get("unnamed")),
                    payload={
                        "template_path": str(fp),
                        "wxa_title": str(payload.get("title", "")).strip(),
                        "wxa_app": str(payload.get("appName", "")).strip(),
                        "wxa_user": str(payload.get("username", "")).strip(),
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
    def _load_bundle_rows(account_id: str) -> list[AssetRow]:
        """加载素材包行到已保存素材列表。"""
        rows: list[AssetRow] = []
        for bundle in list_bundles(account_id):
            rows.append(
                AssetRow(
                    asset_type="bundle",
                    item_id=bundle.id,
                    name=bundle.name,
                    tags=list(bundle.tags),
                    archived=False,
                    updated_at=bundle.created_at,
                    source="bundle",
                    is_unnamed=False,
                    payload={
                        "items_count": len(bundle.items),
                        "step_gaps": list(bundle.step_gaps),
                        "contact_gap": bundle.contact_gap,
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
        for kind in ("voice", "image", "video", "oa", "doc", "channel", "wxa", "text"):
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
                elif kind in {"oa", "doc", "channel"}:
                    title = str((row.payload or {}).get("oa_title", "")).strip()
                    if title:
                        parts.append(title[:20])
                elif kind == "text":
                    variants = row.variants or []
                    preview = str(variants[0]).strip() if variants else ""
                    if preview:
                        parts.append(preview[:20])
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
