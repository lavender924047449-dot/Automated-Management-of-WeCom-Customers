from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger
from PySide6.QtCore import Qt, Signal
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
    QVBoxLayout,
    QWidget,
)

from app.broadcast import image_templates as image_tpl_mod
from app.broadcast import oa_templates as oa_tpl_mod
from app.broadcast import video_templates as video_tpl_mod
from app.broadcast import wxa_templates as wxa_tpl_mod
from app.broadcast.asset_bundles import AssetBundle, BundleItem, delete_bundle, list_bundles, save_bundle
from app.broadcast.library import VoiceLibrary
from app.broadcast.preview import PreviewService
from gui.i18n import ASSET_TYPE_FILTER_OPTIONS, asset_type_from_filter, asset_type_to_zh
from gui.widgets.asset_preview import AssetPreviewPanel


def rename_underlying_asset(asset_type: str, item_id: str, new_name: str, account_id: str) -> None:
    """把素材包内某条素材的名字写回底层素材库（全局生效）。"""
    name = new_name.strip()
    if not name or not item_id:
        return
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
    ) -> None:
        super().__init__(parent)
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
        form.addWidget(self._name_edit, 1)
        root.addLayout(form)

        root.addWidget(
            QLabel("包含素材（可拖拽排序；双击查看详情；右键或下方按钮改名）")
        )
        self._items = QListWidget()
        self._items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._items.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._items.itemDoubleClicked.connect(self._open_material_preview)
        self._items.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._items.customContextMenuRequested.connect(self._on_item_context_menu)
        root.addWidget(self._items, 1)
        for item in bundle.items:
            row = asset_map.get((item.asset_type.strip().lower(), item.item_id.strip()), {})
            name = str(row.get("name", "")).strip() or item.item_id
            self._add_material_item(item.asset_type, item.item_id, name)

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
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_material_item(self, asset_type: str, item_id: str, name: str) -> None:
        it = QListWidgetItem(f"{asset_type_to_zh(asset_type)} · {name}")
        it.setData(
            Qt.ItemDataRole.UserRole,
            {"asset_type": asset_type, "item_id": item_id, "name": name, "orig_name": name},
        )
        self._items.addItem(it)

    def _on_item_context_menu(self, pos) -> None:
        item = self._items.itemAt(pos)
        if item is None:
            return
        self._items.setCurrentItem(item)
        menu = QMenu(self)
        act_rename = menu.addAction("重命名")
        chosen = menu.exec(self._items.viewport().mapToGlobal(pos))
        if chosen == act_rename:
            self._rename_selected_material()

    def _open_material_preview(self, item: QListWidgetItem | None = None) -> None:
        item = item or self._items.currentItem()
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        asset_type = str(data.get("asset_type", "")).strip().lower()
        item_id = str(data.get("item_id", "")).strip()
        row = self._asset_map.get((asset_type, item_id), {})
        display_name = str(data.get("name", "")).strip() or str(row.get("name", "")).strip() or item_id
        # 组装 AssetPreviewPanel.set_asset 需要的 mapping：先带上素材库里保存的
        # 所有字段（含 path/thumb_path/variants/oa_* 等），再用 dialog 里最新的名字覆盖。
        payload: dict[str, Any] = dict(row) if row else {}
        payload.setdefault("asset_type", asset_type or "unknown")
        payload.setdefault("item_id", item_id)
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
                    f"ID：{item_id}\n"
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
        for i in range(self._items.count()):
            data = self._items.item(i).data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            items.append(
                BundleItem(asset_type=str(data.get("asset_type", "")), item_id=str(data.get("item_id", "")))
            )
        return AssetBundle(
            id=self._bundle_id,
            name=self._name_edit.text().strip() or "未命名素材包",
            items=items,
            tags=self._tags,
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
        left.addWidget(self._asset_list, 1)
        self._btn_add = QPushButton("添加到素材包 →")
        left.addWidget(self._btn_add)

        center = QVBoxLayout()
        center.addWidget(QLabel("当前素材包内容（可拖拽排序）"))
        self._bundle_items = QListWidget()
        self._bundle_items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        # 中间"当前素材包"区也支持多选，方便一次移除多条
        self._bundle_items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        self._bundle_tags = QLineEdit()
        self._bundle_tags.setPlaceholderText("标签（可选，顿号或逗号分隔）")
        form_row.addWidget(self._bundle_name, 1)
        form_row.addWidget(self._bundle_tags, 1)
        center.addLayout(form_row)
        self._btn_save = QPushButton("保存素材包")
        center.addWidget(self._btn_save)

        right = QVBoxLayout()
        right.addWidget(QLabel("已保存素材包"))
        self._saved_bundles = QListWidget()
        right.addWidget(self._saved_bundles, 1)
        row_right = QHBoxLayout()
        self._btn_load = QPushButton("加载到中间")
        self._btn_delete = QPushButton("删除素材包")
        row_right.addWidget(self._btn_load)
        row_right.addWidget(self._btn_delete)
        right.addLayout(row_right)

        root.addLayout(left, 3)
        root.addLayout(center, 4)
        root.addLayout(right, 3)

        self._type_filter.currentTextChanged.connect(self._refresh_asset_list)
        self._search.textChanged.connect(self._refresh_asset_list)
        self._btn_add.clicked.connect(self._add_selected_asset)
        self._btn_remove.clicked.connect(self._remove_selected_item)
        self._btn_clear.clicked.connect(self._bundle_items.clear)
        self._btn_save.clicked.connect(self._save_bundle)
        self._btn_load.clicked.connect(self._load_selected_bundle)
        self._btn_delete.clicked.connect(self._delete_selected_bundle)
        self._saved_bundles.itemDoubleClicked.connect(self._open_bundle_detail)

    def set_account(self, account_id: str) -> None:
        self._account_id = account_id.strip() or "default"
        self._reload_bundle_list()

    def set_assets(self, rows: list[dict[str, Any]]) -> None:
        self._asset_rows = list(rows)
        self._refresh_asset_list()

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

    def _refresh_asset_list(self) -> None:
        selected_type = asset_type_from_filter(self._type_filter.currentText())
        keyword = self._search.text().strip().lower()
        self._asset_list.clear()
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
            item = QListWidgetItem(f"{asset_type_to_zh(asset_type)} · {name} ({item_id})")
            item.setData(Qt.ItemDataRole.UserRole, {"asset_type": asset_type, "item_id": item_id})
            self._asset_list.addItem(item)

    def _add_selected_asset(self) -> None:
        # 支持多选：按用户视觉顺序（selectedIndexes 已按选中顺序）批量加入
        items = self._asset_list.selectedItems() or (
            [self._asset_list.currentItem()] if self._asset_list.currentItem() else []
        )
        for src in items:
            data = src.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
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

    def _save_bundle(self) -> None:
        name = self._bundle_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少名称", "请填写素材包名称。")
            return
        items: list[BundleItem] = []
        for i in range(self._bundle_items.count()):
            row = self._bundle_items.item(i).data(Qt.ItemDataRole.UserRole)
            if not isinstance(row, dict):
                continue
            items.append(BundleItem(asset_type=str(row.get("asset_type", "")), item_id=str(row.get("item_id", ""))))
        if not items:
            QMessageBox.warning(self, "素材包为空", "请先添加至少一条素材。")
            return
        tags = [x.strip() for x in self._bundle_tags.text().replace("、", ",").split(",") if x.strip()]
        existing = self._saved_bundles.currentItem()
        bundle_id = ""
        if existing is not None:
            d = existing.data(Qt.ItemDataRole.UserRole)
            if isinstance(d, dict):
                bundle_id = str(d.get("id", "")).strip()
        if bundle_id:
            bundle = AssetBundle(id=bundle_id, name=name, items=items, tags=tags)
        else:
            bundle = AssetBundle(name=name, items=items, tags=tags)
        save_bundle(bundle, account_id=self._account_id)
        QMessageBox.information(self, "保存成功", f"素材包已保存：{name}")
        self._reload_bundle_list()
        self.bundlesChanged.emit()

    def _reload_bundle_list(self) -> None:
        self._saved_bundles.clear()
        for row in list_bundles(self._account_id):
            text = f"{row.name}（{len(row.items)} 条）"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row.model_dump(mode="json"))
            self._saved_bundles.addItem(item)

    def _load_selected_bundle(self) -> None:
        current = self._saved_bundles.currentItem()
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        bundle = AssetBundle.model_validate(data)
        self._bundle_name.setText(bundle.name)
        self._bundle_tags.setText("、".join(bundle.tags))
        self._bundle_items.clear()
        for row in bundle.items:
            text = f"{asset_type_to_zh(row.asset_type)} · {row.item_id}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row.model_dump(mode="json"))
            self._bundle_items.addItem(item)

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
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        errors: list[str] = []
        for asset_type, item_id, new_name in dlg.pending_renames():
            try:
                rename_underlying_asset(asset_type, item_id, new_name, self._account_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{asset_type_to_zh(asset_type)} · {item_id[:8]}: {exc}")
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

    def _delete_selected_bundle(self) -> None:
        current = self._saved_bundles.currentItem()
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        bundle_id = str(data.get("id", "")).strip()
        if not bundle_id:
            return
        if not delete_bundle(bundle_id, account_id=self._account_id):
            QMessageBox.warning(self, "删除失败", "未找到该素材包。")
            return
        self._reload_bundle_list()
        self.bundlesChanged.emit()

    @staticmethod
    def _bundle_item_to_step(
        asset_type: str,
        item_id: str,
        asset_map: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any] | None:
        kind = asset_type.strip().lower()
        key = (kind, item_id.strip())
        row = asset_map.get(key, {})
        if kind == "voice":
            return {"type": "voice", "voice_id": item_id}
        if kind == "text":
            return {"type": "text", "template_id": item_id, "variant_pick": "random"}
        if kind == "link":
            # link = TextTemplate(kind="link")，发送侧当 text 发；plan schema 未接 wxa 实发。
            return {"type": "text", "template_id": item_id, "variant_pick": "random"}
        if kind == "oa":
            return {"type": "oa", "template_id": item_id}
        if kind == "image":
            path = str(row.get("path", "")).strip()
            return {"type": "image", "path": path} if path else None
        if kind == "video":
            path = str(row.get("path", "")).strip()
            if not path:
                return None
            return {
                "type": "video",
                "path": path,
                "thumb_path": str(row.get("thumb_path", "")).strip(),
            }
        return None
