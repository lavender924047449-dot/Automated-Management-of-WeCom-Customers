from __future__ import annotations



import threading

import uuid

from datetime import datetime

from typing import Any, Callable

from loguru import logger



from PySide6.QtCore import QObject, QThread, Qt, Signal

from PySide6.QtWidgets import (

    QAbstractItemView,

    QDialog,

    QGroupBox,

    QHBoxLayout,

    QInputDialog,

    QLabel,

    QListWidget,

    QListWidgetItem,

    QMenu,

    QMessageBox,

    QProgressBar,

    QPushButton,

    QTableWidget,

    QTableWidgetItem,

    QVBoxLayout,

    QWidget,

)



from app.broadcast.last_run_store import LastRunStore

from app.broadcast.plan import BroadcastPlan

from app.broadcast.report_store import ReportStore

from app.broadcast.run_service import RunService

from app.broadcast.runner import BroadcastRunner, RunSummary
from gui.copy_support import add_copy_action
from gui.widgets.send_board import SendBoard
from gui.widgets.send_board_model import build_plan_node





class _RunnerWorker(QObject):

    started = Signal(str, int)

    progress = Signal(int, int, object)

    finished = Signal(object)

    failed = Signal(str)



    def __init__(

        self,

        *,

        plan: object,

        run_uuid: str = "",

        only_failed: bool = False,

        only_step_indexes: set[int] | None = None,

        only_keys: set[tuple[int, int]] | None = None,

        resume_continue: bool = False,

        account_id: str = "default",

        dry_run: bool = True,

        report_store: ReportStore | None = None,

        run_service_factory: Callable[[str, ReportStore | None], RunService] | None = None,

        stop_event: threading.Event | None = None,

    ) -> None:

        super().__init__()

        self.plan = plan

        self.run_uuid = run_uuid

        self.only_failed = only_failed

        self.only_step_indexes = only_step_indexes

        self.only_keys = only_keys

        self.resume_continue = resume_continue

        self.account_id = account_id

        self.dry_run = dry_run

        self.report_store = report_store

        self.run_service_factory = run_service_factory

        self.stop_event = stop_event



    def run(self) -> None:

        try:

            run_uuid = self.run_uuid or str(uuid.uuid4())

            self.started.emit(run_uuid, len(self.plan.contacts) * len(self.plan.sequence))

            service = self._create_run_service()

            if self.resume_continue and self.run_uuid:

                summary = service.start(

                    self.plan,

                    resume_uuid=self.run_uuid,

                    only_failed=False,

                    only_step_indexes=self.only_step_indexes,

                    only_keys=self.only_keys,

                    progress_cb=self._on_progress,

                    dry_run=self.dry_run,

                    run_uuid=run_uuid,

                    stop_event=self.stop_event,

                )

            else:

                summary = service.start(

                    self.plan,

                    resume_uuid=self.run_uuid if self.only_failed else "",

                    only_failed=self.only_failed,

                    only_step_indexes=self.only_step_indexes,

                    only_keys=self.only_keys,

                    progress_cb=self._on_progress,

                    dry_run=self.dry_run,

                    run_uuid=run_uuid,

                    stop_event=self.stop_event,

                )

            self.finished.emit(summary)

        except Exception as exc:

            self.failed.emit(str(exc))



    def _on_progress(self, record, done: int, total: int) -> None:
        from types import SimpleNamespace

        # 跨线程信号只带普通字段。素材包里每一条用 inner_index 区分，不能在传递时丢掉。
        self.progress.emit(
            done,
            total,
            SimpleNamespace(
                contact=str(getattr(record, "contact", "") or ""),
                conv_local=getattr(record, "conv_local", None),
                step_index=int(getattr(record, "step_index", -1)),
                sequence_index=int(getattr(record, "sequence_index", -1)),
                step_type=str(getattr(record, "step_type", "") or ""),
                status=str(getattr(record, "status", "") or ""),
                error=str(getattr(record, "error", "") or ""),
                inner_index=int(getattr(record, "inner_index", -1)),
                phase=str(getattr(record, "phase", "") or ""),
            ),
        )



    def _create_run_service(self) -> RunService:

        if self.run_service_factory is not None:

            return self.run_service_factory(self.account_id, self.report_store)

        return RunService(account_id=self.account_id, report_store=self.report_store)





