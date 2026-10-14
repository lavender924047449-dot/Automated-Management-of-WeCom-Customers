from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from app.broadcast.dto import AssetRowDTO
from app.broadcast.library import VoiceLibraryEntry
from app.broadcast.paths import DEFAULT_ACCOUNT_ID
from app.broadcast.preview import PreviewService
from gui.tabs.library_bundle_tab import LibraryBundleTab
from gui.tabs.library_ingest_tab import LibraryIngestTab
from gui.tabs.library_saved_tab import LibrarySavedTab


class LibraryTab(QWidget):
    """素材库：增加素材 / 制作素材包 / 已保存的素材。"""

    libraryChanged = Signal(list)
    libraryAssetsChanged = Signal(list)
    assetsPurged = Signal(list, list)

    def __init__(self, *, preview_service_provider: Callable[[], PreviewService]) -> None:
        super().__init__()
        self._account_id = "default"
        self._saved_tab = LibrarySavedTab(preview_service_provider=preview_service_provider)
        self._ingest_tab = LibraryIngestTab(preview_service_provider=preview_service_provider)
        self._bundle_tab = LibraryBundleTab(preview_service_provider=preview_service_provider)

        root = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._ingest_tab, "增加素材")
        self._tabs.addTab(self._bundle_tab, "制作素材包")
        self._tabs.addTab(self._saved_tab, "已保存的素材")
        root.addWidget(self._tabs, 1)

        self._saved_tab.libraryChanged.connect(self.libraryChanged.emit)
        self._saved_tab.libraryAssetsChanged.connect(self._on_saved_assets_changed)
        # Step 4: 主模块删除后通知子模块同步移除
        self._saved_tab.itemsDeleted.connect(self._on_master_items_deleted)
        self._ingest_tab.assetsChanged.connect(self._reload_from_store)
        self._bundle_tab.bundlesChanged.connect(self._on_bundles_changed)

        self._gc_timer = QTimer(self)
        self._gc_timer.setInterval(5 * 60 * 1000)
        self._gc_timer.timeout.connect(self._run_unnamed_gc)
        self._gc_timer.start()
        self._reload_from_store()
        self._run_unnamed_gc()

    def set_account(self, account_id: str) -> None:
        self._account_id = account_id.strip() or "default"
        self._ingest_tab.set_account(self._account_id)
        self._saved_tab.set_account(self._account_id)
        self._bundle_tab.set_account(self._account_id)
        self._reload_from_store()

    def stop_listeners(self) -> None:
        self._ingest_tab.stop_all()

    def active_voice_entries(self) -> list[VoiceLibraryEntry]:
        return self._saved_tab.active_voice_entries()

    def active_assets(self) -> list[AssetRowDTO]:
        # 已保存素材里已经有素材包。这里用制作页的同一批包覆盖它们，
        # 只为补上「展开步骤」。按 id 替换，避免发送页下拉出现两个同名包。
        assets = self._saved_tab.active_assets()
        index_by_id = {
            row.item_id: index
            for index, row in enumerate(assets)
            if row.asset_type == "bundle" and row.item_id
        }
        for row in self._bundle_tab.list_bundle_assets():
            item_id = str(row.get("item_id", "")).strip()
            if not item_id:
                continue
            name = str(row.get("name", "")).strip() or "未命名素材包"
            existing = index_by_id.get(item_id)
            entered_at = ""
            if existing is not None:
                entered_at = str(assets[existing].extra.get("entered_at") or "").strip()
            extra: dict = {"steps": row.get("steps", [])}
            if entered_at:
                extra["entered_at"] = entered_at
            dto = AssetRowDTO.model_validate(
                {
                    "asset_type": "bundle",
                    "item_id": item_id,
                    "name": name,
                    "extra": extra,
                }
            )
            if existing is None:
                index_by_id[item_id] = len(assets)
                assets.append(dto)
            else:
                assets[existing] = dto
        return assets

    def _reload_from_store(self) -> None:
        self._saved_tab.reload_from_store()
        removed_items: list = []
        removed_bundles: list = []
        # 启动时账号还是占位 default，不要拿它去清真实账号以外的空数据。
        if self._account_id != DEFAULT_ACCOUNT_ID:
            removed_items, removed_bundles = self._bundle_tab.prune_missing(self._saved_tab.asset_identity_keys())
        if removed_items or removed_bundles:
            self._saved_tab.reload_from_store()
            self.assetsPurged.emit(removed_items, removed_bundles)
        self._bundle_tab.set_assets([row.model_dump(mode="json") for row in self._saved_tab.active_assets()])
        self._emit_assets()

    def _run_unnamed_gc(self) -> None:
        removed = self._saved_tab.run_unnamed_gc(1800)
        if removed > 0:
            self._reload_from_store()

    def _on_master_items_deleted(self, deleted: list) -> None:
        """主模块删除后，从素材包和发送序列里拿掉对应条目。包被拿空则整包删除。"""
        if not deleted:
            return
        non_bundle = [row for row in deleted if str(row[0]).strip().lower() != "bundle"]
        bundle_ids = [
            str(row[1]).strip()
            for row in deleted
            if str(row[0]).strip().lower() == "bundle" and len(row) > 1 and str(row[1]).strip()
        ]
        removed_bundles: list[str] = []
        if non_bundle:
            removed_bundles.extend(self._bundle_tab.remove_items(non_bundle))
        if bundle_ids:
            self._bundle_tab._reload_bundle_list()
            self._bundle_tab.bundlesChanged.emit()
            removed_bundles.extend(bundle_ids)
        self._ingest_tab.remove_items(deleted)
        self.assetsPurged.emit(list(deleted), removed_bundles)

    def _on_saved_assets_changed(self, _rows: list) -> None:
        """已保存素材增删改后，可选素材和发送页素材列表跟着变。"""
        self._bundle_tab.set_assets([row.model_dump(mode="json") for row in self._saved_tab.active_assets()])
        self._emit_assets()

    def _on_bundles_changed(self) -> None:
        """素材包增删改后，同步刷新「已保存的素材」列表并通知外部。"""
        self._saved_tab.reload_from_store()
        self._emit_assets()

    def _emit_assets(self) -> None:
        # 复用 active_assets()，保证 signal 和 pull 两条路径给出一致的合集。
        self.libraryAssetsChanged.emit(self.active_assets())
