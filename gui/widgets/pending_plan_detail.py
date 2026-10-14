"""待执行计划详情：和素材包详情一样的列表，可改顺序和间隔，双击预览单条素材。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QDate, QDateTime, QTime, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.broadcast.asset_bundles import get_bundle, save_bundle
from app.broadcast.asset_names import asset_name_conflict, bundle_name_conflict
from app.broadcast.library import VoiceLibrary
from app.broadcast.plan import BroadcastPlan
from app.broadcast.preview import PreviewService
from gui.copy_support import add_copy_action
from gui.i18n import asset_type_to_zh
from gui.widgets.asset_preview import AssetPreviewPanel


class PendingPlanDetailDialog(QDialog):
    """计划里的发送序列。保存后写回这条待执行计划。"""

    def __init__(
        self,
        entry,
        parent: QWidget | None = None,
        *,
        preview_service_provider: Callable[[], PreviewService] | None = None,
        account_id: str = "default",
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self._account_id = account_id.strip() or "default"
        self._preview_service_provider = preview_service_provider
        self._library = VoiceLibrary.load(account_id=self._account_id)
        self.trigger_changed = False
        self.setWindowTitle("计划详情")
        self.resize(480, 560)

        plan = entry.plan if isinstance(entry.plan, dict) else {}
        contacts = plan.get("contacts", [])
        names = [
            str(row.get("display_name", "")).strip()
            for row in contacts
            if isinstance(row, dict) and str(row.get("display_name", "")).strip()
        ]
        root = QVBoxLayout(self)
        who = "、".join(names) if names else "（无）"
        root.addWidget(QLabel(f"发给 {who}"))
        if entry.mode == "cron" and str(entry.label or "").strip():
            root.addWidget(QLabel(str(entry.label).strip()))

        self._time_row = QWidget()
        time_layout = QHBoxLayout(self._time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        self._time_edit = QDateTimeEdit()
        self._time_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._time_edit.setCalendarPopup(False)
        self._time_edit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self._time_edit.setMinimumWidth(200)
        self._time_edit.setMinimumDateTime(QDateTime(QDate.currentDate(), QTime(0, 0, 0)))
        try:
            trigger = datetime.fromisoformat(str(entry.trigger_at))
        except ValueError:
            trigger = datetime.now()
        self._time_edit.setDateTime(QDateTime(trigger))
        time_layout.addWidget(QLabel("触发时间"))
        time_layout.addWidget(self._time_edit, 1)
        root.addWidget(self._time_row)
        self._time_row.setVisible(entry.mode != "cron")

        root.addWidget(QLabel("发送内容（可拖拽排序；双击素材看详情，双击原子素材包展开；右键改名）"))
        self._items = QListWidget()
        self._items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._items.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._items.itemDoubleClicked.connect(self._open_row)
        self._items.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._items.customContextMenuRequested.connect(self._on_menu)
        self._items.model().rowsMoved.connect(self._on_rows_moved)
        root.addWidget(self._items, 1)

        sequence = plan.get("sequence", [])
        gaps = plan.get("step_gaps", [])
        if isinstance(sequence, list):
            for index, step in enumerate(sequence):
                if not isinstance(step, dict):
                    continue
                if str(step.get("type", "")).strip().lower() == "bundle":
                    self._add_bundle_rows(step)
                else:
                    self._add_step_row(step)
                if index < len(sequence) - 1:
                    gap = gaps[index] if isinstance(gaps, list) and index < len(gaps) else 3
                    self._add_gap_row(gap)

        actions = QHBoxLayout()
        rename_btn = QPushButton("重命名素材")
        remove_btn = QPushButton("移除选中")
        rename_btn.clicked.connect(self._rename_selected)
        remove_btn.clicked.connect(self._remove_selected)
        actions.addWidget(rename_btn)
        actions.addWidget(remove_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_bundle_rows(self, step: dict[str, Any]) -> None:
        group_id = uuid.uuid4().hex
        presentation = str(step.get("presentation", "atomic")).strip() or "atomic"
        if presentation not in {"atomic", "expanded"}:
            presentation = "atomic"
        header_step = {key: value for key, value in step.items() if key not in {"items", "item_gaps"}}
        self._add_step_row(header_step, row_type="bundle_header", group_id=group_id, presentation=presentation)
        if presentation != "expanded":
            return
        items = step.get("items", [])
        inner_gaps = step.get("item_gaps", [])
        if not isinstance(items, list):
            return
        for index, child in enumerate(items):
            if not isinstance(child, dict):
                continue
            self._add_step_row(child, row_type="bundle_child", group_id=group_id)
            if index < len(items) - 1:
                gap = inner_gaps[index] if isinstance(inner_gaps, list) and index < len(inner_gaps) else 3
                self._add_gap_row(gap, group_id=group_id)

    def _add_step_row(
        self,
        step: dict[str, Any],
        *,
        row_type: str = "step",
        group_id: str = "",
        presentation: str = "",
        library_bundle_id: str = "",
        insert_at: int = -1,
    ) -> QListWidgetItem:
        asset_type, item_id, name = self._identify(step)
        prefix = "    " if row_type == "bundle_child" else ""
        label = f"{prefix}{asset_type_to_zh(asset_type)} · {name}"
        if row_type == "bundle_header":
            suffix = "（展开）" if presentation == "expanded" else "（原子）"
            label = f"素材包 · {name}{suffix}"
        item = QListWidgetItem(label)
        payload: dict[str, Any] = {
            "_row_type": row_type,
            "step": dict(step),
            "asset_type": asset_type,
            "item_id": item_id,
            "name": name,
            "orig_name": name,
        }
        if group_id:
            payload["group_id"] = group_id
        if presentation:
            payload["presentation"] = presentation
        if library_bundle_id:
            payload["library_bundle_id"] = library_bundle_id
        item.setData(Qt.ItemDataRole.UserRole, payload)
        if insert_at >= 0:
            self._items.insertItem(insert_at, item)
        else:
            self._items.addItem(item)
        return item

    def _add_gap_row(
        self,
        gap: float,
        *,
        group_id: str = "",
        library_bundle_id: str = "",
        insert_at: int = -1,
    ) -> None:
        payload: dict[str, Any] = {"_row_type": "gap", "gap": float(gap)}
        if group_id:
            payload["group_id"] = group_id
        if library_bundle_id:
            payload["library_bundle_id"] = library_bundle_id
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, payload)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if insert_at >= 0:
            self._items.insertItem(insert_at, item)
        else:
            self._items.addItem(item)
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(28 if group_id else 8, 2, 8, 2)
        label = QLabel("⏱ 间隔")
        label.setStyleSheet("color: #888; font-style: italic;")
        spin = QSpinBox()
        spin.setRange(3, 3600)
        spin.setValue(max(3, int(gap)))
        spin.setSuffix(" 秒")
        layout.addWidget(label)
        layout.addWidget(spin, 1)
        layout.addStretch(1)
        spin.valueChanged.connect(lambda value, row=item: self._set_gap(row, value))
        self._items.setItemWidget(item, box)

    def _set_gap(self, item: QListWidgetItem, value: int) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            data["gap"] = float(value)
            item.setData(Qt.ItemDataRole.UserRole, data)
            bundle_id = str(data.get("library_bundle_id", "")).strip()
            group_id = str(data.get("group_id", "")).strip()
            if bundle_id and group_id:
                self._persist_atomic_bundle_gaps(bundle_id, group_id)

    def _identify(self, step: dict[str, Any]) -> tuple[str, str, str]:
        step_type = str(step.get("type", "")).strip().lower()
        if step_type == "voice":
            item_id = str(step.get("voice_id", "")).strip()
            return "voice", item_id, self._voice_name(item_id)
        if step_type in {"text", "at", "notice"}:
            item_id = str(step.get("template_id", "")).strip()
            if item_id:
                return step_type, item_id, self._text_name(item_id)
            body = str(step.get("body", "")).strip() or "未命名"
            return step_type, "", body
        if step_type in {"oa", "doc", "channel"}:
            item_id = str(step.get("template_id", "")).strip()
            return step_type, item_id, self._template_name("oa", item_id)
        if step_type in {"image", "video"}:
            path = str(step.get("path", "")).strip()
            item_id, name = self._media_identity(step_type, path)
            return step_type, item_id, name
        if step_type == "bundle":
            bundle_id = str(step.get("bundle_id", "")).strip()
            bundle = get_bundle(bundle_id, account_id=self._account_id) if bundle_id else None
            name = bundle.name if bundle is not None else str(step.get("bundle_name", "")).strip()
            if not name or name == bundle_id:
                name = "未命名素材包"
            return "bundle", bundle_id, name
        if step_type == "wxa":
            item_id = str(step.get("template_id", "")).strip()
            return "wxa", item_id, self._template_name("wxa", item_id)
        return step_type or "unknown", "", "未命名"

    def _voice_name(self, item_id: str) -> str:
        for row in self._library.list():
            if row.id == item_id:
                return str(row.note or row.label or "").strip() or "未命名"
        return "未命名"

    def _text_name(self, item_id: str) -> str:
        try:
            template = self._library.get_text_template(item_id)
        except Exception:
            return "未命名"
        name = str(template.name or "").strip()
        return name if name and name != item_id else "未命名"

    def _template_name(self, kind: str, item_id: str) -> str:
        payload = self._load_template(kind, item_id)
        name = str(payload.get("name") or payload.get("title") or "").strip()
        return name if name and name != item_id else "未命名"

    def _media_identity(self, kind: str, path: str) -> tuple[str, str]:
        module = self._template_module(kind)
        if module is None:
            return "", "未命名"
        try:
            files = module.list_templates(self._account_id)
        except Exception:
            return "", "未命名"
        for fp in files:
            try:
                payload = module.load_template(str(fp), account_id=self._account_id)
            except Exception:
                continue
            stored = str(payload.get("path") or payload.get("video_path") or "").strip()
            if stored != path:
                continue
            item_id = str(payload.get("template_id") or fp.stem).strip()
            name = str(payload.get("name") or "").strip()
            if payload.get("unnamed") or not name or name == item_id:
                name = "未命名"
            return item_id, name
        return "", "未命名"

    def _load_template(self, kind: str, item_id: str) -> dict[str, Any]:
        module = self._template_module(kind)
        if module is None or not item_id:
            return {}
        try:
            payload = module.load_template(item_id, account_id=self._account_id)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _template_module(kind: str):
        if kind == "image":
            from app.broadcast import image_templates as module
        elif kind == "video":
            from app.broadcast import video_templates as module
        elif kind in {"oa", "doc", "channel"}:
            from app.broadcast import oa_templates as module
        elif kind == "wxa":
            from app.broadcast import wxa_templates as module
        else:
            return None
        return module

    def _on_menu(self, pos) -> None:
        item = self._items.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
            return
        self._items.setCurrentItem(item)
        menu = QMenu(self)
        add_copy_action(menu, self._items)
        act_open = menu.addAction("查看详情")
        act_rename = menu.addAction("重命名")
        act_remove = menu.addAction("移除")
        chosen = menu.exec(self._items.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._open_from_menu(item)
        elif chosen == act_rename:
            self._rename_selected()
        elif chosen == act_remove:
            self._remove_selected()

    def _open_row(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
            return
        row_type = str(data.get("_row_type", ""))
        if row_type == "bundle_header" or str(data.get("asset_type", "")) == "bundle":
            if str(data.get("presentation", "atomic")) == "atomic":
                self._toggle_atomic_bundle(item)
                return
            return
        self._open_preview(data)

    def _open_from_menu(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
            return
        if str(data.get("_row_type", "")) == "bundle_header" or str(data.get("asset_type", "")) == "bundle":
            self._open_bundle(str(data.get("item_id", "")).strip(), item)
            return
        self._open_preview(data)

    def _toggle_atomic_bundle(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        group_id = str(data.get("group_id", "")).strip()
        bundle_id = str(data.get("item_id", "")).strip()
        header_row = self._items.row(item)
        if header_row < 0 or not group_id:
            return
        if self._group_has_child(group_id):
            self._remove_group_children(group_id)
            return
        bundle = get_bundle(bundle_id, account_id=self._account_id) if bundle_id else None
        if bundle is None or not list(bundle.items):
            QMessageBox.information(self, "素材包", "这个素材包里没有可展开的素材。")
            return
        steps = self._steps_from_bundle(bundle)
        if not steps:
            QMessageBox.information(self, "素材包", "这个素材包里没有可展开的素材。")
            return
        gaps = list(bundle.step_gaps)
        insert_at = header_row + 1
        for index, step in enumerate(steps):
            self._add_step_row(
                step,
                row_type="bundle_child",
                group_id=group_id,
                library_bundle_id=bundle_id,
                insert_at=insert_at,
            )
            insert_at += 1
            if index < len(steps) - 1:
                gap = float(gaps[index]) if index < len(gaps) else 3
                self._add_gap_row(gap, group_id=group_id, library_bundle_id=bundle_id, insert_at=insert_at)
                insert_at += 1

    def _group_has_child(self, group_id: str) -> bool:
        for index in range(self._items.count()):
            row = self._items.item(index).data(Qt.ItemDataRole.UserRole)
            if (
                isinstance(row, dict)
                and str(row.get("group_id", "")).strip() == group_id
                and str(row.get("_row_type", "")) == "bundle_child"
            ):
                return True
        return False

    def _remove_group_children(self, group_id: str) -> None:
        for index in range(self._items.count() - 1, -1, -1):
            row = self._items.item(index).data(Qt.ItemDataRole.UserRole)
            if not isinstance(row, dict) or str(row.get("group_id", "")).strip() != group_id:
                continue
            if str(row.get("_row_type", "")) == "bundle_header":
                continue
            self._items.takeItem(index)

    def _steps_from_bundle(self, bundle) -> list[dict[str, Any]]:
        from gui.tabs.library_bundle_tab import LibraryBundleTab

        asset_map = self._asset_map()
        steps: list[dict[str, Any]] = []
        for item in bundle.items:
            step = LibraryBundleTab._bundle_item_to_step(None, item.asset_type, item.item_id, asset_map)
            if isinstance(step, dict):
                steps.append(step)
        return steps

    def _on_rows_moved(self, *_args) -> None:
        QTimer.singleShot(0, self._repack_bundle_groups)

    def _repack_bundle_groups(self) -> None:
        rows: list[dict[str, Any]] = []
        for index in range(self._items.count()):
            data = self._items.item(index).data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                rows.append(dict(data))
        if not any(str(row.get("group_id", "")).strip() for row in rows):
            return
        groups: dict[str, dict[str, Any]] = {}
        top: list[Any] = []
        for data in rows:
            group_id = str(data.get("group_id", "")).strip()
            row_type = str(data.get("_row_type", ""))
            if row_type == "bundle_header" and group_id:
                bucket = groups.setdefault(group_id, {"header": None, "rest": []})
                bucket["header"] = data
                if not any(item == ("group", group_id) for item in top):
                    top.append(("group", group_id))
                continue
            if group_id:
                bucket = groups.setdefault(group_id, {"header": None, "rest": []})
                bucket["rest"].append(data)
                if bucket["header"] is None and not any(item == ("group", group_id) for item in top):
                    top.append(("group", group_id))
                continue
            top.append(("row", data))
        rebuilt: list[dict[str, Any]] = []
        for kind, payload in top:
            if kind == "row":
                rebuilt.append(payload)
                continue
            bucket = groups.get(str(payload), {})
            header = bucket.get("header")
            if isinstance(header, dict):
                rebuilt.append(header)
            for child in bucket.get("rest", []):
                rebuilt.append(child)
        if rebuilt == rows:
            return
        self._items.blockSignals(True)
        self._items.clear()
        for data in rebuilt:
            self._restore_row(data)
        self._items.blockSignals(False)
        self._normalize_gaps()

    def _restore_row(self, data: dict[str, Any]) -> None:
        row_type = str(data.get("_row_type", ""))
        if row_type == "gap":
            self._add_gap_row(
                float(data.get("gap", 3)),
                group_id=str(data.get("group_id", "")).strip(),
                library_bundle_id=str(data.get("library_bundle_id", "")).strip(),
            )
            return
        step = data.get("step") if isinstance(data.get("step"), dict) else {}
        item = self._add_step_row(
            step,
            row_type=row_type or "step",
            group_id=str(data.get("group_id", "")).strip(),
            presentation=str(data.get("presentation", "")).strip(),
            library_bundle_id=str(data.get("library_bundle_id", "")).strip(),
        )
        name = str(data.get("name", "")).strip()
        if name:
            stored = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(stored, dict):
                stored["name"] = name
                stored["orig_name"] = str(data.get("orig_name", "")).strip()
                item.setData(Qt.ItemDataRole.UserRole, stored)
                asset_type = str(stored.get("asset_type", "")).strip()
                prefix = "    " if row_type == "bundle_child" else ""
                if row_type == "bundle_header":
                    suffix = "（展开）" if stored.get("presentation") == "expanded" else "（原子）"
                    item.setText(f"素材包 · {name}{suffix}")
                else:
                    item.setText(f"{prefix}{asset_type_to_zh(asset_type)} · {name}")

    def _persist_atomic_bundle_gaps(self, bundle_id: str, group_id: str) -> None:
        bundle = get_bundle(bundle_id, account_id=self._account_id)
        if bundle is None:
            return
        gaps: list[float] = []
        for index in range(self._items.count()):
            data = self._items.item(index).data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            if str(data.get("group_id", "")).strip() != group_id:
                continue
            if str(data.get("_row_type", "")) != "gap":
                continue
            gaps.append(max(3.0, float(data.get("gap", 3))))
        save_bundle(bundle.model_copy(update={"step_gaps": gaps}), account_id=self._account_id)

    def _open_bundle(self, bundle_id: str, row: QListWidgetItem) -> None:
        from gui.tabs.library_bundle_tab import BundleDetailDialog, rename_underlying_asset

        bundle = get_bundle(bundle_id, account_id=self._account_id) if bundle_id else None
        if bundle is None:
            QMessageBox.information(self, "素材包不存在", "这个素材包已经不在素材库里。")
            return
        dialog = BundleDetailDialog(
            bundle,
            self._asset_map(),
            self,
            preview_service_provider=self._preview_service_provider,
            account_id=self._account_id,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for asset_type, item_id, new_name in dialog.pending_renames():
            try:
                rename_underlying_asset(asset_type, item_id, new_name, self._account_id)
            except Exception:
                QMessageBox.warning(self, "改名失败", "有的素材没能改名，请稍后在素材库重试。")
        try:
            saved = save_bundle(dialog.result_bundle(), account_id=self._account_id)
        except Exception:
            QMessageBox.warning(self, "保存失败", "素材包保存失败，请稍后重试。")
            return
        data = row.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            data["name"] = saved.name
            data["orig_name"] = saved.name
            row.setData(Qt.ItemDataRole.UserRole, data)
            suffix = "（展开）" if str(data.get("presentation", "")) == "expanded" else "（原子）"
            row.setText(f"素材包 · {saved.name}{suffix}")

    def _asset_map(self) -> dict[tuple[str, str], dict[str, Any]]:
        mapping: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._library.list():
            mapping[("voice", row.id)] = {
                "asset_type": "voice",
                "item_id": row.id,
                "name": row.note or row.label or "未命名",
                "file": row.file,
                "path": row.file,
            }
        try:
            templates = self._library.list_text_templates(include_archived=True)
        except Exception:
            templates = []
        for template in templates:
            kind = "text"
            payload: dict[str, Any] = {
                "asset_type": kind,
                "item_id": template.id,
                "name": template.name,
                "variants": list(template.variants),
            }
            mapping[(kind, template.id)] = payload
        for kind in ("image", "video", "oa", "wxa"):
            # 企微文档和视频号与公众号共用模板文件，下面按 card_kind 改显示类型。
            module = self._template_module(kind)
            if module is None:
                continue
            try:
                files = module.list_templates(self._account_id)
            except Exception:
                continue
            for fp in files:
                try:
                    loaded = module.load_template(str(fp), account_id=self._account_id)
                except Exception:
                    continue
                if not isinstance(loaded, dict):
                    continue
                item_id = str(loaded.get("template_id") or fp.stem).strip()
                display_kind = kind
                if kind == "oa":
                    from app.broadcast.link_cards import card_kind_of

                    display_kind = card_kind_of(loaded)
                if not item_id or (display_kind, item_id) in mapping:
                    continue
                row = dict(loaded)
                row["asset_type"] = display_kind
                row["item_id"] = item_id
                if not str(row.get("name", "")).strip():
                    row["name"] = "未命名"
                mapping[(display_kind, item_id)] = row
        return mapping

    def _open_preview(self, data: dict[str, Any], parent: QWidget | None = None) -> None:
        asset_type = str(data.get("asset_type", "")).strip()
        name = str(data.get("name", "")).strip() or "未命名"
        provider = self._preview_service_provider
        payload = self._preview_payload(data)
        host = parent if parent is not None else self
        if provider is None:
            QMessageBox.information(host, "素材详情", f"类型：{asset_type_to_zh(asset_type)}\n名称：{name}")
            return
        dialog = QDialog(host)
        dialog.setWindowTitle(f"素材详情 · {name}")
        dialog.resize(520, 560)
        layout = QVBoxLayout(dialog)
        panel = AssetPreviewPanel(preview_service_provider=provider)
        panel.set_asset(payload)
        layout.addWidget(panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _preview_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        from app.broadcast.asset_preview_source import asset_preview_payload

        step = data.get("step") if isinstance(data.get("step"), dict) else {}
        asset_type = str(data.get("asset_type") or step.get("type") or "").strip()
        item_id = str(data.get("item_id") or "").strip()
        merged = dict(step)
        if asset_type and not str(merged.get("type") or "").strip():
            merged["type"] = asset_type
        if item_id:
            merged.setdefault("item_id", item_id)
            if asset_type == "voice":
                merged.setdefault("voice_id", item_id)
            if asset_type in {"text", "link", "oa", "doc", "channel", "wxa"}:
                merged.setdefault("template_id", item_id)
            if asset_type in {"image", "video"}:
                merged.setdefault("template_id", item_id)
        return asset_preview_payload(
            self._account_id,
            merged,
            name=str(data.get("name") or ""),
        )

    def _rename_selected(self) -> None:
        item = self._items.currentItem()
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or str(data.get("_row_type", "")) == "gap":
            return
        new_name, ok = QInputDialog.getText(self, "重命名素材", "素材名字：", text=str(data.get("name", "")))
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            return
        asset_type = str(data.get("asset_type", "")).strip()
        item_id = str(data.get("item_id", "")).strip()
        row_type = str(data.get("_row_type", ""))
        if asset_type == "bundle":
            conflict = bundle_name_conflict(self._account_id, new_name, exclude_id=item_id)
        elif item_id:
            conflict = asset_name_conflict(self._account_id, asset_type, new_name, exclude_item_id=item_id)
        else:
            conflict = None
        if conflict:
            QMessageBox.warning(self, "名称重复", conflict)
            return
        data["name"] = new_name
        item.setData(Qt.ItemDataRole.UserRole, data)
        if row_type == "bundle_header":
            suffix = "（展开）" if str(data.get("presentation", "")) == "expanded" else "（原子）"
            item.setText(f"素材包 · {new_name}{suffix}")
        else:
            prefix = "    " if row_type == "bundle_child" else ""
            item.setText(f"{prefix}{asset_type_to_zh(asset_type)} · {new_name}")

    def _remove_selected(self) -> None:
        item = self._items.currentItem()
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        group_id = ""
        if isinstance(data, dict) and str(data.get("_row_type", "")) == "bundle_header":
            group_id = str(data.get("group_id", "")).strip()
        if group_id:
            for index in range(self._items.count() - 1, -1, -1):
                row = self._items.item(index).data(Qt.ItemDataRole.UserRole)
                if isinstance(row, dict) and str(row.get("group_id", "")).strip() == group_id:
                    self._items.takeItem(index)
        else:
            row = self._items.currentRow()
            if row >= 0:
                self._items.takeItem(row)
        self._normalize_gaps()

    def _normalize_gaps(self) -> None:
        changed = True
        while changed:
            changed = False
            count = self._items.count()
            if count == 0:
                return
            first = self._items.item(0)
            last = self._items.item(count - 1)
            if self._is_gap(first):
                self._items.takeItem(0)
                changed = True
                continue
            if self._is_gap(last):
                self._items.takeItem(count - 1)
                changed = True
                continue
            for index in range(count - 1):
                if self._is_gap(self._items.item(index)) and self._is_gap(self._items.item(index + 1)):
                    self._items.takeItem(index + 1)
                    changed = True
                    break

    def _is_gap(self, item: QListWidgetItem | None) -> bool:
        if item is None:
            return False
        data = item.data(Qt.ItemDataRole.UserRole)
        return isinstance(data, dict) and str(data.get("_row_type", "")) == "gap"

    def _save(self) -> None:
        from app.broadcast.pending_plans import PendingPlanStore
        from gui.tabs.library_bundle_tab import rename_underlying_asset

        self._normalize_gaps()
        sequence: list[dict[str, Any]] = []
        gaps: list[float] = []
        pending_gap: float | None = None
        renames: list[tuple[str, str, str]] = []
        index = 0
        count = self._items.count()
        while index < count:
            item = self._items.item(index)
            data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if not isinstance(data, dict):
                index += 1
                continue
            row_type = str(data.get("_row_type", ""))
            group_id = str(data.get("group_id", "")).strip()
            if row_type == "gap" and not group_id:
                pending_gap = max(3.0, float(data.get("gap", 3)))
                index += 1
                continue
            if row_type == "bundle_header":
                presentation = str(data.get("presentation", "atomic")).strip() or "atomic"
                name = str(data.get("name", "")).strip()
                orig = str(data.get("orig_name", "")).strip()
                item_id = str(data.get("item_id", "")).strip()
                items: list[dict[str, Any]] = []
                item_gaps: list[float] = []
                index += 1
                while index < count:
                    child_item = self._items.item(index)
                    child = child_item.data(Qt.ItemDataRole.UserRole) if child_item is not None else None
                    if not isinstance(child, dict) or str(child.get("group_id", "")).strip() != group_id:
                        break
                    child_type = str(child.get("_row_type", ""))
                    if child_type == "gap":
                        item_gaps.append(max(3.0, float(child.get("gap", 3))))
                    elif child_type == "bundle_child":
                        self._collect_rename(child, renames)
                        if presentation == "expanded":
                            items.append(self._named_step(child))
                    index += 1
                step = dict(data.get("step") or {})
                step["type"] = "bundle"
                step["bundle_id"] = item_id
                step["bundle_name"] = name
                step["presentation"] = "expanded" if presentation == "expanded" else "atomic"
                step["items"] = items if presentation == "expanded" else []
                step["item_gaps"] = item_gaps if presentation == "expanded" else []
                if presentation != "expanded" and item_gaps and item_id:
                    self._persist_atomic_bundle_gaps(item_id, group_id)
                if name and name != orig and item_id:
                    bundle = get_bundle(item_id, account_id=self._account_id)
                    if bundle is not None:
                        save_bundle(bundle.model_copy(update={"name": name}), account_id=self._account_id)
                self._append_saved_step(sequence, gaps, step, pending_gap)
                pending_gap = None
                continue
            if row_type in {"gap", "bundle_child"}:
                index += 1
                continue
            self._collect_rename(data, renames)
            step = self._named_step(data)
            self._append_saved_step(sequence, gaps, step, pending_gap)
            pending_gap = None
            index += 1
        if not sequence:
            QMessageBox.warning(self, "发送内容为空", "请至少保留一条素材。")
            return
        plan = dict(self._entry.plan if isinstance(self._entry.plan, dict) else {})
        plan["sequence"] = sequence
        plan["step_gaps"] = gaps
        try:
            BroadcastPlan.model_validate(plan)
        except Exception:
            QMessageBox.warning(self, "计划不可用", "当前内容无法保存，请检查素材是否还在。")
            return
        for asset_type, item_id, new_name in renames:
            try:
                rename_underlying_asset(asset_type, item_id, new_name, self._account_id)
            except Exception as exc:
                QMessageBox.warning(self, "改名失败", str(exc) or "有的素材没能改名。")
                return
        store = PendingPlanStore(account_id=self._account_id)
        if not store.update_plan(str(self._entry.id), plan):
            QMessageBox.information(self, "提示", "该计划已不存在。")
            self.reject()
            return
        self.trigger_changed = self._store_trigger(store)
        self.accept()

    def _collect_rename(self, data: dict[str, Any], renames: list[tuple[str, str, str]]) -> None:
        name = str(data.get("name", "")).strip()
        orig = str(data.get("orig_name", "")).strip()
        asset_type = str(data.get("asset_type", "")).strip()
        item_id = str(data.get("item_id", "")).strip()
        if name and name != orig and asset_type != "bundle" and item_id:
            renames.append((asset_type, item_id, name))

    def _named_step(self, data: dict[str, Any]) -> dict[str, Any]:
        step = dict(data.get("step") or {})
        name = str(data.get("name", "")).strip()
        if str(step.get("type", "")).strip() in {"text", "at", "notice"} and not str(step.get("template_id", "")).strip():
            step["body"] = name
        elif name:
            step["display_name"] = name
        return step

    def _append_saved_step(
        self,
        sequence: list[dict[str, Any]],
        gaps: list[float],
        step: dict[str, Any],
        pending_gap: float | None,
    ) -> None:
        if sequence and pending_gap is not None:
            gaps.append(pending_gap)
        elif sequence:
            gaps.append(3.0)
        sequence.append(step)

    def _store_trigger(self, store) -> bool:
        if self._entry.mode == "cron":
            return False
        now = datetime.now().replace(microsecond=0)
        chosen = self._time_edit.dateTime().toPython()
        if chosen.tzinfo is not None:
            chosen = chosen.astimezone().replace(tzinfo=None)
        chosen = chosen.replace(microsecond=0)
        if chosen < now:
            chosen = now
        previous = str(self._entry.trigger_at or "").strip().replace("T", " ")[:19]
        trigger_at = chosen.isoformat(timespec="seconds")
        label = None
        if previous != chosen.strftime("%Y-%m-%d %H:%M:%S"):
            label = chosen.strftime("%Y-%m-%d %H:%M:%S")
        store.update_trigger(str(self._entry.id), trigger_at, label=label)
        return previous != chosen.strftime("%Y-%m-%d %H:%M:%S")

    def _fill_lookup_ids(self, step: dict[str, Any]) -> None:
        kind = str(step.get("type", "")).strip().lower()
        item_id = str(step.get("item_id", "")).strip()
        if kind == "voice" and not str(step.get("voice_id", "")).strip():
            step["voice_id"] = item_id
        if kind in {"text", "oa", "doc", "channel", "wxa"} and not str(step.get("template_id", "")).strip():
            step["template_id"] = item_id
        if kind not in {"image", "video"} or str(step.get("path", "")).strip() or not item_id:
            return
        module = self._template_module(kind)
        if module is None:
            return
        try:
            files = module.list_templates(self._account_id)
        except Exception:
            return
        for fp in files:
            try:
                loaded = module.load_template(str(fp), account_id=self._account_id)
            except Exception:
                continue
            if not isinstance(loaded, dict):
                continue
            template_id = str(loaded.get("template_id") or fp.stem).strip()
            if template_id != item_id:
                continue
            step["path"] = str(loaded.get("path") or loaded.get("video_path") or "").strip()
            thumb = str(loaded.get("thumb_path") or "").strip()
            if thumb:
                step["thumb_path"] = thumb
            return


def open_message_preview(
    parent: QWidget,
    step: dict[str, Any],
    *,
    account_id: str,
    preview_service_provider: Callable[[], PreviewService] | None,
    name: str = "",
) -> None:
    """双击一条素材时，按素材库里的那一条打开预览。"""
    from app.broadcast.asset_preview_source import asset_preview_payload

    payload = dict(step or {})
    if str(payload.get("type", "")).strip().lower() == "bundle":
        QMessageBox.information(parent, "素材详情", "这是素材包。请双击里面的某一条素材查看详情。")
        return
    preview = asset_preview_payload(account_id, payload, name=name)
    if not str(preview.get("asset_type", "")).strip():
        QMessageBox.information(parent, "素材详情", "这条消息暂时不能预览。")
        return
    host = _PreviewDialogHost(account_id, preview_service_provider)
    host._open_preview(
        {
            "asset_type": str(preview.get("asset_type") or ""),
            "item_id": str(preview.get("item_id") or ""),
            "name": str(preview.get("name") or name or ""),
            "step": payload,
            "preview": preview,
        },
        parent=parent,
    )


class _PreviewDialogHost:
    """只负责弹出和待执行详情相同的预览窗口。"""

    def __init__(self, account_id: str, preview_service_provider: Callable[[], PreviewService] | None) -> None:
        self._account_id = account_id.strip() or "default"
        self._preview_service_provider = preview_service_provider

    def _open_preview(self, data: dict[str, Any], parent: QWidget | None = None) -> None:
        ready = data.get("preview") if isinstance(data.get("preview"), dict) else None
        if ready is None:
            ready = PendingPlanDetailDialog._preview_payload(self, data)
        asset_type = str(ready.get("asset_type", "")).strip()
        name = str(ready.get("name", "")).strip() or "未命名"
        provider = self._preview_service_provider
        host = parent if parent is not None else None
        if provider is None:
            QMessageBox.information(
                host,
                "素材详情",
                f"类型：{asset_type_to_zh(asset_type)}\n名称：{name}",
            )
            return
        dialog = QDialog(host)
        dialog.setWindowTitle(f"素材详情 · {name}")
        dialog.resize(520, 560)
        layout = QVBoxLayout(dialog)
        panel = AssetPreviewPanel(preview_service_provider=provider)
        panel.set_asset(ready)
        layout.addWidget(panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
