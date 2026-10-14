"""Step 4.3: BroadcastRunner 串行执行器。"""

from __future__ import annotations

import random
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Protocol

from app.broadcast.account import resolve_account_id
from app.broadcast.asset_bundles import get_bundle
from app.broadcast.dto import PlanContact
from app.broadcast.image_templates import load_template as load_image_template
from app.broadcast.library import TextTemplate, VoiceLibrary, VoiceLibraryEntry
from app.broadcast.oa_templates import load_template as load_oa_template
from app.broadcast.paths import VOICE_LIB_ROOT
from app.broadcast.wxa_templates import has_card_fields, load_template as load_wxa_template
from app.broadcast.plan import (
    AtStep,
    BundleStep,
    BroadcastPlan,
    ChannelStep,
    DocStep,
    ImageStep,
    NoticeStep,
    OaStep,
    SequenceStep,
    TextStep,
    VideoStep,
    VoiceStep,
    WxaStep,
    step_status,
)
from app.broadcast.report_store import ReportStore
from app.broadcast.send_units import SendUnit, expand_send_units, select_units
from app.broadcast.video_templates import load_template as load_video_template

RunnerStatus = Literal[
    "sent",
    "failed",
    "blocked",
    "resolve_failed",
    "paused",
    "before_send",
]


class SendClient(Protocol):
    """发送客户端抽象，默认实现走 send_engine 子进程。"""

    def send_voice(self, *, conv_local: str, silk_path: str, voice_time: int) -> bool:
        ...

    def send_text(self, *, conv_local: str, body: str) -> bool:
        ...

    def send_at(self, *, conv_local: str, body: str) -> bool:
        ...

    def send_notice(self, *, conv_local: str, body: str) -> bool:
        ...

    def send_image(self, *, conv_local: str, image_path: str) -> bool:
        ...

    def send_oa(self, *, conv_local: str, template: str) -> bool:
        ...

    def send_wxa(self, *, conv_local: str, template: str) -> bool:
        ...

    def send_video(
        self,
        *,
        conv_local: str,
        video_path: str,
        thumb_path: str = "",
        duration: int | None = None,
    ) -> bool:
        ...

    def ensure_ready(self) -> bool:
        ...


