from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.broadcast import image_templates as image_tpl_mod
from app.broadcast import oa_templates as oa_tpl_mod
from app.broadcast import video_templates as video_tpl_mod
from app.broadcast import wxa_templates as wxa_tpl_mod
from app.broadcast.asset_bundles import (
    AssetBundle,
    BundleItem,
    canon_asset_type,
    delete_bundle,
    get_bundle,
    list_bundles,
    prune_missing_items,
    remove_referenced_items,
    save_bundle,
)
from app.broadcast.asset_names import asset_name_conflict, bundle_name_conflict
from app.broadcast.library import VoiceLibrary
from app.broadcast.preview import PreviewService
from gui.asset_order import newest_first
from gui.copy_support import add_copy_action
from gui.i18n import ASSET_TYPE_FILTER_OPTIONS, asset_type_from_filter, asset_type_to_zh
from gui.widgets.asset_preview import AssetPreviewPanel


def rename_underlying_asset(asset_type: str, item_id: str, new_name: str, account_id: str) -> None:
    """把素材包内某条素材的名字写回底层素材库（全局生效）。"""
    name = new_name.strip()
    if not name or not item_id:
        return
    conflict = asset_name_conflict(account_id, asset_type, name, exclude_item_id=item_id)
    if conflict:
        raise ValueError(conflict)
    kind = asset_type.strip().lower()
    if kind == "voice":
        lib = VoiceLibrary.load(account_id=account_id)
        lib.update_note(item_id, name)
        lib.save()
        return
    if kind == "text":
        lib = VoiceLibrary.load(account_id=account_id)
        lib.update_text_template(item_id, name=name)
        lib.save()
        return
    if kind == "link":
        # 手工创建的「小程序链接」= TextTemplate(kind="link")，走 text 通道改名。
        # 兼容遗留 wxa 探针写入的 wxa_templates/*.json：改名时找不到就回退到 wxa 模块。
        lib = VoiceLibrary.load(account_id=account_id)
        try:
            lib.update_text_template(item_id, name=name)
            lib.save()
            return
        except KeyError:
            pass  # 落到下方 wxa_tpl_mod 分支
    module = {
        "image": image_tpl_mod,
        "video": video_tpl_mod,
        "oa": oa_tpl_mod,
        "doc": oa_tpl_mod,
        "channel": oa_tpl_mod,
        "wxa": wxa_tpl_mod,
        "link": wxa_tpl_mod,
    }.get(kind)
    if module is None:
        return
    payload = module.load_template(item_id, account_id=account_id)
    payload["name"] = name
    payload["unnamed"] = False
    module.save_template(payload, template_id=item_id, account_id=account_id, pending=False)