class RunMonitorPanel(QWidget):

    """Step 6: 执行结果页（进度、历史、重试、导出）。"""

    pendingTimerReschedule = Signal(str)
    pendingTimerCancel = Signal(str)
    pendingCronCancel = Signal(str)



    def __init__(

        self,

        *,

        plan_provider: Callable[[], object],

        report_store_provider: Callable[[str], ReportStore] | None = None,

        preview_service_provider: Callable[[], object] | None = None,

    ) -> None:

        super().__init__()

        self._plan_provider = plan_provider

        self._report_store_provider = report_store_provider

        self._preview_service_provider = preview_service_provider

        self._run_service_factory: Callable[[str, ReportStore | None], RunService] | None = None

        self._account_id = "default"

        self._thread: QThread | None = None

        self._worker: _RunnerWorker | None = None

        self._stop_event: threading.Event | None = None

        self._report_store: ReportStore | None = None

        self._run_uuid = ""

        self._run_total = 0

        self._run_done = 0

        self._run_failed = 0

        self._last_summary: RunSummary | None = None

        self._last_plan: BroadcastPlan | None = None

        self._last_plan_run_uuid = ""

        self._interrupted = False
        self._pending_open_id = ""
        self._progress_phase = "idle"
        self._contact_step_counts: dict[str, int] = {}
        self._current_contact = ""
        self._current_sequence_index = -1
        self._current_step_index = -1
        self._unit_state: dict[tuple[int, int], dict] = {}
        self._live_started_date = None
        self._live_clock = ""
        self._restart_after_stop = False
        self._restart_plan_after_stop: BroadcastPlan | None = None
        self._queued_retry_keys: set[tuple[int, int]] | None = None
        self._resume_requested: str | None = None

        self._build_ui()

        self._bind_events()



    def _build_ui(self) -> None:

        outer = QVBoxLayout(self)
        self._board = SendBoard()
        self._board.dateChanged.connect(lambda *_: self._sync_board())
        self._board.stopClicked.connect(self._on_board_stop)
        self._board.restartClicked.connect(self._on_board_restart)
        self._board.retryFailedClicked.connect(self._on_board_retry_failed)
        self._board.rowAction.connect(self._on_board_row_action)
        self._board.nodeActivated.connect(self._on_board_node)
        self._board.executing.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._board.executing.customContextMenuRequested.connect(self._on_board_executing_context_menu)
        self._board.pending.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._board.pending.customContextMenuRequested.connect(self._on_board_pending_context_menu)
        self._board.cron_box.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._board.cron_box.customContextMenuRequested.connect(self._on_board_cron_context_menu)
        outer.addWidget(self._board, 1)
        self._legacy_host = QWidget()
        self._legacy_host.hide()
        root = QVBoxLayout(self._legacy_host)



        card = QWidget()

        card_layout = QVBoxLayout(card)

        self._current_title = QLabel("当前运行计划：无")

        self._progress = QProgressBar()

        self._progress.setRange(0, 1)

        self._progress.setValue(0)
        self._progress.setFormat("尚未开始")
        self._progress.setToolTip(
            "按「每个联系人的每一条消息」计数。一条消息发送结束后才加 1，"
            "发送过程中和等待间隔时数字保持不变。"
        )

        self._counts = QLabel(self._progress_summary())
        self._counts.setWordWrap(True)

        row = QHBoxLayout()

        self._btn_start = QPushButton("开始执行")

        self._btn_interrupt = QPushButton("执行中断")

        self._btn_continue = QPushButton("继续未完成")

        self._btn_retry_failed = QPushButton("重试失败项")

        self._btn_interrupt.setEnabled(False)

        self._btn_continue.setEnabled(False)

        self._btn_retry_failed.setEnabled(False)

        row.addWidget(self._btn_start)

        row.addWidget(self._btn_interrupt)

        row.addWidget(self._btn_continue)

        row.addWidget(self._btn_retry_failed)

        row.addStretch(1)

        card_layout.addWidget(self._current_title)

        card_layout.addWidget(self._progress)

        card_layout.addWidget(self._counts)

        card_layout.addLayout(row)

        root.addWidget(card)

        # 待执行计划区域
        pending_box = QGroupBox("待执行计划")
        pending_layout = QVBoxLayout(pending_box)
        self._pending_list = QListWidget()
        self._pending_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._pending_list.customContextMenuRequested.connect(self._on_pending_context_menu)
        self._pending_list.itemDoubleClicked.connect(self._toggle_pending_detail)
        pending_layout.addWidget(self._pending_list, 1)
        root.addWidget(pending_box)


        body = QHBoxLayout()

        self._history = QListWidget()

        self._history.setMinimumWidth(280)
        self._history.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        body.addWidget(self._history, 2)



        self._table = QTableWidget(0, 7)

        self._table.setHorizontalHeaderLabels(

            ["联系人", "消息序号", "类型", "状态", "时间戳", "失败原因", "操作"]

        )

        self._table.verticalHeader().setVisible(False)

        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        body.addWidget(self._table, 8)

        root.addLayout(body, 1)



        self._retention_note = QLabel("发送记录默认保留 90 天，超期自动清理，请及时导出")

        self._retention_note.setStyleSheet("color: #6b7280;")

        root.addWidget(self._retention_note)



    def _bind_events(self) -> None:

        self._btn_start.clicked.connect(self.start_run)

        self._btn_interrupt.clicked.connect(self.request_interrupt)

        self._btn_continue.clicked.connect(self.continue_run)

        self._btn_retry_failed.clicked.connect(self.retry_failed)

        self._history.itemSelectionChanged.connect(self._on_history_selected)
        self._history.customContextMenuRequested.connect(self._on_history_context_menu)



    def set_account(self, account_id: str) -> None:

        self._account_id = account_id.strip() or "default"

        self._report_store = None

        self.reset_for_account()
        self.refresh_pending_plans()



    def set_report_store(self, report_store: ReportStore) -> None:

        self._report_store = report_store

        self._refresh_history()

        self._restore_last_run_context()



    def set_run_service_factory(

        self,

        factory: Callable[[str, ReportStore | None], RunService],

    ) -> None:

        self._run_service_factory = factory



    def reset_for_account(self) -> None:

        self._run_uuid = ""

        self._run_total = 0

        self._run_done = 0

        self._run_failed = 0

        self._last_summary = None

        self._last_plan = None

        self._last_plan_run_uuid = ""

        self._interrupted = False

        self._progress_phase = "idle"
        self._contact_step_counts = {}
        self._current_contact = ""
        self._current_sequence_index = -1
        self._current_step_index = -1
        self._live_started_date = None
        self._live_clock = ""
        self._unit_state = {}

        self._set_message_progress(0, 0, phase="idle")

        self._current_title.setText("当前运行计划：无")

        self._counts.setText(self._progress_summary())

        self._btn_interrupt.setEnabled(False)

        self._btn_continue.setEnabled(False)

        self._btn_retry_failed.setEnabled(False)

        self._table.setRowCount(0)

        self._refresh_history()

        self._restore_last_run_context()
        self._sync_board()



    def start_run(self) -> None:

        if self._thread is not None:

            QMessageBox.information(self, "提示", "当前已有任务在执行，请稍后重试。")

            return

        try:

            plan = self._plan_provider()

        except Exception as exc:

            logger.warning("执行前构建计划失败: {}", exc)
            QMessageBox.warning(self, "计划不可用", "当前计划配置不完整，请先回到编辑页检查。")

            return

        self.start_run_with_plan(plan)



    def start_run_with_plan(self, plan: object) -> None:

        self._start_worker(plan=plan, run_uuid="", only_failed=False)



    def request_interrupt(self) -> None:

        if self._stop_event is None or self._thread is None:

            return

        self._interrupted = True
        self._persist_last_run("interrupted")
        self._progress.setFormat(f"正在停止 {self._run_done}/{max(1, self._run_total)} 条")
        self._current_title.setText("已中断，正在停止当前发送…")
        self._stop_event.set()
        self._btn_interrupt.setEnabled(False)



    def continue_run(self, run_uuid: str = "") -> None:

        target_uuid = str(run_uuid or self._run_uuid).strip()
        if self._thread is not None:
            self._resume_requested = target_uuid
            return

        snap = LastRunStore(account_id=self._account_id).load()
        snap_matches = (
            snap is not None
            and snap.run_uuid == target_uuid
            and snap.status in {"interrupted", "running"}
        )
        if not target_uuid or (not snap_matches and not self._report_can_resume(target_uuid)):
            QMessageBox.information(self, "提示", "这轮任务没有可继续的中断记录。")
            return

        if target_uuid != self._run_uuid:
            self._run_uuid = target_uuid
            try:
                report = self._active_report_store().load_report(target_uuid)
            except Exception:
                QMessageBox.information(self, "提示", "找不到这轮任务的执行记录。")
                return
            self._restore_plan_from_report(report.rows, run_uuid=target_uuid)
            self._restore_live_marker_from_report(target_uuid, resumable=True)

        plan = self._resolve_plan_for_retry()

        if plan is None or not self._run_uuid:

            QMessageBox.information(self, "提示", "没有可继续的执行上下文。")

            return

        if not self._report_can_resume(self._run_uuid):
            QMessageBox.information(self, "提示", "这轮任务没有还没发出去的消息，无法接续。")
            return

        self._start_worker(

            plan=plan,

            run_uuid=self._run_uuid,

            only_failed=False,

            resume_continue=True,

        )



    def retry_failed(self) -> None:

        plan = self._resolve_plan_for_retry()

        if plan is None or not self._run_uuid:

            QMessageBox.information(self, "提示", "无法重试：缺少计划快照，请重新执行一次。")

            return

        self._start_worker(plan=plan, run_uuid=self._run_uuid, only_failed=True)



    def _start_worker(

        self,

        *,

        plan: object,

        run_uuid: str,

        only_failed: bool,

        only_step_indexes: set[int] | None = None,

        only_keys: set[tuple[int, int]] | None = None,

        resume_continue: bool = False,

    ) -> None:

        if self._thread is not None:

            QMessageBox.information(self, "提示", "当前已有任务在执行，请稍后重试。")

            return

        self._run_done = 0

        self._run_failed = 0

        self._interrupted = False

        self._progress_phase = "running"
        self._contact_step_counts = {}
        self._current_contact = ""
        self._current_sequence_index = -1

        self._run_total = max(0, len(plan.contacts) * len(plan.sequence))

        if isinstance(plan, BroadcastPlan):

            self._last_plan = plan

        else:

            try:

                self._last_plan = BroadcastPlan.model_validate(plan)

            except Exception:

                self._last_plan = None

        if run_uuid:

            self._last_plan_run_uuid = run_uuid

        if not run_uuid:

            self._run_uuid = ""
            self._unit_state.clear()
            from datetime import datetime as _dt

            started = _dt.now()
            self._live_started_date = started.date()
            self._live_clock = started.strftime("%H:%M:%S")

            self._table.setRowCount(0)

            self._current_title.setText("当前运行计划：准备中")

        self._btn_start.setEnabled(False)

        self._btn_interrupt.setEnabled(True)

        self._btn_continue.setEnabled(False)

        self._btn_retry_failed.setEnabled(False)

        self._set_message_progress(0, self._run_total, phase="running")

        self._update_counts()



        self._stop_event = threading.Event()

        self._thread = QThread(self)

        report_store = self._active_report_store()

        self._worker = _RunnerWorker(

            plan=plan,

            run_uuid=run_uuid,

            only_failed=only_failed,

            only_step_indexes=only_step_indexes,

            only_keys=only_keys,

            resume_continue=resume_continue,

            account_id=self._account_id,

            dry_run=False,

            report_store=report_store,

            run_service_factory=self._run_service_factory,

            stop_event=self._stop_event,

        )

        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)

        self._worker.started.connect(self._on_run_started)

        self._worker.progress.connect(self._on_progress)

        self._worker.finished.connect(self._on_finished)

        self._worker.failed.connect(self._on_failed)

        self._worker.finished.connect(self._thread.quit)

        self._worker.failed.connect(self._thread.quit)

        self._thread.finished.connect(self._cleanup_worker)

        self._thread.start()



    def _on_run_started(self, run_uuid: str, total_steps: int) -> None:

        self._run_uuid = run_uuid

        self._last_plan_run_uuid = run_uuid

        self._run_total = max(total_steps, self._run_total)

        self._current_title.setText(f"当前运行计划：{run_uuid}")

        self._update_counts()

        self._persist_last_run("running")
        self._sync_board()



    def _on_progress(self, done: int, total: int, record_obj: object) -> None:

        if self._interrupted:
            self._remember_unit_state(record_obj)
            self._sync_board()
            return

        self._run_done = done

        self._run_total = max(1, total)

        status = str(getattr(record_obj, "status", "")).strip()

        if status in {"failed", "resolve_failed", "blocked"}:

            self._run_failed += 1

        step_index = int(getattr(record_obj, "step_index", -1))

        self._remember_progress_contact(record_obj)

        self._append_record_row(

            contact=str(getattr(record_obj, "contact", "")),

            sequence_index=int(getattr(record_obj, "sequence_index", -1)),

            step_type=str(getattr(record_obj, "step_type", "")),

            status=status,

            ts=datetime.now().isoformat(timespec="seconds"),

            error=str(getattr(record_obj, "error", "")),

            step_index=step_index,

        )

        self._remember_unit_state(record_obj)
        self._set_message_progress(done, total, phase="running")

        self._update_counts()
        self._sync_board()



    def _on_finished(self, summary: RunSummary) -> None:

        self._last_summary = summary

        self._btn_start.setEnabled(True)

        self._btn_interrupt.setEnabled(False)

        failed_count = summary.counts().get("failed", 0) + summary.counts().get("resolve_failed", 0)

        self._btn_retry_failed.setEnabled(failed_count > 0 and bool(self._run_uuid))

        if self._interrupted or self._run_done < self._run_total:

            self._btn_continue.setEnabled(bool(self._run_uuid and self._last_plan))

            if self._interrupted:
                self._current_title.setText(f"已中断：{self._run_uuid}")
            self._set_message_progress(self._run_done, self._run_total, phase="interrupted")

            self._progress_phase = "interrupted"
            self._persist_last_run("interrupted")

        else:

            self._btn_continue.setEnabled(False)

            self._set_message_progress(self._run_total, self._run_total, phase="done")

            self._progress_phase = "done"
            self._persist_last_run("completed")

        self._update_counts()
        self._refresh_history(select_run_uuid=summary.run_uuid)
        self._sync_board()
        self._consume_queued_actions()



    def _on_failed(self, err: str) -> None:

        self._btn_start.setEnabled(True)

        self._btn_interrupt.setEnabled(False)

        self._persist_last_run("failed")

        if "Frida 会话不可用" in err:

            err = f"{err}\n\n建议：\n1) 确认 MuMu 与企微仍在运行\n2) 检查 frida-server 是否在线\n3) 稍后点击“开始执行”继续"

        logger.error("执行失败: {}", err)
        QMessageBox.critical(self, "执行失败", "执行过程中断，请检查模拟器和企微状态后重试。")



    def _cleanup_worker(self) -> None:

        if self._worker is not None:

            self._worker.deleteLater()

        if self._thread is not None:

            self._thread.deleteLater()

        self._worker = None

        self._thread = None

        self._stop_event = None
        self._sync_board()
        self._consume_queued_actions()



    def _refresh_history(self, *, select_run_uuid: str = "") -> None:

        store = self._active_report_store()

        reports = store.list_reports() if store is not None else []

        self._history.blockSignals(True)

        self._history.clear()

        for meta in reports:

            stamp = meta.updated_at.strftime("%Y-%m-%d %H:%M:%S")
            detail = self._history_progress_label(store, meta.run_uuid)
            item = QListWidgetItem(f"{stamp} · {detail}")
            item.setToolTip(meta.run_uuid)
            item.setData(Qt.ItemDataRole.UserRole, meta.run_uuid)

            self._history.addItem(item)

        self._history.blockSignals(False)

        if select_run_uuid:

            self._select_history_item(select_run_uuid)



    def _select_history_item(self, run_uuid: str) -> None:

        for i in range(self._history.count()):

            item = self._history.item(i)

            if str(item.data(Qt.ItemDataRole.UserRole)) == run_uuid:

                self._history.setCurrentRow(i)

                self._load_report(run_uuid)

                return



    def _on_history_selected(self) -> None:

        item = self._history.currentItem()

        if item is None:

            return

        run_uuid = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

        if run_uuid:

            self._load_report(run_uuid)

    def _on_history_context_menu(self, pos) -> None:

        idx = self._history.indexAt(pos)

        if not idx.isValid():

            return

        clicked = self._history.item(idx.row())

        if clicked is None:

            return

        if not clicked.isSelected():

            self._history.clearSelection()

            clicked.setSelected(True)

            self._history.setCurrentItem(clicked)

        run_uuids = self._selected_history_run_uuids()

        if not run_uuids:

            return

        menu = QMenu(self)

        add_copy_action(menu, self._history)

        act_delete = menu.addAction(f"删除选中执行结果（{len(run_uuids)} 条）")

        chosen = menu.exec(self._history.viewport().mapToGlobal(pos))

        if chosen != act_delete:

            return

        self._delete_selected_history(run_uuids)

    def _selected_history_run_uuids(self) -> list[str]:

        out: list[str] = []

        seen: set[str] = set()

        for item in self._history.selectedItems():

            run_uuid = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

            if run_uuid and run_uuid not in seen:

                seen.add(run_uuid)

                out.append(run_uuid)

        return out

    def _delete_selected_history(self, run_uuids: list[str]) -> None:

        if self._thread is not None:

            QMessageBox.information(self, "提示", "任务执行中，暂不支持删除执行结果。")

            return

        if not run_uuids:

            return

        if (

            QMessageBox.question(

                self,

                "确认删除",

                f"将删除 {len(run_uuids)} 条执行结果及关联导出的 CSV 文件，操作不可撤销。是否继续？",

            )

            != QMessageBox.StandardButton.Yes

        ):

            return

        store = self._active_report_store()

        if store is None:

            QMessageBox.warning(self, "删除失败", "报告存储尚未初始化。")

            return

        try:

            store.delete_reports(run_uuids)

        except Exception as exc:

            logger.warning("删除执行结果失败 run_uuids={} err={}", run_uuids, exc)
            QMessageBox.warning(self, "删除失败", "删除执行结果失败，请稍后重试。")

            return

        deleted_set = set(run_uuids)

        if self._run_uuid in deleted_set:

            self._run_uuid = ""

            self._run_total = 0

            self._run_done = 0

            self._run_failed = 0

            self._last_summary = None

            self._set_message_progress(0, 0, phase="idle")

            self._current_title.setText("当前运行计划：无")

            self._progress_phase = "idle"
            self._contact_step_counts = {}
            self._current_contact = ""
            self._current_sequence_index = -1
            self._current_step_index = -1
            self._counts.setText(self._progress_summary())

            self._btn_continue.setEnabled(False)

            self._btn_retry_failed.setEnabled(False)

            self._table.setRowCount(0)

        self._refresh_history()

        if self._history.count() > 0:

            self._history.setCurrentRow(0)



    def _load_report(self, run_uuid: str) -> None:

        store = self._active_report_store()

        if store is None:

            QMessageBox.warning(self, "加载报告失败", "报告存储尚未初始化。")

            return

        try:

            view = store.load_report(run_uuid)

        except Exception as exc:

            logger.warning("加载报告失败 run_uuid={} err={}", run_uuid, exc)
            QMessageBox.warning(self, "加载报告失败", "报告读取失败，请稍后重试。")

            return

        self._run_uuid = run_uuid

        self._current_title.setText(f"当前运行计划：{run_uuid}")

        self._table.setRowCount(0)

        reduced = self._reduce_rows(view.rows)

        for row in reduced:

            self._append_record_row(

                contact=str(row.get("contact", "")),

                sequence_index=int(row.get("sequence_index", -1)),

                step_type=str(row.get("type", "")),

                status=str(row.get("status", "")),

                ts=str(row.get("ts", "")),

                error=str(row.get("error", "")),

                step_index=int(row.get("step_index", -1)),

            )

        self._restore_plan_from_report(view.rows, run_uuid=run_uuid)
        if self._progress_phase != "running":
            self._show_saved_progress(view.rows)

        self._btn_retry_failed.setEnabled(

            self._last_plan is not None

            and self._last_plan_run_uuid == self._run_uuid

            and any(

                str(x.get("status", "")) in {"failed", "resolve_failed", "blocked"}

                for x in reduced

            )

        )

        snap = LastRunStore(account_id=self._account_id).load()

        self._btn_continue.setEnabled(

            bool(

                self._last_plan

                and self._run_uuid

                and snap is not None

                and snap.run_uuid == self._run_uuid

                and snap.status in {"interrupted", "running"}

            )

        )



    def _restore_plan_from_report(self, rows: list[dict[str, Any]], *, run_uuid: str) -> None:

        raw_plan = BroadcastRunner.extract_plan_from_report(rows)

        if raw_plan is not None:

            try:

                self._last_plan = BroadcastPlan.model_validate(raw_plan)

                self._last_plan_run_uuid = run_uuid

                return

            except Exception:

                pass

        snap = LastRunStore(account_id=self._account_id).load()

        if snap is not None and snap.run_uuid == run_uuid:

            try:

                self._last_plan = BroadcastPlan.model_validate(snap.plan)

                self._last_plan_run_uuid = run_uuid

            except Exception:

                self._last_plan = None



    def _restore_last_run_context(self) -> None:

        snap = LastRunStore(account_id=self._account_id).load()

        if snap is None:
            self._live_started_date = None
            self._live_clock = ""
            self._unit_state = {}

            if self._history.count() > 0:

                self._history.setCurrentRow(0)

            return

        try:

            self._last_plan = BroadcastPlan.model_validate(snap.plan)

            self._last_plan_run_uuid = snap.run_uuid

        except Exception:

            self._last_plan = None

        self._select_history_item(snap.run_uuid)
        self._restore_live_marker_from_report(
            snap.run_uuid,
            resumable=snap.status in {"running", "interrupted"},
        )

    def _restore_live_marker_from_report(self, run_uuid: str, *, resumable: bool) -> None:
        self._live_started_date = None
        self._live_clock = ""
        self._unit_state = {}
        if not resumable:
            return
        store = self._active_report_store()
        if store is None:
            return
        try:
            from datetime import datetime as _dt

            from app.broadcast.send_units import latest_unit_status

            view = store.load_report(run_uuid)
            for row in view.rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("type", "")).strip() != "plan_meta":
                    continue
                started_text = str(row.get("started_at", "")).strip()
                if not started_text:
                    break
                started = _dt.fromisoformat(started_text)
                self._live_started_date = started.date()
                self._live_clock = started.strftime("%H:%M:%S")
                break
            if self._live_started_date is None:
                for row in view.rows:
                    if not isinstance(row, dict):
                        continue
                    ts_text = str(row.get("ts", "")).strip()
                    if not ts_text:
                        continue
                    try:
                        started = _dt.fromisoformat(ts_text)
                    except ValueError:
                        continue
                    self._live_started_date = started.date()
                    self._live_clock = started.strftime("%H:%M:%S")
                    break
            latest = latest_unit_status(view.rows)
            self._unit_state = {
                key: {"status": status, "phase": ""}
                for key, status in latest.items()
            }
        except Exception:
            self._live_started_date = None
            self._live_clock = ""
            self._unit_state = {}



    def _persist_last_run(self, status: str) -> None:

        if self._last_plan is None or not self._run_uuid:

            return

        LastRunStore(account_id=self._account_id).save(

            run_uuid=self._run_uuid,

            plan=self._last_plan.model_dump(mode="json"),

            status=status,

        )



    def _resolve_plan_for_retry(self) -> BroadcastPlan | None:

        if self._last_plan is not None and self._last_plan_run_uuid == self._run_uuid:

            return self._last_plan

        store = self._active_report_store()

        if store is None or not self._run_uuid:

            return None

        try:

            view = store.load_report(self._run_uuid)

        except Exception:

            return None

        raw_plan = BroadcastRunner.extract_plan_from_report(view.rows)

        if raw_plan is None:

            return None

        try:

            plan = BroadcastPlan.model_validate(raw_plan)

            self._last_plan = plan

            self._last_plan_run_uuid = self._run_uuid

            return plan

        except Exception:

            return None



    def _reduce_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:

        latest_by_step: dict[int, dict[str, Any]] = {}

        for row in rows:

            if not isinstance(row, dict):

                continue

            if str(row.get("type", "")).strip() == "plan_meta":

                continue

            try:

                idx = int(row.get("step_index", -1))

            except Exception:

                continue

            if idx < 0:

                continue

            latest_by_step[idx] = dict(row)

        return [latest_by_step[k] for k in sorted(latest_by_step.keys())]



    def _append_record_row(

        self,

        *,

        contact: str,

        sequence_index: int,

        step_type: str,

        status: str,

        ts: str,

        error: str,

        step_index: int,

    ) -> None:

        row = self._table.rowCount()

        self._table.insertRow(row)

        items = [

            QTableWidgetItem(contact),

            QTableWidgetItem(str(sequence_index + 1 if sequence_index >= 0 else step_index + 1)),

            QTableWidgetItem(step_type),

            QTableWidgetItem(self._status_text(status)),

            QTableWidgetItem(ts),

            QTableWidgetItem(error),

        ]

        for col, item in enumerate(items):

            self._table.setItem(row, col, item)

        self._attach_retry_button(row, step_index, status)



    def _attach_retry_button(self, row: int, step_index: int, status: str) -> None:

        btn = QPushButton("重试")

        btn.setEnabled(step_index >= 0 and status in {"failed", "resolve_failed", "blocked"})

        btn.clicked.connect(lambda _checked=False, idx=step_index: self._retry_single_step(idx))

        self._table.setCellWidget(row, 6, btn)



    def _retry_single_step(self, step_index: int) -> None:

        if step_index < 0:

            return

        plan = self._resolve_plan_for_retry()

        if plan is None:

            QMessageBox.information(self, "提示", "无法重试：缺少计划快照。")

            return

        run_uuid = self._run_uuid if self._last_plan_run_uuid == self._run_uuid else ""

        self._start_worker(

            plan=plan,

            run_uuid=run_uuid,

            only_failed=False,

            only_step_indexes={step_index},

        )



    def _status_text(self, status: str) -> str:

        if status == "sent":

            return "✅ 已发送"

        if status in {"before_send", "retry_scheduled"}:

            return "⏳ 执行中"

        if status in {"failed", "resolve_failed", "blocked"}:

            return "❌ 失败"

        return f"⏳ {status}"



    def _update_counts(self) -> None:

        self._counts.setText(self._progress_summary())

    def _set_message_progress(self, done: int, total: int, *, phase: str) -> None:
        total_n = max(0, int(total))
        done_n = max(0, int(done))
        if total_n <= 0:
            self._progress.setRange(0, 1)
            self._progress.setValue(0)
            self._progress.setFormat("尚未开始")
            return
        done_n = min(done_n, total_n)
        self._progress.setRange(0, total_n)
        self._progress.setValue(done_n)
        if phase == "interrupted":
            self._progress.setFormat("已中断 %v/%m 条")
        elif phase == "done":
            self._progress.setFormat("已完成 %v/%m 条")
        elif phase == "running":
            self._progress.setFormat("已完成 %v/%m 条")
        else:
            self._progress.setFormat("已记录 %v/%m 条")

    def _remember_progress_contact(self, record_obj: object) -> None:
        contact = str(getattr(record_obj, "contact", "")).strip() or "未知联系人"
        sequence_index = int(getattr(record_obj, "sequence_index", -1))
        self._current_contact = contact
        self._current_sequence_index = sequence_index
        self._current_step_index = int(getattr(record_obj, "step_index", -1))
        self._contact_step_counts[contact] = self._contact_step_counts.get(contact, 0) + 1

    def _progress_summary(self) -> str:
        sent = max(0, self._run_done - self._run_failed)
        remaining_msgs = max(0, self._run_total - self._run_done)
        seq_len = len(self._last_plan.sequence) if self._last_plan is not None else 0
        contact_total = len(self._last_plan.contacts) if self._last_plan is not None else 0
        completed_contacts = 0
        if seq_len > 0:
            completed_contacts = sum(1 for count in self._contact_step_counts.values() if count >= seq_len)
        contact_pos = 0
        if seq_len > 0 and self._current_step_index >= 0:
            contact_pos = self._current_step_index // seq_len + 1
        lines = [
            f"消息：已处理 {self._run_done}/{self._run_total} 条 · 成功 {sent} · 失败 {self._run_failed} · 还剩 {remaining_msgs}",
            f"联系人：已处理完 {completed_contacts}/{contact_total} 人",
        ]
        if self._progress_phase == "running" and self._current_contact:
            index = self._current_sequence_index + 1 if self._current_sequence_index >= 0 else 1
            total = seq_len or max(index, 1)
            who = f"第 {contact_pos}/{contact_total} 人" if contact_pos and contact_total else "当前联系人"
            lines.append(f"当前：{who}「{self._current_contact}」的第 {index}/{total} 条（本条已结束）")
        elif self._progress_phase == "running":
            lines.append("当前：准备发送第 1 条")
        elif self._progress_phase == "done":
            lines.append("当前：计划里的消息已全部处理")
        elif self._progress_phase == "interrupted":
            lines.append("当前：已中断，停在上面这条已结束的消息")
        else:
            lines.append("当前：尚未开始")
        return "\n".join(lines)



    def _active_report_store(self) -> ReportStore | None:

        if self._report_store is not None:

            return self._report_store

        if self._report_store_provider is None:

            self._report_store = ReportStore(account_id=self._account_id)

        else:

            self._report_store = self._report_store_provider(self._account_id)

        return self._report_store

    # ------------------------------------------------------------------
    # 待执行计划
    # ------------------------------------------------------------------
    def _remember_unit_state(self, record_obj: object) -> None:
        step_index = int(getattr(record_obj, "step_index", -1))
        inner_index = int(getattr(record_obj, "inner_index", -1))
        if step_index < 0:
            return
        phase = str(getattr(record_obj, "phase", "") or "")
        status = str(getattr(record_obj, "status", "") or "")
        if phase in {"prepare", "sending"}:
            self._unit_state[(step_index, inner_index)] = {"phase": phase, "status": "before_send"}
            return
        self._unit_state[(step_index, inner_index)] = {"phase": "", "status": status}

    def _confirm_send(self, text: str) -> bool:
        return (
            QMessageBox.question(self, "确认", text)
            == QMessageBox.StandardButton.Yes
        )

    def _selected_day_matches_live(self) -> bool:
        if not self._live_clock or self._last_plan is None:
            return False
        chosen = self._board.selected_date()
        started = getattr(self, "_live_started_date", None)
        return started == chosen

    def _on_board_stop(self) -> None:
        if self._thread is not None:
            if not self._confirm_send("停止后将停在当前还没发出去的那一条。是否停止？"):
                return
            self.request_interrupt()
            return
        # 按钮已经写成「接续开始」，这里直接继续，不再做一次会被静默吃掉的日期判断。
        self.continue_run()

    def _on_board_restart(self) -> None:
        candidates = self._resend_candidates_for_selected_day()
        if not candidates:
            QMessageBox.information(self, "提示", "当前日期没有可重新发送的任务。")
            return
        labels = [label for label, _plan in candidates]
        chosen, ok = QInputDialog.getItem(self, "重新发送任务", "请选择要重新发送的任务：", labels, 0, False)
        if not ok or not chosen:
            return
        selected = next((plan for label, plan in candidates if label == chosen), None)
        if selected is None:
            return
        self._queued_retry_keys = None
        if self._thread is not None:
            self._restart_plan_after_stop = selected
            self.request_interrupt()
            QMessageBox.information(self, "已排队", "当前这轮发送结束后，将重新发送所选任务。")
            return
        self._unit_state.clear()
        self._start_worker(plan=selected, run_uuid="", only_failed=False)

    def _resend_candidates_for_selected_day(self) -> list[tuple[str, BroadcastPlan]]:
        chosen_day = self._board.selected_date()
        store = self._active_report_store()
        if store is None:
            return []
        rows: list[tuple[datetime, str, BroadcastPlan]] = []
        for meta in store.list_reports():
            try:
                view = store.load_report(meta.run_uuid)
            except Exception:
                continue
            plan = self._plan_from_dict(BroadcastRunner.extract_plan_from_report(view.rows))
            if plan is None:
                continue
            started = meta.updated_at
            for row in view.rows:
                if isinstance(row, dict) and str(row.get("type", "")) == "plan_meta":
                    try:
                        started = datetime.fromisoformat(str(row.get("started_at", "")))
                    except ValueError:
                        pass
                    break
            if started.date() != chosen_day:
                continue
            title = self._display_plan_title(plan, mode="immediate", when=started)
            label = f"{started.strftime('%H:%M:%S')} · {title}"
            rows.append((started, label, plan))
        rows.sort(key=lambda item: item[0], reverse=True)
        seen: dict[str, int] = {}
        result: list[tuple[str, BroadcastPlan]] = []
        for _started, label, plan in rows:
            seen[label] = seen.get(label, 0) + 1
            shown = label if seen[label] == 1 else f"{label}（{seen[label]}）"
            result.append((shown, plan))
        return result
    def _on_board_retry_failed(self) -> None:
        if self._thread is None:
            return
        if not self._confirm_send("重新发送将在本轮消息发送结束后开始"):
            return
        self._queued_retry_keys = {
            key
            for key, state in self._unit_state.items()
            if str(state.get("status", "")) in {"failed", "resolve_failed", "blocked"}
        }
        QMessageBox.information(self, "已排队", "重新发送将在本轮消息发送结束后开始")

    def _on_board_row_action(self, action: str, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        step_index = int(payload.get("step_index", -1))
        inner_index = int(payload.get("inner_index", -1))
        if action == "resume":
            target = str(payload.get("run_uuid", "") or "")
            if self._thread is not None:
                self._resume_requested = target
                return
            self.continue_run(target)
            return
        if action != "retry" or step_index < 0:
            return
        if not self._confirm_send("只重新发送这一条失败消息。是否继续？"):
            return
        key = (step_index, inner_index)
        if self._thread is not None:
            queued = set(self._queued_retry_keys or set())
            queued.add(key)
            self._queued_retry_keys = queued
            QMessageBox.information(self, "已排队", "重新发送将在本轮消息发送结束后开始")
            return
        self._start_one_keys({key})

    def _consume_queued_actions(self) -> None:
        if self._thread is not None:
            return
        if self._resume_requested is not None:
            target = self._resume_requested
            self._resume_requested = None
            self.continue_run(target)
            return
        restart_plan = self._restart_plan_after_stop
        if restart_plan is not None:
            self._restart_plan_after_stop = None
            self._restart_after_stop = False
            self._unit_state.clear()
            self._start_worker(plan=restart_plan, run_uuid="", only_failed=False)
            return
        if self._restart_after_stop and self._last_plan is not None:
            self._restart_after_stop = False
            self._unit_state.clear()
            self._start_worker(plan=self._last_plan, run_uuid="", only_failed=False)
            return
        keys = self._queued_retry_keys
        self._queued_retry_keys = None
        if not keys or self._last_plan is None:
            return
        self._start_one_keys(keys)
    def _start_one_keys(self, keys: set[tuple[int, int]]) -> None:
        if not keys or self._last_plan is None:
            return
        self._start_worker(
            plan=self._last_plan,
            run_uuid=self._run_uuid,
            only_failed=False,
            only_keys=set(keys),
        )

    def _on_board_node(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("kind", ""))
        if kind in {"pending", "cron"}:
            entry_id = str(payload.get("entry_id", "")).strip()
            if entry_id:
                self._open_pending_detail(entry_id)
            return
        if kind != "message":
            return
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        step = preview.get("step") if isinstance(preview.get("step"), dict) else {}
        if not isinstance(step, dict) or not step:
            step = {}
        if not str(step.get("type", "")).strip():
            leaf_type = str(payload.get("leaf_type") or payload.get("step_type") or "").strip()
            if leaf_type:
                step = dict(step)
                step["type"] = leaf_type
        if not step:
            QMessageBox.information(self, "素材详情", "这条消息暂时不能预览。")
            return
        from gui.widgets.pending_plan_detail import open_message_preview

        try:
            open_message_preview(
                self,
                step,
                account_id=self._account_id,
                preview_service_provider=self._preview_service_provider,
                name=str(preview.get("name", "")),
            )
        except Exception as exc:
            logger.warning("打开素材预览失败: {}", exc)
            QMessageBox.information(self, "素材详情", "这条消息暂时不能预览。")

    @staticmethod
    def _board_payload_from_item(item) -> dict[str, Any]:
        cursor = item
        while cursor is not None:
            payload = cursor.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(payload, dict) and str(payload.get("entry_id", "")).strip():
                return dict(payload)
            cursor = cursor.parent()
        return {}

    def _board_asset_name(self, kind: str, item_id: str, path: str) -> str:
        from app.broadcast.asset_preview_source import lookup_asset_name

        return lookup_asset_name(self._account_id, kind, item_id=item_id, path=path)

    def _on_board_executing_context_menu(self, pos) -> None:
        tree = self._board.executing
        item = tree.itemAt(pos)
        if item is None:
            return
        plan_item = item
        while plan_item.parent() is not None:
            plan_item = plan_item.parent()
        payload = plan_item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict) or str(payload.get("kind", "")) != "plan":
            return
        if not plan_item.isSelected():
            tree.clearSelection()
            plan_item.setSelected(True)
        selected = self._selected_executing_plans()
        if not selected:
            return
        menu = QMenu(self)
        add_copy_action(menu, tree)
        act_rename = menu.addAction("重命名")
        delete_label = f"删除（{len(selected)} 个）" if len(selected) > 1 else "删除"
        act_delete = menu.addAction(delete_label)
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))
        if chosen == act_rename:
            clicked = next((row for row in selected if row[0] is plan_item), None)
            if clicked is not None:
                self._rename_executing_plan(clicked[1], clicked[2])
        elif chosen == act_delete:
            self._delete_executing_plans(selected)

    def _selected_executing_plans(self) -> list[tuple[object, str, str]]:
        """选中的计划：(树节点, run_uuid, 当前显示名)。同一计划只记一次。"""
        tree = self._board.executing
        seen: set[int] = set()
        out: list[tuple[object, str, str]] = []
        for item in tree.selectedItems():
            plan_item = item
            while plan_item.parent() is not None:
                plan_item = plan_item.parent()
            marker = id(plan_item)
            if marker in seen:
                continue
            payload = plan_item.data(0, Qt.ItemDataRole.UserRole)
            if not isinstance(payload, dict) or str(payload.get("kind", "")) != "plan":
                continue
            seen.add(marker)
            run_uuid = str(payload.get("run_uuid", "")).strip()
            current_name = str(payload.get("plan_name", "")).strip()
            out.append((plan_item, run_uuid, current_name))
        return out

    def _rename_executing_plan(self, run_uuid: str, current_name: str) -> None:
        new_name, ok = QInputDialog.getText(self, "重命名计划", "计划名称：", text=current_name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == current_name:
            return
        store = self._active_report_store()
        saved = False
        if store is not None and run_uuid:
            try:
                saved = store.rename_plan(run_uuid, new_name)
            except Exception as exc:
                logger.warning("重命名计划失败 run_uuid={} err={}", run_uuid, exc)
                QMessageBox.warning(self, "改名失败", "计划名称没有保存，请稍后重试。")
                return
        if self._last_plan is not None and (not run_uuid or self._run_uuid == run_uuid):
            self._last_plan = self._last_plan.model_copy(update={"name": new_name})
            saved = True
        if not saved:
            QMessageBox.information(self, "提示", "这条计划记录里没有可以改的名称。")
            return
        self._sync_board()

    def _delete_executing_plans(self, selected: list[tuple[object, str, str]]) -> None:
        live = self._run_uuid if self._thread is not None else ""
        targets = [run_uuid for _item, run_uuid, _name in selected if run_uuid and run_uuid != live]
        blocked = [run_uuid for _item, run_uuid, _name in selected if run_uuid and run_uuid == live]
        if not targets and blocked:
            QMessageBox.information(self, "提示", "正在发送的任务不能删除，请先停止。")
            return
        if not targets:
            QMessageBox.information(self, "提示", "没有可以删除的计划记录。")
            return
        extra = "正在发送的那一条会保留。" if blocked else "删除后不能恢复。"
        if (
            QMessageBox.question(self, "确认删除", f"删除选中的 {len(targets)} 个计划？{extra}")
            != QMessageBox.StandardButton.Yes
        ):
            return
        store = self._active_report_store()
        if store is None:
            QMessageBox.warning(self, "删除失败", "报告存储尚未初始化。")
            return
        try:
            store.delete_reports(targets)
        except Exception as exc:
            logger.warning("删除计划失败 run_uuids={} err={}", targets, exc)
            QMessageBox.warning(self, "删除失败", "删除计划失败，请稍后重试。")
            return
        if self._thread is None and self._run_uuid in set(targets):
            self._run_uuid = ""
            self._last_plan = None
            self._unit_state.clear()
        self._sync_board()

    def _on_board_pending_context_menu(self, pos) -> None:
        tree = self._board.pending
        item = tree.itemAt(pos)
        if item is None:
            return
        payload = self._board_payload_from_item(item)
        if str(payload.get("kind", "")) != "pending":
            return
        entry_id = str(payload.get("entry_id", "")).strip()
        if not entry_id:
            return
        menu = QMenu(self)
        add_copy_action(menu, tree)
        act_detail = menu.addAction("查看计划详情")
        act_delete = menu.addAction("删除")
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_detail:
            self._open_pending_detail(entry_id)
        elif chosen == act_delete:
            self._delete_pending_plan(entry_id)

    def _on_board_cron_context_menu(self, pos) -> None:
        tree = self._board.cron_box
        item = tree.itemAt(pos)
        if item is None:
            return
        payload = self._board_payload_from_item(item)
        if str(payload.get("kind", "")) != "cron":
            return
        entry_id = str(payload.get("entry_id", "")).strip()
        if not entry_id:
            return
        menu = QMenu(self)
        add_copy_action(menu, tree)
        act_detail = menu.addAction("查看计划详情")
        act_delete = menu.addAction("删除")
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_detail:
            self._open_pending_detail(entry_id)
        elif chosen == act_delete:
            self._delete_pending_plan(entry_id)

    def _sync_board(self) -> None:
        if not hasattr(self, "_board"):
            return
        from datetime import datetime as dt

        from app.broadcast.pending_plans import PendingPlanStore

        from app.broadcast.send_units import latest_unit_status

        chosen = self._board.selected_date()
        executing = []
        live_uuid = self._run_uuid if self._last_plan is not None else ""
        report_store = self._active_report_store()
        if report_store is not None:
            for meta in report_store.list_reports():
                if meta.run_uuid == live_uuid and getattr(self, "_live_started_date", None) == chosen:
                    continue
                try:
                    view = report_store.load_report(meta.run_uuid)
                except Exception:
                    continue
                raw_plan = BroadcastRunner.extract_plan_from_report(view.rows)
                plan = self._plan_from_dict(raw_plan)
                if plan is None:
                    continue
                started_text = ""
                for row in view.rows:
                    if isinstance(row, dict) and str(row.get("type", "")) == "plan_meta":
                        started_text = str(row.get("started_at", "")).strip()
                        break
                if not started_text:
                    started_text = meta.updated_at.isoformat(timespec="seconds")
                try:
                    started = dt.fromisoformat(started_text)
                except ValueError:
                    continue
                if started.date() != chosen:
                    continue
                states = {
                    key: {"status": status, "phase": ""}
                    for key, status in latest_unit_status(view.rows).items()
                }
                executing.append(
                    build_plan_node(
                        plan,
                        title=self._display_plan_title(
                            plan,
                            mode=str(plan.schedule_mode or "immediate"),
                            label=str(plan.schedule_label or ""),
                            when=started,
                        ),
                        clock=started.strftime("%H:%M:%S"),
                        states=states,
                        load_bundle=self._load_bundle,
                        show_actions=True,
                        name_of=self._board_asset_name,
                    )
                )
                self._attach_run_uuid(executing[-1], meta.run_uuid)
                executing[-1].payload.update({"sort_at": started.isoformat(), "run_uuid": meta.run_uuid})
        if self._last_plan is not None and getattr(self, "_live_started_date", None) == chosen:
            clock = self._live_clock or ""
            live_when = datetime.combine(chosen, datetime.strptime(self._live_clock or "00:00:00", "%H:%M:%S").time())
            executing.append(
                build_plan_node(
                    self._last_plan,
                    title=self._display_plan_title(
                        self._last_plan,
                        mode=str(self._last_plan.schedule_mode or "immediate"),
                        label=str(self._last_plan.schedule_label or ""),
                        when=live_when,
                    ),
                    clock=clock,
                    states=self._unit_state,
                    load_bundle=self._load_bundle,
                    show_actions=True,
                    name_of=self._board_asset_name,
                )
            )
            self._attach_run_uuid(executing[-1], self._run_uuid)
            executing[-1].payload.update({
                "sort_at": live_when.isoformat(),
                "run_uuid": self._run_uuid,
                "live": True,
            })
        pending_nodes = []
        cron_nodes = []
        try:
            store = PendingPlanStore(account_id=self._account_id)
        except Exception:
            store = None
        if store is not None:
            for entry in store.list():
                try:
                    when = dt.fromisoformat(entry.trigger_at)
                except ValueError:
                    continue
                plan = self._plan_from_dict(entry.plan)
                if plan is None:
                    continue
                title = self._display_plan_title(
                    plan,
                    mode=entry.mode,
                    label=entry.label,
                    when=when,
                    created_at=entry.created_at,
                )
                if entry.mode == "cron":
                    from gui.widgets.send_board import BoardNode

                    cron_nodes.append(BoardNode(title=title, kind="cron", payload={"entry_id": entry.id, "kind": "cron"}))
                    continue
                if when.date() != chosen:
                    continue
                node = build_plan_node(
                    plan,
                    title=title,
                    clock=when.strftime("%H:%M:%S"),
                    load_bundle=self._load_bundle,
                    show_actions=False,
                    name_of=self._board_asset_name,
                )
                node.payload = {
                    "entry_id": entry.id,
                    "kind": "pending",
                    "sort_at": str(entry.created_at or entry.trigger_at),
                }
                pending_nodes.append(node)
        executing.sort(key=self._recent_queue_key, reverse=True)
        pending_nodes.sort(key=self._recent_queue_key, reverse=True)
        running = self._thread is not None
        paused = (not running) and any(str(item.get("status", "")) == "paused" for item in self._unit_state.values())
        can_retry = any(
            str(item.get("status", "")) in {"failed", "resolve_failed", "blocked"}
            for item in self._unit_state.values()
        )
        same_day = getattr(self, "_live_started_date", None) == chosen
        self._board.set_running(
            running and same_day,
            paused=paused and same_day,
            can_retry=can_retry and running and same_day,
            can_restart=bool(executing),
        )
        self._board.set_lists(executing=executing, pending=pending_nodes, cron=cron_nodes)

    @staticmethod
    def _recent_queue_key(node) -> tuple[int, float]:
        """最近进入队列的排在上面。正在跑的那一轮再靠前。"""
        raw = str(node.payload.get("sort_at", "") or "").strip()
        try:
            stamp = datetime.fromisoformat(raw).timestamp()
        except ValueError:
            stamp = 0.0
        live_rank = 1 if node.payload.get("live") else 0
        return (live_rank, stamp)

    def _report_can_resume(self, run_uuid: str) -> bool:
        from app.broadcast.send_units import expand_send_units, select_units

        rows: list[dict[str, Any]] = []
        plan = self._last_plan if self._last_plan is not None and self._run_uuid == run_uuid else None
        store = self._active_report_store()
        if store is not None and run_uuid:
            try:
                view = store.load_report(run_uuid)
            except Exception:
                view = None
            if view is not None:
                rows = [row for row in view.rows if isinstance(row, dict)]
                if plan is None:
                    plan = self._plan_from_dict(BroadcastRunner.extract_plan_from_report(rows))
        if plan is None:
            snap = LastRunStore(account_id=self._account_id).load()
            if snap is not None and snap.run_uuid == run_uuid:
                plan = self._plan_from_dict(snap.plan)
        if plan is None:
            return False
        units = expand_send_units(plan, load_bundle=self._load_bundle)
        return bool(select_units(units, rows))

    @staticmethod
    def _attach_run_uuid(node, run_uuid: str) -> None:
        node.payload["run_uuid"] = str(run_uuid or "")
        for child in node.children:
            RunMonitorPanel._attach_run_uuid(child, run_uuid)

    @staticmethod
    def _display_plan_title(
        plan: BroadcastPlan,
        *,
        mode: str,
        label: str = "",
        when: datetime | None = None,
        created_at: str = "",
    ) -> str:
        explicit = str(plan.name or "").strip()
        if explicit and explicit not in {"未命名计划", "未命名"} and not explicit.startswith("（"):
            return explicit
        if not mode:
            mode = str(plan.schedule_mode or "")
        if not label:
            label = str(plan.schedule_label or "")
        if explicit.startswith("（"):
            return explicit
        stamp = when.strftime("%Y-%m-%d %H:%M:%S") if when is not None else ""
        if mode == "cron":
            return f"（循环任务）{label or stamp or '循环'}"
        if mode == "after":
            delay = label.strip()
            if not delay and when is not None and created_at:
                try:
                    seconds = max(0, int((when - datetime.fromisoformat(created_at)).total_seconds()))
                    if seconds and seconds % 3600 == 0:
                        delay = f"{seconds // 3600}小时后"
                    elif seconds and seconds % 60 == 0:
                        delay = f"{seconds // 60}分钟后"
                    elif seconds:
                        delay = f"{seconds}秒后"
                except ValueError:
                    pass
            return f"（{delay or '延时执行'}）{stamp}".rstrip()
        if mode == "at":
            return f"（具体时刻）{stamp or label}"
        return f"（立即执行）{stamp}".rstrip()
    def _plan_from_dict(self, raw: object):
        if not isinstance(raw, dict):
            return None
        try:
            return BroadcastPlan.model_validate(raw)
        except Exception:
            return None

    def _load_bundle(self, bundle_id: str):
        from app.broadcast.asset_bundles import get_bundle

        return get_bundle(bundle_id, account_id=self._account_id)

    def refresh_pending_plans(self) -> None:
        from app.broadcast.pending_plans import PendingPlanStore
        store = PendingPlanStore(account_id=self._account_id)
        entries = store.list()

        self._pending_list.clear()
        for entry in entries:
            item = QListWidgetItem(self._pending_item_text(entry))
            item.setToolTip("计划还没开始，这里只有预计人数和消息条数，没有执行进度")
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            self._pending_list.addItem(item)
        self._sync_board()

    def _on_pending_context_menu(self, pos) -> None:
        item = self._pending_list.itemAt(pos)
        if item is None:
            return
        self._pending_list.setCurrentItem(item)
        entry_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not entry_id:
            return
        menu = QMenu(self)
        add_copy_action(menu, self._pending_list)
        act_detail = menu.addAction("查看计划详情")
        act_delete = menu.addAction("删除")
        chosen = menu.exec(self._pending_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_detail:
            self._open_pending_detail(entry_id)
        elif chosen == act_delete:
            self._delete_pending_plan(entry_id)

    def _toggle_pending_detail(self, item: QListWidgetItem) -> None:
        entry_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if entry_id:
            self._open_pending_detail(entry_id)

    def _open_pending_detail(self, entry_id: str) -> None:
        from app.broadcast.pending_plans import PendingPlanStore
        from gui.widgets.pending_plan_detail import PendingPlanDetailDialog

        store = PendingPlanStore(account_id=self._account_id)
        entry = store.get(entry_id)
        if entry is None:
            QMessageBox.information(self, "提示", "该计划已不存在。")
            self.refresh_pending_plans()
            return
        dialog = PendingPlanDetailDialog(
            entry,
            self,
            preview_service_provider=self._preview_service_provider,
            account_id=self._account_id,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.refresh_pending_plans()
            return
        if dialog.trigger_changed:
            self.pendingTimerReschedule.emit(entry_id)
        self.refresh_pending_plans()

    def _describe_pending_plan(self, entry) -> str:
        from app.broadcast.asset_bundles import get_bundle
        from app.broadcast.library import VoiceLibrary

        lib = VoiceLibrary.load(account_id=self._account_id)
        plan = entry.plan if isinstance(entry.plan, dict) else {}
        contacts = plan.get("contacts", [])
        sequence = plan.get("sequence", [])
        gaps = plan.get("step_gaps", [])
        names = [
            str(row.get("display_name", "")).strip()
            for row in contacts
            if isinstance(row, dict) and str(row.get("display_name", "")).strip()
        ]
        lines: list[str] = []
        if entry.mode == "cron" and entry.label.strip():
            lines.append(entry.label.strip())
        if not names:
            lines.append("发给：（无）")
        elif len(names) == 1:
            lines.append(f"发给 {names[0]}")
        else:
            lines.append(f"发给 {len(names)} 人：{'、'.join(names)}")
        lines.append(self._compact_sequence(sequence, gaps, lib, get_bundle))
        if len(names) > 1:
            lines.append(f"换人间隔 {self._short_gap(plan.get('contact_gap', 1))}")
        return "\n".join(lines)

    def _compact_sequence(self, sequence, gaps, lib, get_bundle) -> str:
        if not isinstance(sequence, list) or not sequence:
            return "（没有素材）"
        steps = [step for step in sequence if isinstance(step, dict)]
        if not steps:
            return "（没有素材）"
        parts: list[str] = []
        for index, step in enumerate(steps):
            parts.append(self._compact_step(step, lib, get_bundle))
            if index < len(steps) - 1:
                gap = gaps[index] if index < len(gaps) else 3
                parts.append(self._short_gap(gap))
        return " → ".join(parts)

    def _compact_step(self, step: dict, lib, get_bundle) -> str:
        from gui.i18n import asset_type_to_zh

        step_type = str(step.get("type", "")).strip()
        if step_type == "bundle":
            return self._compact_bundle(step, lib, get_bundle)
        if step_type in {"text", "at", "notice"}:
            template_id = str(step.get("template_id", "")).strip()
            shown = str(step.get("display_name") or "").strip()
            if shown == template_id:
                shown = ""
            if not shown and template_id:
                looked = self._lookup_asset_name("text", template_id, lib)
                if looked and looked != template_id:
                    shown = looked
            if not shown:
                shown = str(step.get("body", "")).strip() or "未命名"
            label = "文字" if step_type == "text" else asset_type_to_zh(step_type)
            return f"{label} {shown}"
        if step_type in {"image", "video"}:
            shown = self._lookup_media_name_by_path(step_type, str(step.get("path") or "")) or "未命名"
            return f"{asset_type_to_zh(step_type)} {shown}"
        label = asset_type_to_zh(step_type)
        item_id = str(step.get("voice_id") or step.get("template_id") or "").strip()
        explicit = str(step.get("display_name") or "").strip()
        if explicit == item_id:
            explicit = ""
        looked = self._lookup_asset_name(step_type, item_id, lib) if item_id else ""
        if looked == item_id:
            looked = ""
        return f"{label} {explicit or looked or '未命名'}"

    def _compact_bundle(self, step: dict, lib, get_bundle) -> str:
        from gui.i18n import asset_type_to_zh

        bundle_id = str(step.get("bundle_id", "")).strip()
        bundle = get_bundle(bundle_id, account_id=self._account_id) if bundle_id else None
        title = bundle.name if bundle is not None else str(step.get("bundle_name") or "").strip()
        if not title or title == bundle_id:
            title = "未命名素材包"
        if bundle is None:
            return f"素材包 {title}（已不存在）"
        inner: list[str] = []
        for index, item in enumerate(bundle.items):
            name = self._lookup_asset_name(item.asset_type, item.item_id, lib)
            if not name or name == item.item_id:
                name = "未命名"
            inner.append(f"{asset_type_to_zh(item.asset_type)} {name}")
            if index < len(bundle.items) - 1:
                gap = bundle.step_gaps[index] if index < len(bundle.step_gaps) else 3
                inner.append(self._short_gap(gap))
        body = " → ".join(inner) if inner else "空"
        return f"素材包 {title}：{body}"

    @staticmethod
    def _short_gap(value) -> str:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = 0
        if seconds >= 3600 and abs(seconds % 3600) < 1e-6:
            return f"{int(seconds // 3600)}时"
        if seconds >= 60 and abs(seconds % 60) < 1e-6:
            return f"{int(seconds // 60)}分"
        if seconds.is_integer():
            return f"{int(seconds)}秒"
        return f"{seconds:g}秒"

    def _pending_item_text(self, entry) -> str:
        from app.broadcast.pending_plans import pending_list_text

        plan = entry.plan if isinstance(entry.plan, dict) else {}
        contacts = plan.get("contacts") if isinstance(plan.get("contacts"), list) else []
        sequence = plan.get("sequence") if isinstance(plan.get("sequence"), list) else []
        when = pending_list_text(entry)
        return f"未开始 · {len(contacts)}人 · {len(sequence)}条消息 · {when}"

    def _history_progress_label(self, store: ReportStore | None, run_uuid: str) -> str:
        if store is None:
            return run_uuid
        try:
            view = store.load_report(run_uuid)
        except Exception:
            return run_uuid
        sent, failed, done, total = self._count_report_messages(view.rows)
        return f"已处理 {done}/{total} 条 · 成功 {sent} · 失败 {failed}"

    def _show_saved_progress(self, rows: list[dict[str, Any]]) -> None:
        sent, failed, done, total = self._count_report_messages(rows)
        self._set_message_progress(done, total, phase="history")
        remain = max(0, total - done)
        self._counts.setText(
            f"消息：已处理 {done}/{total} 条 · 成功 {sent} · 失败 {failed} · 还剩 {remain}"
        )

    def _count_report_messages(self, rows: list[dict[str, Any]]) -> tuple[int, int, int, int]:
        reduced = self._reduce_rows(rows)
        sent = 0
        failed = 0
        for row in reduced:
            status = str(row.get("status", "")).strip()
            if status == "sent":
                sent += 1
            elif status in {"failed", "resolve_failed", "blocked"}:
                failed += 1
        done = sent + failed
        total = done
        raw = BroadcastRunner.extract_plan_from_report(rows)
        if isinstance(raw, dict):
            contacts = raw.get("contacts") if isinstance(raw.get("contacts"), list) else []
            sequence = raw.get("sequence") if isinstance(raw.get("sequence"), list) else []
            planned = len(contacts) * len(sequence)
            if planned > 0:
                total = max(planned, done)
        return sent, failed, done, total

    def _describe_step_lines(self, step: dict, number: int, lib, get_bundle) -> list[str]:
        from gui.i18n import asset_type_to_zh

        step_type = str(step.get("type", "")).strip()
        if step_type in {"text", "at", "notice"}:
            template_id = str(step.get("template_id", "")).strip()
            shown = str(step.get("display_name") or "").strip()
            if shown == template_id:
                shown = ""
            if not shown and template_id:
                looked = self._lookup_asset_name("text", template_id, lib)
                if looked and looked != template_id:
                    shown = looked
            if not shown:
                shown = str(step.get("body", "")).strip() or "未命名"
            label = "文字" if step_type == "text" else asset_type_to_zh(step_type)
            return [f"{number}. {label}「{shown}」"]
        if step_type == "bundle":
            bundle_id = str(step.get("bundle_id", "")).strip()
            bundle = get_bundle(bundle_id, account_id=self._account_id) if bundle_id else None
            title = bundle.name if bundle is not None else str(step.get("bundle_name") or "").strip()
            if not title or title == bundle_id:
                title = "未命名素材包"
            lines = [f"{number}. 素材包「{title}」，按下面顺序连续发送："]
            if bundle is None:
                lines.append("    （素材包已不存在）")
                return lines
            for inner_index, item in enumerate(bundle.items):
                label = asset_type_to_zh(item.asset_type)
                name = self._lookup_asset_name(item.asset_type, item.item_id, lib)
                if not name or name == item.item_id:
                    name = "未命名"
                lines.append(f"    {inner_index + 1}) {label}「{name}」")
                if inner_index < len(bundle.items) - 1:
                    gap = bundle.step_gaps[inner_index] if inner_index < len(bundle.step_gaps) else 3
                    lines.append(f"        然后等待 {self._gap_text(gap)}")
            return lines
        label = asset_type_to_zh(step_type)
        if step_type in {"image", "video"}:
            shown = self._lookup_media_name_by_path(step_type, str(step.get("path") or ""))
            return [f"{number}. {label}「{shown or '未命名'}」"]
        item_id = str(step.get("voice_id") or step.get("template_id") or "").strip()
        explicit = str(step.get("display_name") or "").strip()
        if explicit == item_id:
            explicit = ""
        looked = self._lookup_asset_name(step_type, item_id, lib) if item_id else ""
        if looked == item_id:
            looked = ""
        shown = explicit or looked or "未命名"
        return [f"{number}. {label}「{shown}」"]

    def _lookup_asset_name(self, asset_type: str, item_id: str, lib) -> str:
        kind = str(asset_type).strip().lower()
        item_id = str(item_id).strip()
        if not item_id:
            return ""
        if kind in {"text", "link"}:
            try:
                template = lib.get_text_template(item_id)
                return str(template.name or item_id)
            except Exception:
                if kind != "link":
                    return item_id
                try:
                    from app.broadcast.wxa_templates import load_template as load_wxa

                    data = load_wxa(item_id, account_id=self._account_id)
                    return str(data.get("name") or data.get("title") or item_id)
                except Exception:
                    return item_id
        if kind == "voice":
            for row in lib.list():
                if row.id == item_id:
                    return str(row.note or row.label or row.file or item_id)
            return item_id
        if kind in {"oa", "doc", "channel"}:
            try:
                from app.broadcast.oa_templates import load_template as load_oa

                data = load_oa(item_id, account_id=self._account_id)
                return str(data.get("name") or data.get("title") or item_id)
            except Exception:
                return item_id
        if kind == "wxa":
            try:
                from app.broadcast.wxa_templates import load_template as load_wxa

                data = load_wxa(item_id, account_id=self._account_id)
                return str(data.get("name") or data.get("title") or item_id)
            except Exception:
                return item_id
        if kind == "image":
            try:
                from app.broadcast.image_templates import load_template as load_image

                data = load_image(item_id, account_id=self._account_id)
                return str(data.get("name") or item_id)
            except Exception:
                return item_id
        if kind == "video":
            try:
                from app.broadcast.video_templates import load_template as load_video

                data = load_video(item_id, account_id=self._account_id)
                return str(data.get("name") or item_id)
            except Exception:
                return item_id
        return item_id

    def _lookup_media_name_by_path(self, kind: str, path: str) -> str:
        target = str(path or "").strip()
        if not target:
            return ""
        if kind == "image":
            from app.broadcast.image_templates import list_templates, load_template
        else:
            from app.broadcast.video_templates import list_templates, load_template
        try:
            files = list_templates(self._account_id)
        except Exception:
            return ""
        for fp in files:
            try:
                data = load_template(str(fp), account_id=self._account_id)
            except Exception:
                continue
            stored = str(data.get("path") or data.get("video_path") or "").strip()
            if stored != target:
                continue
            if data.get("unnamed"):
                return ""
            name = str(data.get("name") or "").strip()
            tid = str(data.get("template_id") or "").strip()
            if name and name != tid:
                return name
            return ""
        return ""

    @staticmethod
    def _gap_text(value) -> str:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = 0
        if seconds.is_integer():
            return f"{int(seconds)} 秒"
        return f"{seconds:g} 秒"

    def _delete_pending_plan(self, entry_id: str) -> None:
        from app.broadcast.pending_plans import PendingPlanStore
        store = PendingPlanStore(account_id=self._account_id)
        entry = store.get(entry_id)
        schedule_id = entry.schedule_id if entry is not None and entry.mode == "cron" else ""
        store.remove(entry_id)
        self.pendingTimerCancel.emit(entry_id)
        if schedule_id:
            self.pendingCronCancel.emit(schedule_id)
        self.refresh_pending_plans()
