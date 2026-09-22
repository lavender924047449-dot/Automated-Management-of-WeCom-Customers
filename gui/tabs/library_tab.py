from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from app.broadcast.dto import AssetRowDTO
from app.broadcast.library import VoiceLibraryEntry
from app.broadcast.preview import PreviewService
from gui.tabs.library_bundle_tab import LibraryBundleTab
from gui.tabs.library_ingest_tab import LibraryIngestTab
from gui.tabs.library_saved_tab import LibrarySavedTab


class LibraryTab(QWidget):
    """素材库：增加素材 / 制作素材包 / 已保存的素材。"""

    libraryChanged = Signal(list)
    libraryAssetsChanged = Signal(list)

    def __init__(self, *, preview_service_provider: Callable[[], PreviewService]) -> None:
        super().__init__()
        self._account_id = "default"
        self._saved_tab = LibrarySavedTab(preview_service_provider=preview_service_provider)
        self._ingest_tab = LibraryIngestTab()
        self._bundle_tab = LibraryBundleTab(preview_service_provider=preview_service_provider)

        root = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._ingest_tab, "增加素材")
        self._tabs.addTab(self._bundle_tab, "制作素材包")
        self._tabs.addTab(self._saved_tab, "已保存的素材")
        root.addWidget(self._tabs, 1)

        self._saved_tab.libraryChanged.connect(self.libraryChanged.emit)
        self._saved_tab.libraryAssetsChanged.connect(lambda _rows: self._emit_assets())
        self._ingest_tab.assetsChanged.connect(self._reload_from_store)
        self._ingest_tab.unnamedCountChanged.connect(self._update_ingest_tab_title)
        self._bundle_tab.bundlesChanged.connect(self._emit_assets)

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
        # 注意：必须把 bundle 也合并进来，否则 MainWindow 首启同步 / 切账号时
        # `set_library_assets(active_assets())` 只塞进 saved 素材，编辑页
        # 「素材包」类型下拉会为空，直到用户手动触发一次 libraryAssetsChanged。
        assets = self._saved_tab.active_assets()
        for row in self._bundle_tab.list_bundle_assets():
            assets.append(
                AssetRowDTO.model_validate(
                    {
                        "asset_type": row.get("asset_type", "bundle"),
                        "item_id": row.get("item_id", ""),
                        "name": row.get("name", ""),
                        "extra": {"steps": row.get("steps", [])},
                    }
                )
            )
        return assets

    def _reload_from_store(self) -> None:
        self._saved_tab.reload_from_store()
        self._bundle_tab.set_assets([row.model_dump(mode="json") for row in self._saved_tab.active_assets()])
        self._update_ingest_tab_title(self._saved_tab.unnamed_count())
        self._emit_assets()

    def _run_unnamed_gc(self) -> None:
        removed = self._saved_tab.run_unnamed_gc(1800)
        if removed > 0:
            self._reload_from_store()
        else:
            self._update_ingest_tab_title(self._saved_tab.unnamed_count())

    def _update_ingest_tab_title(self, count: int) -> None:
        self._tabs.setTabText(0, f"增加素材（待命名 {max(0, int(count))}）")

    def _emit_assets(self) -> None:
        # 复用 active_assets()，保证 signal 和 pull 两条路径给出一致的合集。
        self.libraryAssetsChanged.emit(self.active_assets())
