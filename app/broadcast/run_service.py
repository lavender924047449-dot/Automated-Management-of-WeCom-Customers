"""Step 4: 执行 facade（Runner 装配下沉到业务层）。"""

from __future__ import annotations

import threading
import uuid
from typing import Callable

from app.broadcast.account import resolve_account_id
from app.broadcast.plan import BroadcastPlan
from app.broadcast.report_store import ReportStore
from app.broadcast.runner import BroadcastRunner, RunRecord, RunSummary
from app.broadcast.send_clients import DryRunClient
from runtime.wework_re_android.conv_resolver import ConversationResolver

ProgressCallback = Callable[[RunRecord, int, int], None]


class RunService:
    def __init__(self, *, account_id: str, report_store: ReportStore | None = None) -> None:
        self.account_id = resolve_account_id(account_id)
        self.report_store = report_store or ReportStore(account_id=self.account_id)

    def start(
        self,
        plan: BroadcastPlan,
        *,
        resume_uuid: str = "",
        only_failed: bool = False,
        only_step_indexes: set[int] | None = None,
        progress_cb: ProgressCallback | None = None,
        dry_run: bool = False,
        run_uuid: str = "",
        stop_event: threading.Event | None = None,
    ) -> RunSummary:
        runner = self._build_runner(
            plan=plan,
            progress_cb=progress_cb,
            dry_run=dry_run,
            stop_event=stop_event,
        )
        if resume_uuid:
            runner.run_uuid = resume_uuid
            if only_step_indexes is not None:
                return runner._run_from(start_step_index=0, only_step_indexes=only_step_indexes)
            return runner.resume(resume_uuid, only_failed=only_failed)
        runner.run_uuid = run_uuid.strip() or str(uuid.uuid4())
        if only_step_indexes is not None:
            return runner._run_from(start_step_index=0, only_step_indexes=only_step_indexes)
        return runner.run()

    def _build_runner(
        self,
        *,
        plan: BroadcastPlan,
        progress_cb: ProgressCallback | None,
        dry_run: bool,
        stop_event: threading.Event | None = None,
    ) -> BroadcastRunner:
        resolver = ConversationResolver(account_id=self.account_id).resolve
        send_client = DryRunClient() if dry_run else None
        return BroadcastRunner(
            plan=plan,
            resolver=resolver,
            send_client=send_client,
            progress_cb=progress_cb,
            account_id=self.account_id,
            report_store=self.report_store,
            stop_event=stop_event,
        )


__all__ = ["RunService", "ProgressCallback"]
