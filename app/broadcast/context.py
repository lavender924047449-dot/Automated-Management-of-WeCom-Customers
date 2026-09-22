"""Step 3: GUI 进程级 AppContext。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.broadcast.paths import account_contacts_cache_path, account_voice_index_path, normalize_account_id
from app.broadcast.preview import PreviewService
from app.broadcast.report_store import ReportStore
from app.broadcast.scheduler import BroadcastScheduler, ScheduleEntry

ScheduleListener = Callable[[ScheduleEntry], None]


class AppContext:
    """统一持有账号相关后端资源，支持账号热切换。"""

    def __init__(self, account_id: str = "default") -> None:
        self._account_id = normalize_account_id(account_id)
        self._scheduler: BroadcastScheduler | None = None
        self._report_store: ReportStore | None = None
        self._preview_service: PreviewService | None = None
        self._schedule_listeners: list[ScheduleListener] = []

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def scheduler(self) -> BroadcastScheduler:
        if self._scheduler is None:
            self._scheduler = BroadcastScheduler(
                account_id=self._account_id,
                execute_cb=self._dispatch_schedule_entry,
            )
        return self._scheduler

    @property
    def report_store(self) -> ReportStore:
        if self._report_store is None:
            self._report_store = ReportStore(account_id=self._account_id)
        return self._report_store

    @property
    def preview_service(self) -> PreviewService:
        if self._preview_service is None:
            self._preview_service = PreviewService()
        return self._preview_service

    def voice_index_path(self) -> Path:
        return account_voice_index_path(self._account_id)

    def has_initialization_artifacts(self) -> bool:
        """首启判断依据：核心数据存在 OR 账号数据目录存在（任何子物皆算）。

        修订（2026-09-17）：只要 `data/<account_id>/` 目录下有任何文件或子目录，
        就视为已初始化，避免"账号目录已建但 voice_lib 还没抓过语音"的场景反复弹引导框。
        """
        from app.broadcast.paths import data_root_for

        if self.voice_index_path().exists() or account_contacts_cache_path(self._account_id).exists():
            return True
        root = data_root_for(self._account_id)
        try:
            return root.exists() and any(root.iterdir())
        except OSError:
            return False

    def add_schedule_listener(self, listener: ScheduleListener) -> None:
        if listener in self._schedule_listeners:
            return
        self._schedule_listeners.append(listener)

    def remove_schedule_listener(self, listener: ScheduleListener) -> None:
        self._schedule_listeners = [row for row in self._schedule_listeners if row != listener]

    def rebind(self, account_id: str) -> str:
        target = normalize_account_id(account_id)
        if target == self._account_id:
            return self._account_id
        self.shutdown_scheduler()
        self._account_id = target
        self._report_store = None
        self._preview_service = None
        return self._account_id

    def shutdown_scheduler(self) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        self._scheduler = None

    def shutdown(self) -> None:
        self.shutdown_scheduler()

    def _dispatch_schedule_entry(self, entry: ScheduleEntry) -> None:
        for listener in list(self._schedule_listeners):
            try:
                listener(entry)
            except Exception:
                continue


__all__ = ["AppContext", "ScheduleListener"]
