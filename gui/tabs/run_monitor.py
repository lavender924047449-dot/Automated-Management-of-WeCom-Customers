from __future__ import annotations



import threading

import uuid

from datetime import datetime

from typing import Any, Callable

from loguru import logger



from PySide6.QtCore import QObject, QThread, Qt, Signal

from PySide6.QtWidgets import (

    QAbstractItemView,

    QFileDialog,

    QHBoxLayout,

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

                    progress_cb=self._on_progress,

                    dry_run=self.dry_run,

                    run_uuid=run_uuid,

                    stop_event=self.stop_event,

                )

            self.finished.emit(summary)

        except Exception as exc:

            self.failed.emit(str(exc))



    def _on_progress(self, record, done: int, total: int) -> None:

        self.progress.emit(done, total, record)



    def _create_run_service(self) -> RunService:

        if self.run_service_factory is not None:

            return self.run_service_factory(self.account_id, self.report_store)

        return RunService(account_id=self.account_id, report_store=self.report_store)





class RunMonitorPanel(QWidget):

    """Step 6: 执行结果页（进度、历史、重试、导出）。"""



    def __init__(

        self,

        *,

        plan_provider: Callable[[], object],

        report_store_provider: Callable[[str], ReportStore] | None = None,

    ) -> None:

        super().__init__()

        self._plan_provider = plan_provider

        self._report_store_provider = report_store_provider

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

        self._build_ui()

        self._bind_events()



    def _build_ui(self) -> None:

        root = QVBoxLayout(self)



        card = QWidget()

        card_layout = QVBoxLayout(card)

        self._current_title = QLabel("当前运行 Plan：无")

        self._progress = QProgressBar()

        self._progress.setRange(0, 100)

        self._progress.setValue(0)

        self._counts = QLabel("已发 0 / 待发 0 / 失败 0")

        row = QHBoxLayout()

        self._btn_start = QPushButton("开始执行")

        self._btn_interrupt = QPushButton("执行中断")

        self._btn_continue = QPushButton("继续未完成")

        self._btn_retry_failed = QPushButton("重试失败项")

        self._btn_export_csv = QPushButton("导出 CSV")

        self._btn_interrupt.setEnabled(False)

        self._btn_continue.setEnabled(False)

        self._btn_retry_failed.setEnabled(False)

        self._btn_export_csv.setEnabled(False)

        row.addWidget(self._btn_start)

        row.addWidget(self._btn_interrupt)

        row.addWidget(self._btn_continue)

        row.addWidget(self._btn_retry_failed)

        row.addWidget(self._btn_export_csv)

        row.addStretch(1)

        card_layout.addWidget(self._current_title)

        card_layout.addWidget(self._progress)

        card_layout.addWidget(self._counts)

        card_layout.addLayout(row)

        root.addWidget(card)



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

        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

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

        self._btn_export_csv.clicked.connect(self.export_csv)

        self._history.itemSelectionChanged.connect(self._on_history_selected)
        self._history.customContextMenuRequested.connect(self._on_history_context_menu)



    def set_account(self, account_id: str) -> None:

        self._account_id = account_id.strip() or "default"

        self._report_store = None

        self.reset_for_account()



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

        self._progress.setValue(0)

        self._current_title.setText("当前运行 Plan：无")

        self._counts.setText("已发 0 / 待发 0 / 失败 0")

        self._btn_interrupt.setEnabled(False)

        self._btn_continue.setEnabled(False)

        self._btn_retry_failed.setEnabled(False)

        self._btn_export_csv.setEnabled(False)

        self._table.setRowCount(0)

        self._refresh_history()

        self._restore_last_run_context()



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



    def start_partial_run(self, plan: object, step_indexes: list[int]) -> None:

        indexes = {int(x) for x in step_indexes if int(x) >= 0}

        if not indexes:

            return

        run_uuid = self._run_uuid if self._last_plan_run_uuid == self._run_uuid else ""

        self._start_worker(

            plan=plan,

            run_uuid=run_uuid,

            only_failed=False,

            only_step_indexes=indexes,

        )



    def request_interrupt(self) -> None:

        if self._stop_event is None or self._thread is None:

            return

        self._stop_event.set()

        self._btn_interrupt.setEnabled(False)

        self._interrupted = True



    def continue_run(self) -> None:

        plan = self._resolve_plan_for_retry()

        if plan is None or not self._run_uuid:

            QMessageBox.information(self, "提示", "没有可继续的执行上下文。")

            return

        self._start_worker(

            plan=plan,

            run_uuid=self._run_uuid,

            resume_continue=True,

        )



    def retry_failed(self) -> None:

        plan = self._resolve_plan_for_retry()

        if plan is None or not self._run_uuid:

            QMessageBox.information(self, "提示", "无法重试：缺少 Plan 快照，请重新执行一次。")

            return

        self._start_worker(plan=plan, run_uuid=self._run_uuid, only_failed=True)



    def _start_worker(

        self,

        *,

        plan: object,

        run_uuid: str,

        only_failed: bool,

        only_step_indexes: set[int] | None = None,

        resume_continue: bool = False,

    ) -> None:

        if self._thread is not None:

            QMessageBox.information(self, "提示", "当前已有任务在执行，请稍后重试。")

            return

        self._run_done = 0

        self._run_failed = 0

        self._interrupted = False

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

            self._table.setRowCount(0)

            self._current_title.setText("当前运行 Plan：准备中")

        self._btn_start.setEnabled(False)

        self._btn_interrupt.setEnabled(True)

        self._btn_continue.setEnabled(False)

        self._btn_retry_failed.setEnabled(False)

        self._btn_export_csv.setEnabled(False)

        self._progress.setValue(0)

        self._update_counts()



        self._stop_event = threading.Event()

        self._thread = QThread(self)

        report_store = self._active_report_store()

        self._worker = _RunnerWorker(

            plan=plan,

            run_uuid=run_uuid,

            only_failed=only_failed,

            only_step_indexes=only_step_indexes,

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

        self._current_title.setText(f"当前运行 Plan：{run_uuid}")

        self._update_counts()

        self._persist_last_run("running")



    def _on_progress(self, done: int, total: int, record_obj: object) -> None:

        self._run_done = done

        self._run_total = max(1, total)

        status = str(getattr(record_obj, "status", "")).strip()

        if status in {"failed", "resolve_failed", "blocked"}:

            self._run_failed += 1

        step_index = int(getattr(record_obj, "step_index", -1))

        self._append_record_row(

            contact=str(getattr(record_obj, "contact", "")),

            sequence_index=int(getattr(record_obj, "sequence_index", -1)),

            step_type=str(getattr(record_obj, "step_type", "")),

            status=status,

            ts=datetime.now().isoformat(timespec="seconds"),

            error=str(getattr(record_obj, "error", "")),

            step_index=step_index,

        )

        pct = int(done * 100 / max(1, total))

        self._progress.setValue(pct)

        self._update_counts()



    def _on_finished(self, summary: RunSummary) -> None:

        self._last_summary = summary

        self._btn_start.setEnabled(True)

        self._btn_interrupt.setEnabled(False)

        self._btn_export_csv.setEnabled(bool(self._run_uuid))

        failed_count = summary.counts().get("failed", 0) + summary.counts().get("resolve_failed", 0)

        self._btn_retry_failed.setEnabled(failed_count > 0 and bool(self._run_uuid))

        if self._interrupted or self._run_done < self._run_total:

            self._btn_continue.setEnabled(bool(self._run_uuid and self._last_plan))

            self._progress.setValue(int(self._run_done * 100 / max(1, self._run_total)))

            self._persist_last_run("interrupted")

        else:

            self._btn_continue.setEnabled(False)

            self._progress.setValue(100)

            self._persist_last_run("completed")

        self._refresh_history(select_run_uuid=summary.run_uuid)



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



    def export_csv(self) -> None:

        store = self._active_report_store()

        if store is None:

            QMessageBox.information(self, "提示", "报告存储尚未初始化。")

            return

        if not self._run_uuid:

            QMessageBox.information(self, "提示", "暂无可导出的运行记录。")

            return

        default_name = f"plan_{self._run_uuid}.csv"

        path_text, _ = QFileDialog.getSaveFileName(self, "导出 CSV", default_name, "CSV 文件 (*.csv)")

        try:

            if path_text:

                out = store.export_csv(self._run_uuid, out_path=path_text)

            else:

                out = store.export_csv(self._run_uuid)

        except Exception as exc:

            logger.error("导出 CSV 失败 run_uuid={} err={}", self._run_uuid, exc)
            QMessageBox.critical(self, "导出失败", "导出失败，请确认目标文件未被占用后重试。")

            return

        logger.info("导出 CSV 成功 run_uuid={} out={}", self._run_uuid, out)
        QMessageBox.information(self, "导出成功", "导出完成，请到您选择的文件夹查看。")



    def _refresh_history(self, *, select_run_uuid: str = "") -> None:

        store = self._active_report_store()

        reports = store.list_reports() if store is not None else []

        self._history.blockSignals(True)

        self._history.clear()

        for meta in reports:

            text = f"{meta.updated_at.strftime('%Y-%m-%d %H:%M:%S')} · {meta.run_uuid}"

            item = QListWidgetItem(text)

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

            self._progress.setValue(0)

            self._current_title.setText("当前运行 Plan：无")

            self._counts.setText("已发 0 / 待发 0 / 失败 0")

            self._btn_export_csv.setEnabled(False)

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

        self._current_title.setText(f"当前运行 Plan：{run_uuid}")

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

        self._btn_export_csv.setEnabled(True)

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

            if self._history.count() > 0:

                self._history.setCurrentRow(0)

            return

        try:

            self._last_plan = BroadcastPlan.model_validate(snap.plan)

            self._last_plan_run_uuid = snap.run_uuid

        except Exception:

            self._last_plan = None

        self._select_history_item(snap.run_uuid)



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

            QMessageBox.information(self, "提示", "无法重试：缺少 Plan 快照。")

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

        sent = max(0, self._run_done - self._run_failed)

        pending = max(0, self._run_total - self._run_done)

        self._counts.setText(f"已发 {sent} / 待发 {pending} / 失败 {self._run_failed}")



    def _active_report_store(self) -> ReportStore | None:

        if self._report_store is not None:

            return self._report_store

        if self._report_store_provider is None:

            self._report_store = ReportStore(account_id=self._account_id)

        else:

            self._report_store = self._report_store_provider(self._account_id)

        return self._report_store