class BundleDetailDialog(QDialog):
    """素材包详情：重命名素材包 + 拖拽排序 + 双击查看详情 + 右键重命名。"""

    def __init__(
        self,
        bundle: AssetBundle,
        asset_map: dict[tuple[str, str], dict[str, Any]],
        parent: QWidget | None = None,
        *,
        preview_service_provider: Callable[[], PreviewService] | None = None,
        account_id: str = "default",
        readonly: bool = False,
    ) -> None:
        super().__init__(parent)
        self._readonly = bool(readonly)
        self._account_id = account_id.strip() or "default"
        self._bundle_id = bundle.id
        self._tags = list(bundle.tags)
        self._asset_map = asset_map
        self._preview_service_provider = preview_service_provider
        self.setWindowTitle("素材包详情")
        self.resize(460, 520)

        root = QVBoxLayout(self)
        form = QHBoxLayout()
        form.addWidget(QLabel("素材包名称"))
        self._name_edit = QLineEdit(bundle.name)
        self._name_edit.setReadOnly(self._readonly)
        form.addWidget(self._name_edit, 1)
        root.addLayout(form)

        hint = "包含素材（双击查看详情）" if self._readonly else "包含素材（可拖拽排序；双击查看详情；右键或下方按钮改名）"
        root.addWidget(QLabel(hint))
        self._items = QListWidget()
        if not self._readonly:
            self._items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._items.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._items.itemDoubleClicked.connect(self._open_material_preview)
        self._items.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._items.customContextMenuRequested.connect(self._on_item_context_menu)
        root.addWidget(self._items, 1)
        for idx, item in enumerate(bundle.items):
            stored = item.asset_type.strip().lower()
            key_id = item.item_id.strip()
            row = asset_map.get((stored, key_id), {})
            show_type = stored
            if not row and stored in {"oa", "doc", "channel"}:
                for alt in ("doc", "channel", "oa"):
                    alt_row = asset_map.get((alt, key_id), {})
                    if alt_row:
                        row = alt_row
                        show_type = alt
                        break
            if stored == "link" and not row:
                wxa_row = asset_map.get(("wxa", key_id), {})
                if wxa_row:
                    row = wxa_row
                    show_type = "wxa"
            name = str(row.get("name", "")).strip()
            if not name or name == key_id:
                from app.broadcast.asset_preview_source import lookup_asset_name

                name = lookup_asset_name(self._account_id, show_type, item_id=key_id) or "未命名"
            self._add_material_item(show_type, key_id, name)
            # 在素材条目之间插入间隔行
            if idx < len(bundle.items) - 1:
                gap = bundle.step_gaps[idx] if idx < len(bundle.step_gaps) else 3.0
                self._add_gap_item(gap, editable=not self._readonly)

        if not self._readonly:
            actions = QHBoxLayout()
            self._btn_rename = QPushButton("重命名素材")
            self._btn_remove = QPushButton("移除选中")
            self._btn_rename.clicked.connect(self._rename_selected_material)
            self._btn_remove.clicked.connect(self._remove_selected_material)
            actions.addWidget(self._btn_rename)
            actions.addWidget(self._btn_remove)
            actions.addStretch(1)
            root.addLayout(actions)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(self._accept_if_names_ok)
            buttons.rejected.connect(self.reject)
        else:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
            if close_btn is not None:
                close_btn.clicked.connect(self.reject)
        root.addWidget(buttons)

    def _accept_if_names_ok(self) -> None:
        name = self._name_edit.text().strip() or "未命名素材包"
        conflict = bundle_name_conflict(self._account_id, name, exclude_id=self._bundle_id)
        if conflict:
            QMessageBox.warning(self, "名称重复", conflict)
            return
        seen: set[tuple[str, str]] = set()
        for asset_type, item_id, new_name in self.pending_renames():
            key = (canon_asset_type(asset_type), new_name.strip())
            if key in seen:
                QMessageBox.warning(self, "名称重复", f"同一种素材不能都叫「{new_name.strip()}」。")
                return
            seen.add(key)
            conflict = asset_name_conflict(
                self._account_id,
                asset_type,
                new_name,
                exclude_item_id=item_id,
            )
            if conflict:
                QMessageBox.warning(self, "名称重复", conflict)
                return
        self.accept()

    def _add_material_item(self, asset_type: str, item_id: str, name: str) -> None:
        it = QListWidgetItem(f"{asset_type_to_zh(asset_type)} · {name}")
        it.setData(
            Qt.ItemDataRole.UserRole,
            {"asset_type": asset_type, "item_id": item_id, "name": name, "orig_name": name},
        )
        self._items.addItem(it)

    def _add_gap_item(self, gap: float = 3.0, *, editable: bool = True) -> None:
        """在详情对话框列表中插入一行间隔设置。"""
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, {"_row_type": "gap", "gap": gap})
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._items.addItem(it)
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(8, 2, 8, 2)
        label = QLabel("⏱ 间隔")
        label.setStyleSheet("color: #888; font-style: italic;")
        spin = QSpinBox()
        spin.setRange(3, 3600)
        spin.setValue(max(3, int(gap)))
        spin.setSuffix(" 秒")
        spin.setEnabled(editable)
        layout.addWidget(label)
        layout.addWidget(spin, 1)
        layout.addStretch(1)
        spin.valueChanged.connect(lambda v, item=it: self._on_gap_changed(item, v))
        self._items.setItemWidget(it, w)

    def _on_gap_changed(self, item: QListWidgetItem, value: int) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            data["gap"] = float(value)
            item.setData(Qt.ItemDataRole.UserRole, data)

    def _on_item_context_menu(self, pos) -> None:
        item = self._items.itemAt(pos)
        if item is None:
            return
        self._items.setCurrentItem(item)
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict) and str(data.get("_row_type", "")) == "gap":
            menu = QMenu(self)
            add_copy_action(menu, self._items)
            menu.exec(self._items.viewport().mapToGlobal(pos))
            return
        menu = QMenu(self)
        add_copy_action(menu, self._items)
        if self._readonly:
            act_detail = menu.addAction("查看详情")
            chosen = menu.exec(self._items.viewport().mapToGlobal(pos))
            if chosen == act_detail:
                self._open_material_preview(item)
            return
        act_rename = menu.addAction("重命名")
        chosen = menu.exec(self._items.viewport().mapToGlobal(pos))
        if chosen == act_rename:
            self._rename_selected_material()

    def _open_material_preview(self, item: QListWidgetItem | None = None) -> None:
        item = item or self._items.currentItem()
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
            return
        asset_type = str(data.get("asset_type", "")).strip().lower()
        item_id = str(data.get("item_id", "")).strip()
        row = self._asset_map.get((asset_type, item_id), {})
        display_name = str(data.get("name", "")).strip() or str(row.get("name", "")).strip()
        if not display_name or display_name == item_id:
            display_name = "未命名"
        # 先用素材库里这条素材的预览字段，再用对话框里改过的名字盖住。
        payload: dict[str, Any] = dict(row) if row else {}
        payload.setdefault("asset_type", asset_type or "unknown")
        payload.setdefault("item_id", item_id)
        from app.broadcast.asset_preview_source import asset_preview_payload, usable_asset_name

        library_name = usable_asset_name(display_name, item_id)
        enriched = asset_preview_payload(
            self._account_id,
            {
                "type": asset_type,
                "item_id": item_id,
                "template_id": item_id,
                "voice_id": item_id if asset_type == "voice" else "",
                "path": str(payload.get("path") or ""),
                "thumb_path": str(payload.get("thumb_path") or ""),
                "display_name": library_name,
            },
            name=library_name,
        )
        for key, value in enriched.items():
            if value in ("", None, [], {}):
                continue
            if payload.get(key) in (None, "", [], {}):
                payload[key] = value
        if library_name:
            payload["name"] = library_name
            display_name = library_name
        elif str(enriched.get("name") or "").strip():
            display_name = str(enriched.get("name") or "").strip()
            payload["name"] = display_name
        else:
            payload["name"] = display_name

        provider = self._preview_service_provider
        if provider is None:
            # 无预览后端：降级为纯文本提示（tests / offscreen 环境）。
            QMessageBox.information(
                self,
                "素材详情",
                (
                    f"类型：{asset_type_to_zh(asset_type)}\n"
                    f"名称：{display_name}\n"
                    "（当前环境未提供预览服务，仅显示元数据）"
                ),
            )
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"素材详情 · {display_name}")
        dlg.resize(520, 560)
        layout = QVBoxLayout(dlg)
        panel = AssetPreviewPanel(preview_service_provider=provider)
        panel.set_asset(payload)
        layout.addWidget(panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        # Close 按钮在 Qt 里默认是 RejectRole，用 rejected 兜底
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()

    def _rename_selected_material(self) -> None:
        current = self._items.currentItem()
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        new_name, ok = QInputDialog.getText(
            self, "重命名素材", "素材名字：", text=str(data.get("name", ""))
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            return
        data["name"] = new_name
        current.setData(Qt.ItemDataRole.UserRole, data)
        current.setText(f"{asset_type_to_zh(str(data.get('asset_type', '')))} · {new_name}")

    def _remove_selected_material(self) -> None:
        row = self._items.currentRow()
        if row >= 0:
            self._items.takeItem(row)

    def result_bundle(self) -> AssetBundle:
        items: list[BundleItem] = []
        step_gaps: list[float] = []
        for i in range(self._items.count()):
            data = self._items.item(i).data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            if str(data.get("_row_type", "")) == "gap":
                step_gaps.append(max(3.0, float(data.get("gap", 10.0))))
                continue
            items.append(
                BundleItem(asset_type=str(data.get("asset_type", "")), item_id=str(data.get("item_id", "")))
            )
        return AssetBundle(
            id=self._bundle_id,
            name=self._name_edit.text().strip() or "未命名素材包",
            items=items,
            tags=self._tags,
            step_gaps=step_gaps,
        )

    def pending_renames(self) -> list[tuple[str, str, str]]:
        """返回 [(asset_type, item_id, new_name)]，仅含名字被改动的素材。"""
        out: list[tuple[str, str, str]] = []
        for i in range(self._items.count()):
            data = self._items.item(i).data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            name = str(data.get("name", "")).strip()
            orig = str(data.get("orig_name", "")).strip()
            if name and name != orig:
                out.append((str(data.get("asset_type", "")), str(data.get("item_id", "")), name))
        return out


class LibraryBundleTab(QWidget):
    """制作素材包：从素材库挑选条目并组合保存。"""

    bundlesChanged = Signal()

    def __init__(
        self,
        *,
        preview_service_provider: Callable[[], PreviewService] | None = None,
    ) -> None:
        super().__init__()
        self._account_id = "default"
        self._asset_rows: list[dict[str, Any]] = []
        self._preview_service_provider = preview_service_provider
        self._right_press_rows: dict[int, list[int]] = {}
        self._editing_bundle_id = ""
        self._build_ui()
        self._reload_bundle_list()

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        left.addWidget(QLabel("可选素材"))
        filter_row = QHBoxLayout()
        self._type_filter = QComboBox()
        self._type_filter.addItems(ASSET_TYPE_FILTER_OPTIONS)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索素材名")
        filter_row.addWidget(self._type_filter)
        filter_row.addWidget(self._search, 1)
        left.addLayout(filter_row)
        self._asset_list = QListWidget()
        # 支持长拉 / Ctrl+Click / Shift+Click 多选，一次批量加入素材包
        self._asset_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Step 4: 双击预览，右键重命名（不提供删除）
        self._asset_list.itemDoubleClicked.connect(self._preview_asset_item)
        self._asset_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._asset_list.customContextMenuRequested.connect(self._on_asset_list_context_menu)
        self._asset_list.viewport().installEventFilter(self)
        left.addWidget(self._asset_list, 1)
        self._btn_add = QPushButton("添加到素材包 →")
        left.addWidget(self._btn_add)

        center = QVBoxLayout()
        self._editor_hint = QLabel("当前素材包内容（可拖拽排序）")
        center.addWidget(self._editor_hint)
        self._bundle_items = QListWidget()
        self._bundle_items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        # 中间"当前素材包"区也支持多选，方便一次移除多条
        self._bundle_items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Step 4: 双击预览，右键重命名+删除（只从当前包移除）
        self._bundle_items.itemDoubleClicked.connect(self._preview_bundle_item)
        self._bundle_items.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._bundle_items.customContextMenuRequested.connect(self._on_bundle_items_context_menu)
        self._bundle_items.viewport().installEventFilter(self)
        center.addWidget(self._bundle_items, 1)
        row_center = QHBoxLayout()
        self._btn_remove = QPushButton("移除选中")
        self._btn_clear = QPushButton("清空")
        row_center.addWidget(self._btn_remove)
        row_center.addWidget(self._btn_clear)
        row_center.addStretch(1)
        center.addLayout(row_center)
        form_row = QHBoxLayout()
        self._bundle_name = QLineEdit()
        self._bundle_name.setPlaceholderText("素材包名称")
        form_row.addWidget(self._bundle_name, 1)
        center.addLayout(form_row)
        self._btn_save = QPushButton("保存素材包")
        center.addWidget(self._btn_save)

        right = QVBoxLayout()
        right.addWidget(QLabel("已保存素材包"))
        self._saved_bundles = QListWidget()
        self._saved_bundles.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._saved_bundles.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._saved_bundles.customContextMenuRequested.connect(self._on_saved_bundles_context_menu)
        self._saved_bundles.viewport().installEventFilter(self)
        right.addWidget(self._saved_bundles, 1)
        self._btn_edit = QPushButton("编辑")
        right.addWidget(self._btn_edit)

        root.addLayout(left, 3)
        root.addLayout(center, 4)
        root.addLayout(right, 3)

        self._type_filter.currentTextChanged.connect(self._refresh_asset_list)
        self._search.textChanged.connect(self._refresh_asset_list)
        self._btn_add.clicked.connect(self._add_selected_asset)
        self._btn_remove.clicked.connect(self._remove_selected_item)
        self._btn_clear.clicked.connect(self._clear_editor_items)
        self._btn_save.clicked.connect(self._save_bundle)
        self._btn_edit.clicked.connect(self._edit_selected_bundle)
        self._saved_bundles.itemDoubleClicked.connect(self._open_bundle_detail)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.RightButton:
                for widget in (self._asset_list, self._bundle_items, self._saved_bundles):
                    if obj is widget.viewport():
                        self._right_press_rows[id(widget)] = [widget.row(it) for it in widget.selectedItems()]
                        break
        return super().eventFilter(obj, event)

    def _context_rows(self, widget: QListWidget, clicked: QListWidgetItem) -> list[int]:
        clicked_row = widget.row(clicked)
        snapshot = [row for row in self._right_press_rows.get(id(widget), []) if row >= 0]
        if clicked_row in snapshot:
            return snapshot
        return [clicked_row] if clicked_row >= 0 else []

    def set_account(self, account_id: str) -> None:
        self._account_id = account_id.strip() or "default"
        self._reload_bundle_list()

    def set_assets(self, rows: list[dict[str, Any]]) -> None:
        self._asset_rows = list(rows)
        self._refresh_asset_list()
        self._refresh_open_bundle_names()

    def list_bundle_assets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        asset_map = {
            (str(row.get("asset_type", "")).strip(), str(row.get("item_id", "")).strip()): row
            for row in self._asset_rows
        }
        for row in list_bundles(self._account_id):
            steps: list[dict[str, Any]] = []
            for item in row.items:
                step = self._bundle_item_to_step(item.asset_type, item.item_id, asset_map)
                if step is not None:
                    steps.append(step)
            out.append(
                {
                    "asset_type": "bundle",
                    "item_id": row.id,
                    "name": row.name,
                    "steps": steps,
                }
            )
        return out

    def _refresh_open_bundle_names(self) -> None:
        """已保存素材改名后，当前正在编辑的素材包内容跟着改。"""
        for index in range(self._bundle_items.count()):
            item = self._bundle_items.item(index)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
                continue
            asset_type = str(data.get("asset_type", "")).strip().lower()
            item_id = str(data.get("item_id", "")).strip()
            row = self._asset_map_for(asset_type, item_id)
            show_type = asset_type
            if not row and asset_type == "link":
                row = self._asset_map_for("wxa", item_id)
                if row:
                    show_type = "wxa"
                    data["asset_type"] = "wxa"
                    item.setData(Qt.ItemDataRole.UserRole, data)
            name = str(row.get("name", "")).strip() if row else ""
            if not name or name == item_id:
                continue
            item.setText(f"{asset_type_to_zh(show_type)} · {name}")

    def _refresh_asset_list(self) -> None:
        selected_type = asset_type_from_filter(self._type_filter.currentText())
        keyword = self._search.text().strip().lower()
        self._asset_list.clear()
        visible: list[dict[str, Any]] = []
        for row in self._asset_rows:
            asset_type = str(row.get("asset_type", "")).strip().lower()
            if asset_type == "bundle":
                continue
            if selected_type and asset_type != selected_type:
                continue
            name = str(row.get("name", "")).strip()
            item_id = str(row.get("item_id", "")).strip()
            if keyword and keyword not in name.lower():
                continue
            visible.append(row)
        for row in newest_first(visible):
            asset_type = str(row.get("asset_type", "")).strip().lower()
            name = str(row.get("name", "")).strip()
            item_id = str(row.get("item_id", "")).strip()
            shown = name if name and name != item_id else "未命名"
            item = QListWidgetItem(f"{asset_type_to_zh(asset_type)} · {shown}")
            item.setData(Qt.ItemDataRole.UserRole, {"asset_type": asset_type, "item_id": item_id})
            self._asset_list.addItem(item)

    def _add_selected_asset(self) -> None:
        # 支持多选：按用户视觉顺序批量加入
        items = self._asset_list.selectedItems() or (
            [self._asset_list.currentItem()] if self._asset_list.currentItem() else []
        )
        for src in items:
            data = src.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            # 如果当前列表不为空且末尾不是 gap 行，先插入间隔行
            if self._bundle_items.count() > 0:
                last_data = self._bundle_items.item(self._bundle_items.count() - 1).data(Qt.ItemDataRole.UserRole)
                if isinstance(last_data, dict) and str(last_data.get("_row_type", "asset")) != "gap":
                    self._insert_gap_row()
            item = QListWidgetItem(src.text())
            item.setData(Qt.ItemDataRole.UserRole, data)
            self._bundle_items.addItem(item)

    def _remove_selected_item(self) -> None:
        # 支持一次移除多条：按行号倒序 takeItem，避免索引位移
        rows = sorted({idx.row() for idx in self._bundle_items.selectedIndexes()}, reverse=True)
        if not rows:
            cur = self._bundle_items.currentRow()
            if cur >= 0:
                rows = [cur]
        for r in rows:
            self._bundle_items.takeItem(r)
        # 移除后修正间隔行：清除相邻的重复 gap 行及首尾 gap 行
        self._normalize_gap_rows()

    def _insert_gap_row(self, gap: float = 3.0) -> None:
        """在当前素材包列表末尾插入一行间隔设置。"""
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, {"_row_type": "gap", "gap": gap})
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._bundle_items.addItem(item)
        widget = self._make_gap_widget(item, gap)
        self._bundle_items.setItemWidget(item, widget)

    def _make_gap_widget(self, item: QListWidgetItem, gap: float = 3.0) -> QWidget:
        """创建间隔行内嵌 widget：包含标签 + QSpinBox。"""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(8, 2, 8, 2)
        label = QLabel("⏱ 间隔")
        label.setStyleSheet("color: #888; font-style: italic;")
        spin = QSpinBox()
        spin.setRange(3, 3600)
        spin.setValue(max(3, int(gap)))
        spin.setSuffix(" 秒")
        layout.addWidget(label)
        layout.addWidget(spin, 1)
        layout.addStretch(1)
        # 值变化时更新 item data
        spin.valueChanged.connect(lambda v, it=item: self._on_gap_value_changed(it, v))
        return w

    def _on_gap_value_changed(self, item: QListWidgetItem, value: int) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            data["gap"] = float(value)
            item.setData(Qt.ItemDataRole.UserRole, data)

    def _normalize_gap_rows(self) -> None:
        """移除首尾 gap 行及连续重复 gap 行。"""
        changed = True
        while changed:
            changed = False
            count = self._bundle_items.count()
            if count == 0:
                break
            # 移除首部 gap
            first = self._bundle_items.item(0)
            if first and isinstance(first.data(Qt.ItemDataRole.UserRole), dict):
                if str(first.data(Qt.ItemDataRole.UserRole).get("_row_type", "")) == "gap":
                    self._bundle_items.takeItem(0)
                    changed = True
                    continue
            # 移除尾部 gap
            count = self._bundle_items.count()
            if count > 0:
                last = self._bundle_items.item(count - 1)
                if last and isinstance(last.data(Qt.ItemDataRole.UserRole), dict):
                    if str(last.data(Qt.ItemDataRole.UserRole).get("_row_type", "")) == "gap":
                        self._bundle_items.takeItem(count - 1)
                        changed = True
                        continue
            # 移除连续 gap
            for i in range(self._bundle_items.count() - 1):
                d1 = self._bundle_items.item(i).data(Qt.ItemDataRole.UserRole)
                d2 = self._bundle_items.item(i + 1).data(Qt.ItemDataRole.UserRole)
                if (isinstance(d1, dict) and str(d1.get("_row_type", "")) == "gap"
                        and isinstance(d2, dict) and str(d2.get("_row_type", "")) == "gap"):
                    self._bundle_items.takeItem(i + 1)
                    changed = True
                    break

    def _save_bundle(self) -> None:
        name = self._bundle_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少名称", "请填写素材包名称。")
            return
        # 只有点过「编辑」才覆盖那一个包。空白编辑保存永远新建。
        bundle_id = self._editing_bundle_id.strip()
        conflict = bundle_name_conflict(self._account_id, name, exclude_id=bundle_id)
        if conflict:
            QMessageBox.warning(self, "名称重复", conflict)
            return
        items: list[BundleItem] = []
        step_gaps: list[float] = []
        for i in range(self._bundle_items.count()):
            row = self._bundle_items.item(i).data(Qt.ItemDataRole.UserRole)
            if not isinstance(row, dict):
                continue
            row_type = str(row.get("_row_type", "asset"))
            if row_type == "gap":
                step_gaps.append(max(3.0, float(row.get("gap", 10.0))))
                continue
            items.append(BundleItem(asset_type=str(row.get("asset_type", "")), item_id=str(row.get("item_id", ""))))
        if not items:
            QMessageBox.warning(self, "素材包为空", "请先添加至少一条素材。")
            return
        tags: list[str] = []
        contact_gap = 60.0
        if bundle_id:
            existing = get_bundle(bundle_id, account_id=self._account_id)
            if existing is not None:
                tags = list(existing.tags)
                contact_gap = existing.contact_gap
            bundle = AssetBundle(
                id=bundle_id,
                name=name,
                items=items,
                tags=tags,
                step_gaps=step_gaps,
                contact_gap=contact_gap,
            )
        else:
            bundle = AssetBundle(name=name, items=items, tags=tags, step_gaps=step_gaps)
        save_bundle(bundle, account_id=self._account_id)
        QMessageBox.information(self, "保存成功", f"素材包已保存：{name}")
        self._clear_editor()
        self._reload_bundle_list()
        self.bundlesChanged.emit()

    def _clear_editor(self) -> None:
        self._editing_bundle_id = ""
        self._bundle_name.clear()
        self._bundle_items.clear()
        self._editor_hint.setText("当前素材包内容（可拖拽排序）")

    def _clear_editor_items(self) -> None:
        """清空中间内容，并退出编辑态，避免下一次保存盖掉正在编辑的包。"""
        self._bundle_items.clear()
        self._editing_bundle_id = ""
        self._editor_hint.setText("当前素材包内容（可拖拽排序）")

    def _reload_bundle_list(self) -> None:
        self._saved_bundles.clear()
        bundles = sorted(list_bundles(self._account_id), key=lambda row: row.created_at or "", reverse=True)
        for row in bundles:
            text = f"{row.name}（{len(row.items)} 条）"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row.model_dump(mode="json"))
            self._saved_bundles.addItem(item)

    def _edit_selected_bundle(self) -> None:
        current = self._saved_bundles.currentItem()
        if current is None:
            QMessageBox.information(self, "未选择素材包", "请先选中一个素材包。")
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        bundle = AssetBundle.model_validate(data)
        self._editing_bundle_id = bundle.id
        self._bundle_name.setText(bundle.name)
        self._editor_hint.setText(f"正在编辑：{bundle.name}（保存将覆盖此包）")
        self._bundle_items.clear()
        asset_map = {
            (str(row.get("asset_type", "")).strip().lower(), str(row.get("item_id", "")).strip()): row
            for row in self._asset_rows
        }
        for idx, row in enumerate(bundle.items):
            meta = asset_map.get((row.asset_type.strip().lower(), row.item_id.strip()), {})
            name = str(meta.get("name", "")).strip()
            if not name or name == row.item_id:
                from app.broadcast.asset_preview_source import lookup_asset_name

                name = lookup_asset_name(
                    self._account_id,
                    row.asset_type,
                    item_id=row.item_id,
                ) or "未命名"
            item = QListWidgetItem(f"{asset_type_to_zh(row.asset_type)} · {name}")
            item.setData(Qt.ItemDataRole.UserRole, row.model_dump(mode="json"))
            self._bundle_items.addItem(item)
            if idx < len(bundle.items) - 1:
                gap = bundle.step_gaps[idx] if idx < len(bundle.step_gaps) else 3.0
                self._insert_gap_row(gap)

    def _open_bundle_detail(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        try:
            bundle = AssetBundle.model_validate(data)
        except Exception:
            return
        asset_map = {
            (str(row.get("asset_type", "")).strip().lower(), str(row.get("item_id", "")).strip()): row
            for row in self._asset_rows
        }
        dlg = BundleDetailDialog(
            bundle,
            asset_map,
            self,
            preview_service_provider=self._preview_service_provider,
            account_id=self._account_id,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        errors: list[str] = []
        for asset_type, item_id, new_name in dlg.pending_renames():
            try:
                rename_underlying_asset(asset_type, item_id, new_name, self._account_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{asset_type_to_zh(asset_type)} 改名失败")
        try:
            save_bundle(dlg.result_bundle(), account_id=self._account_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存素材包失败 account_id={} err={}", self._account_id, exc)
            QMessageBox.warning(self, "保存失败", "素材包保存失败，请稍后重试。")
            return
        if errors:
            logger.warning("素材包内改名部分失败 errors={}", errors)
            QMessageBox.warning(self, "部分素材改名失败", "部分素材改名失败，请稍后在素材库重试。")
        self._reload_bundle_list()
        self.bundlesChanged.emit()

    def _delete_saved_rows(self, rows: list[int]) -> None:
        ids: list[str] = []
        for row in rows:
            item = self._saved_bundles.item(row)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            bundle_id = str(data.get("id", "")).strip()
            if bundle_id:
                ids.append(bundle_id)
        if not ids:
            return
        removed_editing = self._editing_bundle_id in ids
        missing = False
        for bundle_id in ids:
            if not delete_bundle(bundle_id, account_id=self._account_id):
                missing = True
        if removed_editing:
            self._clear_editor()
        self._reload_bundle_list()
        self.bundlesChanged.emit()
        if missing:
            QMessageBox.warning(self, "删除失败", "有的素材包已经不在列表里。")

    def _rename_saved_bundle(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        try:
            bundle = AssetBundle.model_validate(data)
        except Exception:
            return
        new_name, ok = QInputDialog.getText(self, "重命名素材包", "素材包名称：", text=bundle.name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "缺少名称", "请填写素材包名称。")
            return
        conflict = bundle_name_conflict(self._account_id, new_name, exclude_id=bundle.id)
        if conflict:
            QMessageBox.warning(self, "名称重复", conflict)
            return
        save_bundle(bundle.model_copy(update={"name": new_name}), account_id=self._account_id)
        if self._editing_bundle_id == bundle.id:
            self._bundle_name.setText(new_name)
            self._editor_hint.setText(f"正在编辑：{new_name}（保存将覆盖此包）")
        self._reload_bundle_list()
        self.bundlesChanged.emit()

    def _on_saved_bundles_context_menu(self, pos) -> None:
        item = self._saved_bundles.itemAt(pos)
        if item is None:
            return
        rows = self._context_rows(self._saved_bundles, item)
        menu = QMenu(self)
        add_copy_action(menu, self._saved_bundles)
        act_detail = menu.addAction("展开详情")
        act_rename = menu.addAction("重命名")
        delete_label = f"删除素材包（{len(rows)} 个）" if len(rows) > 1 else "删除素材包"
        act_delete = menu.addAction(delete_label)
        chosen = menu.exec(self._saved_bundles.viewport().mapToGlobal(pos))
        if chosen == act_detail:
            self._open_bundle_detail(item)
        elif chosen == act_rename:
            self._rename_saved_bundle(item)
        elif chosen == act_delete:
            self._delete_saved_rows(rows)

    def remove_items(self, deleted: list) -> list[str]:
        """从所有已保存素材包中移除这些素材。包被拿空则整包删除，返回被删掉的包 id。"""
        if not deleted:
            return []
        _removed_items, removed_bundles = remove_referenced_items(deleted, account_id=self._account_id)
        keys = {
            (canon_asset_type(str(row[0])), str(row[1]).strip())
            for row in deleted
            if len(row) > 1 and str(row[1]).strip()
        }
        self._strip_editor_keys(keys)
        if _removed_items or removed_bundles:
            self._reload_bundle_list()
            self.bundlesChanged.emit()
        return removed_bundles

    def prune_missing(self, existing: set[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str]]:
        """打开或刷新时清掉素材包里已经不存在的素材。空包一并删除。"""
        removed_items, removed_bundles = prune_missing_items(existing, account_id=self._account_id)
        if removed_items or removed_bundles:
            self._strip_editor_keys({(canon_asset_type(asset_type), item_id) for asset_type, item_id in removed_items})
            self._reload_bundle_list()
        return removed_items, removed_bundles

    def _strip_editor_keys(self, keys: set[tuple[str, str]]) -> int:
        if not keys:
            return 0
        rows: list[int] = []
        for index in range(self._bundle_items.count()):
            data = self._bundle_items.item(index).data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
                continue
            key = (canon_asset_type(str(data.get("asset_type", ""))), str(data.get("item_id", "")).strip())
            if key in keys:
                rows.append(index)
        for row in reversed(rows):
            self._bundle_items.takeItem(row)
        if rows:
            self._normalize_gap_rows()
        return len(rows)

    def _preview_asset_item(self, item: QListWidgetItem) -> None:
        """Step 4: 左侧可选素材双击→预览。"""
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        asset_type = str(data.get("asset_type", "")).strip().lower()
        item_id = str(data.get("item_id", "")).strip()
        row = self._asset_map_for(asset_type, item_id)
        if not row:
            return
        self._open_preview_dialog(row, asset_type, item_id)

    def _preview_bundle_item(self, item: QListWidgetItem) -> None:
        """Step 4: 中间当前素材包双击→预览。"""
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        asset_type = str(data.get("asset_type", "")).strip().lower()
        item_id = str(data.get("item_id", "")).strip()
        row = self._asset_map_for(asset_type, item_id)
        if not row:
            return
        self._open_preview_dialog(row, asset_type, item_id)

    def _asset_map_for(self, asset_type: str, item_id: str) -> dict:
        for r in self._asset_rows:
            if (str(r.get("asset_type", "")).strip().lower() == asset_type
                    and str(r.get("item_id", "")).strip() == item_id):
                return dict(r)
        return {}

    def _open_preview_dialog(self, row: dict, asset_type: str, item_id: str) -> None:
        provider = self._preview_service_provider
        if provider is None:
            return
        from gui.widgets.asset_preview import AssetPreviewPanel

        payload = dict(row)
        payload.setdefault("asset_type", asset_type)
        payload.setdefault("item_id", item_id)
        name = str(payload.get("name", "")).strip()
        if not name or name == item_id:
            name = "未命名"

        dlg = QDialog(self)
        dlg.setWindowTitle(f"素材详情 · {name}")
        dlg.resize(520, 560)
        layout = QVBoxLayout(dlg)
        panel = AssetPreviewPanel(preview_service_provider=provider)
        panel.set_asset(payload)
        layout.addWidget(panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _prompt_rename_asset(self, asset_type: str, item_id: str) -> None:
        new_name, ok = QInputDialog.getText(
            self,
            "重命名素材",
            "素材名字：",
            text=str(self._asset_map_for(asset_type, item_id).get("name", "")),
        )
        if not ok or not new_name.strip():
            return
        try:
            rename_underlying_asset(asset_type, item_id, new_name.strip(), self._account_id)
        except ValueError as exc:
            QMessageBox.warning(self, "名称重复", str(exc))
            return
        self.bundlesChanged.emit()

    def _on_asset_list_context_menu(self, pos) -> None:
        """左侧可选素材右键：重命名，或把选中素材从当前正在编辑的素材包里移除。"""
        item = self._asset_list.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        rows = self._context_rows(self._asset_list, item)
        menu = QMenu(self)
        add_copy_action(menu, self._asset_list)
        act_rename = menu.addAction("重命名")
        remove_label = f"从当前素材包移除（{len(rows)} 条）" if len(rows) > 1 else "从当前素材包移除"
        act_remove = menu.addAction(remove_label)
        act_preview = menu.addAction("预览")
        chosen = menu.exec(self._asset_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        asset_type = str(data.get("asset_type", "")).strip()
        item_id = str(data.get("item_id", "")).strip()
        if chosen == act_rename:
            self._prompt_rename_asset(asset_type, item_id)
        elif chosen == act_remove:
            keys: set[tuple[str, str]] = set()
            for row_index in rows:
                src = self._asset_list.item(row_index)
                if src is None:
                    continue
                payload = src.data(Qt.ItemDataRole.UserRole)
                if not isinstance(payload, dict):
                    continue
                key_type = str(payload.get("asset_type", "")).strip()
                key_id = str(payload.get("item_id", "")).strip()
                if key_type and key_id:
                    keys.add((canon_asset_type(key_type), key_id))
            removed = self._strip_editor_keys(keys)
            if removed == 0:
                QMessageBox.information(self, "未移除", "当前素材包里没有这些素材。")
        elif chosen == act_preview:
            row = self._asset_map_for(asset_type, item_id)
            if row:
                self._open_preview_dialog(row, asset_type, item_id)

    def _on_bundle_items_context_menu(self, pos) -> None:
        """中间当前素材包右键：重命名，或把多选的行从正在编辑的包里移除。"""
        item = self._bundle_items.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        rows = self._context_rows(self._bundle_items, item)
        menu = QMenu(self)
        add_copy_action(menu, self._bundle_items)
        act_rename = menu.addAction("重命名")
        delete_label = f"从素材包移除（{len(rows)} 条）" if len(rows) > 1 else "从素材包移除"
        act_delete = menu.addAction(delete_label)
        act_preview = menu.addAction("预览")
        chosen = menu.exec(self._bundle_items.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_rename:
            if str(data.get("_row_type", "")) == "gap":
                return
            self._prompt_rename_asset(str(data.get("asset_type", "")).strip(), str(data.get("item_id", "")).strip())
        elif chosen == act_delete:
            for row_index in sorted(rows, reverse=True):
                self._bundle_items.takeItem(row_index)
            self._normalize_gap_rows()
        elif chosen == act_preview:
            if str(data.get("_row_type", "")) == "gap":
                return
            asset_type = str(data.get("asset_type", "")).strip()
            item_id = str(data.get("item_id", "")).strip()
            row = self._asset_map_for(asset_type, item_id)
            if row:
                self._open_preview_dialog(row, asset_type, item_id)

    @staticmethod
    def _bundle_item_to_step(
        asset_type: str,
        item_id: str,
        asset_map: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any] | None:
        kind = asset_type.strip().lower()
        key = (kind, item_id.strip())
        row = asset_map.get(key, {})
        shown = str(row.get("name", "")).strip()
        if kind == "voice":
            return {"type": "voice", "voice_id": item_id, "display_name": shown}
        if kind == "text":
            return {
                "type": "text",
                "template_id": item_id,
                "variant_pick": "random",
                "text_kind": "text",
                "display_name": str(row.get("name", "")).strip(),
            }
        if kind == "link":
            wxa_row = asset_map.get(("wxa", item_id.strip()), {})
            if wxa_row and key not in asset_map:
                shown = str(wxa_row.get("name", "")).strip() or shown
                return {
                    "type": "wxa",
                    "template_id": item_id,
                    "thumb_path": str(wxa_row.get("thumb_path", "")).strip(),
                    "display_name": shown,
                }
            return {
                "type": "text",
                "template_id": item_id,
                "variant_pick": "random",
                "text_kind": "text",
                "display_name": str(row.get("name", "")).strip(),
            }
        if kind == "wxa":
            return {
                "type": "wxa",
                "template_id": item_id,
                "thumb_path": str(row.get("thumb_path", "")).strip(),
                "display_name": shown,
            }
        if kind in {"oa", "doc", "channel"}:
            if not row:
                for alt in ("doc", "channel", "oa"):
                    alt_row = asset_map.get((alt, item_id.strip()), {})
                    if alt_row:
                        row = alt_row
                        kind = alt
                        shown = str(row.get("name", "")).strip() or shown
                        break
            return {"type": kind, "template_id": item_id, "display_name": shown}
        if kind == "image":
            path = str(row.get("path", "")).strip()
            if not path:
                return None
            return {"type": "image", "path": path, "display_name": shown}
        if kind == "video":
            path = str(row.get("path", "")).strip()
            if not path:
                return None
            return {
                "type": "video",
                "path": path,
                "thumb_path": str(row.get("thumb_path", "")).strip(),
                "display_name": shown,
            }
        return None