class SubprocessSendClient:
    """默认发送客户端：调用 `runtime.wework_re_android.send_engine`。"""

    # 生产发送应回到历史验收路径：每一步直接启动 send_engine 子进程，
    # 由 send_engine 自己 attach/run_script/detach。runner 层的常驻
    # ensure_ready 预检查会提前 attach 并可能失败或占用会话，导致真正
    # 的 send_engine 还没运行就被挡住。
    requires_preflight = False

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        module: str | None = None,
        account_id: str | None = None,
        adb_serial: str = "127.0.0.1:16384",
        frida_remote: str = "127.0.0.1:27042",
        process_name: str = "com.tencent.wework",
        stop_event: threading.Event | None = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.module = module or "runtime.wework_re_android.send_engine"
        self.account_id = resolve_account_id(account_id)
        self.adb_serial = adb_serial
        self.frida_remote = frida_remote
        self.process_name = process_name
        self._frida_session = None
        self.last_error = ""
        self.stop_event = stop_event
        self._active_proc: subprocess.Popen | None = None

    def kill_active_process(self) -> None:
        """立即杀掉正在执行的子进程。"""
        proc = self._active_proc
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass

    def _run(self, args: list[str], expected: str) -> bool:
        if bool(getattr(sys, "frozen", False)):
            cmd = [
                self.python_executable,
                "__send_engine__",
                "--account-id",
                self.account_id,
                *args,
            ]
        else:
            cmd = [
                self.python_executable,
                "-m",
                self.module,
                "--account-id",
                self.account_id,
                *args,
            ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._active_proc = proc
        try:
            # 轮询等待子进程结束，同时检查 stop_event
            while proc.poll() is None:
                time.sleep(0.1)
        finally:
            self._active_proc = None
        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        out = f"{stdout}\n{stderr}".strip()
        if proc.returncode != 0:
            detail = out or f"exit code {proc.returncode}"
            self.last_error = detail[:500]
            return False
        if expected not in out:
            self.last_error = out[:500] or f"未找到期望输出: {expected}"
            return False
        self.last_error = ""
        return True

    def send_voice(self, *, conv_local: str, silk_path: str, voice_time: int) -> bool:
        return self._run(
            ["voice", "--conv-local", conv_local, "--silk-path", silk_path, "--voice-time", str(voice_time)],
            "action=voice return=true",
        )

    def send_text(self, *, conv_local: str, body: str) -> bool:
        # 必须用 --body=VALUE：正文以 "-" / "--" 开头时，分开传参会被 argparse 当成新 flag。
        return self._run(
            ["text", "--conv-local", conv_local, f"--body={body}"],
            "action=text return=true",
        )

    def send_at(self, *, conv_local: str, body: str) -> bool:
        return self._run(
            ["at", "--conv-local", conv_local, f"--body={body}"],
            "action=at return=true",
        )

    def send_notice(self, *, conv_local: str, body: str) -> bool:
        return self._run(
            ["notice", "--conv-local", conv_local, f"--body={body}"],
            "action=notice return=true",
        )

    def send_image(self, *, conv_local: str, image_path: str) -> bool:
        return self._run(
            ["image", "--conv-local", conv_local, "--image-path", image_path],
            "action=image return=true",
        )

    def send_oa(self, *, conv_local: str, template: str) -> bool:
        return self._run(
            ["oa", "--conv-local", conv_local, "--template", template],
            "action=oa return=true",
        )

    def send_wxa(self, *, conv_local: str, template: str) -> bool:
        return self._run(
            ["--wait-seconds", "8", "wxa", "--conv-local", conv_local, "--template", template],
            "action=wxa return=true",
        )

    def send_video(
        self,
        *,
        conv_local: str,
        video_path: str,
        thumb_path: str = "",
        duration: int | None = None,
    ) -> bool:
        args = ["video", "--conv-local", conv_local, "--video-path", video_path]
        if thumb_path:
            args.extend(["--thumb-path", thumb_path])
        if duration is not None:
            args.extend(["--duration", str(duration)])
        return self._run(args, "action=video return=true")

    def ensure_ready(self) -> bool:
        if self._frida_session is None:
            from runtime.wework_re_android.frida_session import FridaSession
            from runtime.wework_re_android.send_engine import find_pid

            self._frida_session = FridaSession(
                frida_remote=self.frida_remote,
                process_name=self.process_name,
                pid_provider=find_pid,
            )
        sess = self._frida_session
        if sess.session is not None and sess.heartbeat():
            return True
        return sess.reconnect(adb_serial=self.adb_serial, retries=3)


@dataclass
class RunRecord:
    contact: str
    conv_local: str | None
    step_index: int
    sequence_index: int
    step_type: str
    status: RunnerStatus
    error: str = ""
    variant_used: str = ""
    inner_index: int = -1
    phase: str = ""


class RunCancelled(Exception):
    """用户请求 graceful 中断。"""


@dataclass
class DispatchResult:
    ok: bool
    variant_used: str = ""
    error: str = ""


@dataclass
class RunSummary:
    run_uuid: str
    records: list[RunRecord] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        stat: dict[str, int] = {}
        for row in self.records:
            stat[row.status] = stat.get(row.status, 0) + 1
        return stat


class BroadcastRunner:
    """广播执行器：contacts × sequence 串行发送。"""

    def __init__(
        self,
        *,
        plan: BroadcastPlan,
        resolver: Callable[[str], str],
        send_client: SendClient | None = None,
        voice_index_path: str | Path | None = None,
        rand_fn: Callable[[float, float], float] = random.uniform,
        sleep_fn: Callable[[float], None] = time.sleep,
        progress_cb: Callable[[RunRecord, int, int], None] | None = None,
        account_id: str | None = None,
        run_uuid: str | None = None,
        report_store: ReportStore | None = None,
        report_gc_days: int = 90,
        retry_delays_seconds: tuple[float, ...] = (),
        stop_event: threading.Event | None = None,
    ) -> None:
        self.plan = plan
        self.resolver = resolver
        self.account_id = resolve_account_id(account_id)
        self.send_client = send_client or SubprocessSendClient(account_id=self.account_id, stop_event=stop_event)
        self.rand_fn = rand_fn
        self.sleep_fn = sleep_fn
        self.progress_cb = progress_cb
        self.retry_delays_seconds = tuple(float(x) for x in retry_delays_seconds)
        self.stop_event = stop_event
        self.report_store = report_store or ReportStore(account_id=self.account_id)
        self.report_store.gc(days=max(1, int(report_gc_days)))
        self.run_uuid = str(run_uuid or uuid.uuid4())
        self.voice_lib = VoiceLibrary.load(voice_index_path, account_id=self.account_id)
        self._voice_map: dict[str, VoiceLibraryEntry] = {
            row.id: row for row in self.voice_lib.list_active()
        }
        self._text_template_map: dict[str, TextTemplate] = {
            row.id: row for row in self.voice_lib.list_text_templates()
        }

    def run(self) -> RunSummary:
        return self._run_from(start_step_index=0)

    def resume(self, run_uuid: str, *, only_failed: bool = False) -> RunSummary:
        text = str(run_uuid).strip()
        if not text:
            raise ValueError("run_uuid 不能为空")
        self.run_uuid = text
        return self._run_from(start_step_index=0, only_failed=only_failed)

    def _run_from(
        self,
        *,
        start_step_index: int,
        only_step_indexes: set[int] | None = None,
        only_failed: bool = False,
        only_keys: set[tuple[int, int]] | None = None,
    ) -> RunSummary:
        units = expand_send_units(self.plan, load_bundle=self._load_bundle)
        self._conv_cache = {}
        self._report_inner_index = -1
        prior_rows: list[dict[str, object]] = []
        try:
            prior_rows = list(self.report_store.load_report(self.run_uuid).rows)
        except Exception:
            prior_rows = []
        selected = select_units(
            units,
            prior_rows,
            only_failed=only_failed,
            only_keys=only_keys,
            only_step_indexes=only_step_indexes,
            start_step_index=start_step_index,
        )
        total = len(selected)
        summary = RunSummary(run_uuid=self.run_uuid)
        if total < 1:
            self.report_store.export_csv(self.run_uuid)
            return summary

        self._append_plan_meta()
        done = 0
        failed_bundles: set[int] = set()
        selected_keys = {unit.key for unit in selected}
        for pos, unit in enumerate(selected):
            if self._should_stop():
                self._mark_paused(summary, unit, done=done, total=total)
                break
            conv_local = self._conv_for_unit(unit)
            if conv_local is None:
                done += 1
                record = self._finish_unit(
                    summary,
                    unit,
                    status="resolve_failed",
                    error="contact not found",
                    conv_local="",
                    done=done,
                    total=total,
                )
                self._note_bundle_failure(summary, unit, failed_bundles, done=done, total=total)
                if record.status == "paused":
                    break
                self._sleep_between(unit, selected, pos, selected_keys)
                continue
            self._emit_phase(unit, phase="prepare", done=done, total=total, conv_local=conv_local)
            if self._should_stop():
                self._mark_paused(summary, unit, done=done, total=total)
                break
            self._emit_phase(unit, phase="sending", done=done, total=total, conv_local=conv_local)
            record = self._run_unit(unit, conv_local=conv_local)
            if record.status == "paused" or (self._should_stop() and record.status != "sent"):
                if record.status != "paused":
                    record = self._rewrite_as_paused(unit, conv_local=conv_local)
                self._append_record(summary, record, done=done, total=total)
                break
            done += 1
            self._append_record(summary, record, done=done, total=total)
            if record.status == "failed":
                self._note_bundle_failure(summary, unit, failed_bundles, done=done, total=total)
            if record.status == "sent" and self._should_stop():
                nxt = selected[pos + 1] if pos + 1 < len(selected) else None
                if nxt is not None:
                    self._mark_paused(summary, nxt, done=done, total=total)
                break
            if self._should_stop():
                break
            self._sleep_between(unit, selected, pos, selected_keys)
        self.report_store.export_csv(self.run_uuid)
        return summary

    def _load_bundle(self, bundle_id: str):
        return get_bundle(bundle_id, account_id=self.account_id)

    def _conv_for_unit(self, unit: SendUnit) -> str | None:
        cache = getattr(self, "_conv_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._conv_cache = cache
        if unit.contact_index in cache:
            return cache[unit.contact_index]
        contact = self.plan.contacts[unit.contact_index]
        resolved = self._resolve_contact(contact)
        cache[unit.contact_index] = resolved
        return resolved

    def _unit_record(
        self,
        unit: SendUnit,
        *,
        status: RunnerStatus,
        error: str,
        conv_local: str,
        phase: str = "",
        step_type: str = "",
    ) -> RunRecord:
        return RunRecord(
            contact=unit.contact_name,
            conv_local=conv_local or None,
            step_index=unit.step_index,
            sequence_index=unit.sequence_index,
            step_type=step_type or unit.step_type,
            status=status,
            error=error,
            inner_index=unit.inner_index,
            phase=phase,
        )

    def _emit_phase(
        self,
        unit: SendUnit,
        *,
        phase: str,
        done: int,
        total: int,
        conv_local: str,
    ) -> None:
        record = self._unit_record(
            unit,
            status="before_send",
            error="",
            conv_local=conv_local,
            phase=phase,
        )
        if self.progress_cb is not None:
            self.progress_cb(record, done, total)

    def _finish_unit(
        self,
        summary: RunSummary,
        unit: SendUnit,
        *,
        status: RunnerStatus,
        error: str,
        conv_local: str,
        done: int,
        total: int,
        step_type: str = "",
    ) -> RunRecord:
        self._report_inner_index = unit.inner_index
        self._append_report(
            step_index=unit.step_index,
            sequence_index=unit.sequence_index,
            contact=unit.contact_name,
            conv_local=conv_local,
            step_type=step_type or unit.step_type,
            status=status,
            error=error,
            attempt=0,
        )
        record = self._unit_record(
            unit,
            status=status,
            error=error,
            conv_local=conv_local,
            step_type=step_type,
        )
        self._append_record(summary, record, done=done, total=total)
        return record

    def _mark_paused(self, summary: RunSummary, unit: SendUnit, *, done: int, total: int) -> None:
        self._finish_unit(
            summary,
            unit,
            status="paused",
            error="用户中断",
            conv_local="",
            done=done,
            total=total,
        )

    def _note_bundle_failure(
        self,
        summary: RunSummary,
        unit: SendUnit,
        failed_bundles: set[int],
        *,
        done: int,
        total: int,
    ) -> None:
        if unit.presentation not in {"atomic", "expanded"} or unit.inner_index < 0:
            return
        if unit.step_index in failed_bundles:
            return
        failed_bundles.add(unit.step_index)
        self._report_inner_index = -1
        self._append_report(
            step_index=unit.step_index,
            sequence_index=unit.sequence_index,
            contact=unit.contact_name,
            conv_local="",
            step_type="bundle",
            status="failed",
            error="包内有发送失败",
            attempt=0,
        )
        outer = RunRecord(
            contact=unit.contact_name,
            conv_local=None,
            step_index=unit.step_index,
            sequence_index=unit.sequence_index,
            step_type="bundle",
            status="failed",
            error="包内有发送失败",
            inner_index=-1,
        )
        if self.progress_cb is not None:
            self.progress_cb(outer, done, total)
        summary.records.append(outer)

    def _rewrite_as_paused(self, unit: SendUnit, *, conv_local: str) -> RunRecord:
        self._report_inner_index = unit.inner_index
        self._append_report(
            step_index=unit.step_index,
            sequence_index=unit.sequence_index,
            contact=unit.contact_name,
            conv_local=conv_local,
            step_type=unit.step_type,
            status="paused",
            error="用户中断",
            attempt=1,
        )
        return self._unit_record(unit, status="paused", error="用户中断", conv_local=conv_local)

    def _run_unit(self, unit: SendUnit, *, conv_local: str) -> RunRecord:
        self._report_inner_index = unit.inner_index
        if unit.blocked_reason:
            self._append_report(
                step_index=unit.step_index,
                sequence_index=unit.sequence_index,
                contact=unit.contact_name,
                conv_local=conv_local,
                step_type=unit.step_type,
                status="blocked",
                error=unit.blocked_reason,
                attempt=0,
            )
            return self._unit_record(
                unit,
                status="blocked",
                error=unit.blocked_reason,
                conv_local=conv_local,
            )
        if unit.leaf is not None:
            record = self._run_step(
                contact=unit.contact_name,
                conv_local=conv_local,
                index=unit.sequence_index,
                step_index=unit.step_index,
                step=unit.leaf,
            )
            record.inner_index = unit.inner_index
            return record
        ok = False
        error = "send returned false"
        try:
            ok = self._send_bundle_item(
                conv_local=conv_local,
                asset_type=unit.bundle_asset_type,
                item_id=unit.bundle_item_id,
            )
            if not ok:
                error = str(getattr(self.send_client, "last_error", "") or error)
        except RunCancelled:
            error = "用户中断"
            ok = False
        except Exception as exc:
            error = str(exc)
            ok = False
        status: RunnerStatus = "sent" if ok else ("paused" if error == "用户中断" else "failed")
        self._append_report(
            step_index=unit.step_index,
            sequence_index=unit.sequence_index,
            contact=unit.contact_name,
            conv_local=conv_local,
            step_type=unit.step_type,
            status="sent" if ok else status,
            error="" if ok else error,
            attempt=1,
        )
        return self._unit_record(
            unit,
            status="sent" if ok else status,
            error="" if ok else error,
            conv_local=conv_local,
        )

    def _send_bundle_item(self, *, conv_local: str, asset_type: str, item_id: str) -> bool:
        kind = asset_type.strip().lower()
        if self._should_stop():
            raise RunCancelled()
        if kind == "voice":
            entry = self._voice_map.get(item_id)
            if entry is None:
                raise KeyError(f"素材包引用的 voice 不存在: {item_id}")
            return bool(
                self.send_client.send_voice(
                    conv_local=conv_local,
                    silk_path=self._voice_path(entry.file),
                    voice_time=entry.duration,
                )
            )
        if kind == "text":
            body, _ = self._resolve_text_body(
                TextStep(type="text", template_id=item_id, variant_pick="random", text_kind="text")
            )
            return bool(self.send_client.send_text(conv_local=conv_local, body=body))
        if kind == "link":
            if self._legacy_link_is_wxa(item_id):
                return bool(self._send_wxa(conv_local=conv_local, template_id=item_id))
            body, _ = self._resolve_text_body(
                TextStep(type="text", template_id=item_id, variant_pick="random", text_kind="link")
            )
            return bool(self.send_client.send_text(conv_local=conv_local, body=body))
        if kind == "wxa":
            return bool(self._send_wxa(conv_local=conv_local, template_id=item_id))
        if kind in {"oa", "doc", "channel"}:
            _ = load_oa_template(item_id, account_id=self.account_id)
            return bool(self.send_client.send_oa(conv_local=conv_local, template=item_id))
        if kind == "image":
            payload = load_image_template(item_id, account_id=self.account_id)
            path = str(payload.get("path", "") or payload.get("file_path", "")).strip()
            if not path:
                raise ValueError(f"素材包引用的 image 缺少路径: {item_id}")
            return bool(self.send_client.send_image(conv_local=conv_local, image_path=path))
        if kind == "video":
            payload = load_video_template(item_id, account_id=self.account_id)
            video_path = str(payload.get("path", "") or payload.get("video_path", "")).strip()
            thumb_path = str(payload.get("thumb_path", "")).strip()
            duration = payload.get("duration")
            duration_int = int(duration) if isinstance(duration, int | float) else None
            if not video_path:
                raise ValueError(f"素材包引用的 video 缺少路径: {item_id}")
            return bool(
                self.send_client.send_video(
                    conv_local=conv_local,
                    video_path=video_path,
                    thumb_path=thumb_path,
                    duration=duration_int,
                )
            )
        raise NotImplementedError(f"素材包内暂不支持类型: {kind}")

    def _sleep_between(
        self,
        unit: SendUnit,
        selected: list[SendUnit],
        pos: int,
        _selected_keys: set[tuple[int, int]],
    ) -> None:
        if self._should_stop() or pos + 1 >= len(selected):
            return
        nxt = selected[pos + 1]
        if nxt.contact_index != unit.contact_index:
            self._sleep_contact_gap()
            return
        neighbor = (
            nxt.step_index == unit.step_index and nxt.inner_index == unit.inner_index + 1
        ) or (
            nxt.contact_index == unit.contact_index and nxt.sequence_index == unit.sequence_index + 1
        )
        if neighbor and unit.gap_after_seconds > 0:
            self._interruptible_sleep(unit.gap_after_seconds)

    def _collect_targets(
        self,
        *,
        total_steps: int,
        sequence_size: int,
        start_step_index: int,
        only_step_indexes: set[int] | None,
    ) -> list[int]:
        if total_steps < 1 or sequence_size < 1:
            return []
        if only_step_indexes is None:
            return list(range(start_step_index, total_steps))
        out = [
            idx
            for idx in sorted(only_step_indexes)
            if start_step_index <= idx < total_steps
        ]
        return out

    def _collect_failed_step_indexes(self, rows: list[dict[str, object]]) -> set[int]:
        latest: dict[int, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx_raw = row.get("step_index", -1)
            try:
                idx = int(idx_raw)  # type: ignore[arg-type]
            except Exception:
                continue
            status = str(row.get("status", "")).strip()
            latest[idx] = status
        return {
            idx
            for idx, status in latest.items()
            if status in {"failed", "resolve_failed", "blocked"}
        }

    def _append_record(self, summary: RunSummary, record: RunRecord, *, done: int, total: int) -> None:
        summary.records.append(record)
        if self.progress_cb is not None:
            self.progress_cb(record, done, total)

    def _should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _append_plan_meta(self) -> None:
        try:
            view = self.report_store.load_report(self.run_uuid)
            if self.extract_plan_from_report(view.rows):
                return
        except Exception:
            pass
        self.report_store.append(
            self.run_uuid,
            {
                "type": "plan_meta",
                "plan": self.plan.model_dump(mode="json"),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "name": self.plan.name,
            },
        )

    @staticmethod
    def extract_plan_from_report(rows: list[dict[str, object]]) -> dict[str, object] | None:
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("type", "")).strip() != "plan_meta":
                continue
            raw_plan = row.get("plan")
            if isinstance(raw_plan, dict):
                return dict(raw_plan)
        return None

    def _resolve_contact(self, contact: PlanContact) -> str | None:
        conv_local = contact.conv_local.strip()
        if conv_local:
            return conv_local
        try:
            value = self.resolver(contact.display_name)
        except Exception:
            return None
        value = str(value).strip()
        return value or None

    def _run_step(
        self,
        *,
        contact: str,
        conv_local: str,
        index: int,
        step_index: int,
        step: SequenceStep,
    ) -> RunRecord:
        status = step_status(step)
        if status != "ready":
            record = RunRecord(
                contact=contact,
                conv_local=conv_local,
                step_index=step_index,
                sequence_index=index,
                step_type=step.type,
                status="blocked",
                error=status,
            )
            self._append_report(
                step_index=step_index,
                sequence_index=index,
                contact=contact,
                conv_local=conv_local,
                step_type=step.type,
                status="blocked",
                error=status,
                attempt=0,
            )
            return record

        last_error = "send returned false"
        last_variant = ""
        total_attempts = len(self.retry_delays_seconds) + 1
        for attempt in range(1, total_attempts + 1):
            if self._should_stop():
                last_error = "用户中断"
                break
            if getattr(self.send_client, "requires_preflight", True) and hasattr(
                self.send_client, "ensure_ready"
            ):
                ready = bool(getattr(self.send_client, "ensure_ready")())
                if not ready:
                    err = "Frida 会话不可用：重连 3 次失败，请检查模拟器/企微/frida-server"
                    self._append_report(
                        step_index=step_index,
                        sequence_index=index,
                        contact=contact,
                        conv_local=conv_local,
                        step_type=step.type,
                        status="failed",
                        error=err,
                        attempt=attempt,
                    )
                    raise RuntimeError(err)
            self._append_report(
                step_index=step_index,
                sequence_index=index,
                contact=contact,
                conv_local=conv_local,
                step_type=step.type,
                status="before_send",
                error="",
                attempt=attempt,
            )
            try:
                dispatch = self._dispatch(conv_local=conv_local, step=step)
                last_variant = dispatch.variant_used
                if dispatch.ok:
                    self._append_report(
                        step_index=step_index,
                        sequence_index=index,
                        contact=contact,
                        conv_local=conv_local,
                        step_type=step.type,
                        status="sent",
                        error="",
                        attempt=attempt,
                        variant_used=dispatch.variant_used,
                    )
                    return RunRecord(
                        contact=contact,
                        conv_local=conv_local,
                        step_index=step_index,
                        sequence_index=index,
                        step_type=step.type,
                        status="sent",
                        variant_used=dispatch.variant_used,
                    )
                detail = getattr(self.send_client, "last_error", "")
                last_error = detail.strip() or "send returned false"
            except RunCancelled:
                last_error = "用户中断"
                break
            except Exception as exc:
                last_error = str(exc)

            if self._should_stop() or last_error == "用户中断":
                last_error = "用户中断"
                break
            if attempt < total_attempts:
                next_wait = self.retry_delays_seconds[attempt - 1]
                self._append_report(
                    step_index=step_index,
                    sequence_index=index,
                    contact=contact,
                    conv_local=conv_local,
                    step_type=step.type,
                    status="retry_scheduled",
                    error=last_error,
                    attempt=attempt,
                    next_wait_seconds=next_wait,
                    variant_used=last_variant,
                )
                if next_wait > 0:
                    self._interruptible_sleep(next_wait)
                continue
            break

        final_status: RunnerStatus = "paused" if last_error == "用户中断" else "failed"
        self._append_report(
            step_index=step_index,
            sequence_index=index,
            contact=contact,
            conv_local=conv_local,
            step_type=step.type,
            status=final_status,
            error=last_error,
            attempt=total_attempts,
            variant_used=last_variant,
        )
        return RunRecord(
            contact=contact,
            conv_local=conv_local,
            step_index=step_index,
            sequence_index=index,
            step_type=step.type,
            status=final_status,
            error=last_error,
            variant_used=last_variant,
        )

    def _dispatch(self, *, conv_local: str, step: SequenceStep) -> DispatchResult:
        if isinstance(step, VoiceStep):
            entry = self._voice_map.get(step.voice_id)
            if entry is None:
                raise KeyError(f"voice_id 不存在: {step.voice_id}")
            silk_path = self._voice_path(entry.file)
            return DispatchResult(
                ok=self.send_client.send_voice(
                    conv_local=conv_local,
                    silk_path=silk_path,
                    voice_time=entry.duration,
                )
            )
        if isinstance(step, (OaStep, DocStep, ChannelStep)):
            return DispatchResult(
                ok=self.send_client.send_oa(conv_local=conv_local, template=step.template_id)
            )
        if isinstance(step, WxaStep):
            return DispatchResult(ok=self._send_wxa(conv_local=conv_local, template_id=step.template_id))
        if isinstance(step, TextStep):
            if step.text_kind == "link" and step.template_id and self._legacy_link_is_wxa(step.template_id):
                return DispatchResult(ok=self._send_wxa(conv_local=conv_local, template_id=step.template_id))
            body, used = self._resolve_text_body(step)
            return DispatchResult(
                ok=self.send_client.send_text(conv_local=conv_local, body=body),
                variant_used=used,
            )
        if isinstance(step, (AtStep, NoticeStep)):
            body, used = self._resolve_text_body(step)
            sender = self.send_client.send_at if isinstance(step, AtStep) else self.send_client.send_notice
            return DispatchResult(
                ok=sender(conv_local=conv_local, body=body),
                variant_used=used,
            )
        if isinstance(step, ImageStep):
            return DispatchResult(
                ok=self.send_client.send_image(conv_local=conv_local, image_path=step.path)
            )
        if isinstance(step, BundleStep):
            return DispatchResult(ok=self._dispatch_bundle(conv_local=conv_local, bundle_id=step.bundle_id))
        if isinstance(step, VideoStep):
            return DispatchResult(
                ok=self.send_client.send_video(
                    conv_local=conv_local,
                    video_path=step.path,
                    thumb_path=step.thumb_path,
                    duration=step.duration,
                )
            )
        raise NotImplementedError(f"{step.type} 尚未接入发送实现")

    def _legacy_link_is_wxa(self, template_id: str) -> bool:
        """旧界面把小程序卡片存成 link。文字链接模板优先，其余能读到卡片字段的按小程序发送。"""
        item_id = template_id.strip()
        if not item_id:
            return False
        try:
            self.voice_lib.get_text_template(item_id)
            return False
        except Exception:
            pass
        try:
            payload = load_wxa_template(item_id, account_id=self.account_id)
        except Exception:
            return False
        return has_card_fields(payload)

    def _send_wxa(self, *, conv_local: str, template_id: str) -> bool:
        payload = load_wxa_template(template_id, account_id=self.account_id)
        if not has_card_fields(payload):
            raise ValueError(f"小程序模板缺少卡片字段: {template_id}")
        return self.send_client.send_wxa(conv_local=conv_local, template=template_id)

    def _dispatch_bundle(self, *, conv_local: str, bundle_id: str) -> bool:
        """发送素材包：逐条发送 items，在 items 之间按 step_gaps 等待。"""
        bundle = get_bundle(bundle_id, account_id=self.account_id)
        if bundle is None:
            raise KeyError(f"bundle_id 不存在: {bundle_id}")
        if not bundle.items:
            raise ValueError(f"素材包为空: {bundle_id}")
        for idx, item in enumerate(bundle.items):
            if self._should_stop():
                raise RunCancelled()
            asset_type = item.asset_type.strip().lower()
            item_id = item.item_id.strip()
            if asset_type == "voice":
                entry = self._voice_map.get(item_id)
                if entry is None:
                    raise KeyError(f"素材包引用的 voice 不存在: {item_id}")
                silk_path = self._voice_path(entry.file)
                ok = self.send_client.send_voice(
                    conv_local=conv_local,
                    silk_path=silk_path,
                    voice_time=entry.duration,
                )
            elif asset_type == "text":
                body, _ = self._resolve_text_body(
                    TextStep(type="text", template_id=item_id, variant_pick="random", text_kind="text")
                )
                ok = self.send_client.send_text(conv_local=conv_local, body=body)
            elif asset_type == "link":
                if self._legacy_link_is_wxa(item_id):
                    ok = self._send_wxa(conv_local=conv_local, template_id=item_id)
                else:
                    body, _ = self._resolve_text_body(
                        TextStep(type="text", template_id=item_id, variant_pick="random", text_kind="link")
                    )
                    ok = self.send_client.send_text(conv_local=conv_local, body=body)
            elif asset_type == "wxa":
                ok = self._send_wxa(conv_local=conv_local, template_id=item_id)
            elif asset_type in {"oa", "doc", "channel"}:
                _ = load_oa_template(item_id, account_id=self.account_id)
                ok = self.send_client.send_oa(conv_local=conv_local, template=item_id)
            elif asset_type == "image":
                payload = load_image_template(item_id, account_id=self.account_id)
                path = str(payload.get("path", "") or payload.get("file_path", "")).strip()
                if not path:
                    raise ValueError(f"素材包引用的 image 缺少路径: {item_id}")
                ok = self.send_client.send_image(conv_local=conv_local, image_path=path)
            elif asset_type == "video":
                payload = load_video_template(item_id, account_id=self.account_id)
                video_path = str(payload.get("path", "") or payload.get("video_path", "")).strip()
                thumb_path = str(payload.get("thumb_path", "")).strip()
                duration = payload.get("duration")
                duration_int = int(duration) if isinstance(duration, int | float) else None
                if not video_path:
                    raise ValueError(f"素材包引用的 video 缺少路径: {item_id}")
                ok = self.send_client.send_video(
                    conv_local=conv_local,
                    video_path=video_path,
                    thumb_path=thumb_path,
                    duration=duration_int,
                )
            else:
                raise NotImplementedError(f"素材包内暂不支持类型: {asset_type}")
            if not ok:
                return False
            # 在 bundle items 之间按 step_gaps 等待
            if idx < len(bundle.items) - 1 and idx < len(bundle.step_gaps):
                gap = max(3.0, bundle.step_gaps[idx])
                self._interruptible_sleep(gap)
        return True

    def _voice_path(self, file_field: str) -> str:
        if file_field.startswith("/"):
            return file_field
        return f"{VOICE_LIB_ROOT.rstrip('/')}/{file_field.lstrip('/')}"

    def _interruptible_sleep(self, seconds: float) -> None:
        sec = float(seconds)
        if sec <= 0:
            return
        if self.stop_event is None:
            self.sleep_fn(sec)
            return
        end = time.time() + sec
        while time.time() < end:
            if self._should_stop():
                return
            remaining = end - time.time()
            chunk = min(0.2, max(0.0, remaining))
            if chunk > 0:
                self.sleep_fn(chunk)

    def _sleep_step_gap(self, seq_index: int) -> None:
        """发完 sequence[seq_index] 后按 plan.step_gaps[seq_index] 等待。"""
        if seq_index < 0 or seq_index >= len(self.plan.step_gaps):
            return
        sec = max(3.0, float(self.plan.step_gaps[seq_index]))
        self._interruptible_sleep(sec)

    def _sleep_contact_gap(self) -> None:
        """发完一个联系人所有消息后按 plan.contact_gap 等待。"""
        sec = max(1.0, float(self.plan.contact_gap))
        self._interruptible_sleep(sec)

    def _append_report(
        self,
        *,
        step_index: int,
        sequence_index: int,
        contact: str,
        conv_local: str,
        step_type: str,
        status: str,
        error: str,
        attempt: int,
        variant_used: str = "",
        next_wait_seconds: float | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "run_uuid": self.run_uuid,
            "step_index": int(step_index),
            "sequence_index": int(sequence_index),
            "inner_index": int(getattr(self, "_report_inner_index", -1)),
            "contact": contact,
            "conv_local": conv_local,
            "type": step_type,
            "status": status,
            "error": error,
            "attempt": int(attempt),
        }
        if variant_used:
            payload["variant_used"] = variant_used
        if next_wait_seconds is not None:
            payload["next_wait_seconds"] = float(next_wait_seconds)
        self.report_store.append(self.run_uuid, payload)

    def _resolve_text_body(self, step: TextStep | AtStep | NoticeStep) -> tuple[str, str]:
        if step.template_id:
            template = self._text_template_map.get(step.template_id)
            if template is None:
                raise KeyError(f"text template 不存在或已归档: {step.template_id}")
            idx = self._pick_variant_index(step.variant_pick, len(template.variants))
            return template.variants[idx], f"{template.id}#{idx}"
        return step.body, "inline"

    def _pick_variant_index(self, pick: str, total: int) -> int:
        if total < 1:
            raise ValueError("text template variants 不能为空")
        mode = pick.strip()
        if mode == "random":
            idx = int(self.rand_fn(0, total - 1))
            if idx < 0:
                return 0
            if idx >= total:
                return total - 1
            return idx
        if mode.startswith("index:"):
            idx_text = mode.removeprefix("index:").strip()
            if not idx_text.isdigit():
                raise ValueError(f"非法 variant_pick: {pick}")
            idx = int(idx_text)
            if idx < 0 or idx >= total:
                raise ValueError(f"variant_pick 越界: {pick}")
            return idx
        raise ValueError(f"非法 variant_pick: {pick}")


__all__ = [
    "BroadcastRunner",
    "RunRecord",
    "RunSummary",
    "SendClient",
    "SubprocessSendClient",
]

