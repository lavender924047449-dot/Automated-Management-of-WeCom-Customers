"""Step 14: 运行报告存储（jsonl + resume + csv + gc）。"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.broadcast.paths import account_reports_dir
from app.security import get_secure_store

COMPLETE_STATUSES = frozenset({"sent", "blocked", "resolve_failed"})
_SECURE_STORE = get_secure_store()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class ReportView:
    run_uuid: str
    path: Path
    rows: list[dict[str, Any]]

    def last_completed_step_index(self) -> int:
        done = -1
        for row in self.rows:
            status = str(row.get("status", "")).strip()
            if status not in COMPLETE_STATUSES:
                continue
            idx = int(row.get("step_index", -1))
            if idx > done:
                done = idx
        return done

    def next_resume_step_index(self) -> int:
        return self.last_completed_step_index() + 1


@dataclass
class ReportMeta:
    run_uuid: str
    path: Path
    updated_at: datetime


class ReportStore:
    """按账号存储 plan_<uuid>.jsonl 报告。"""

    def __init__(self, *, account_id: str | None = None, reports_dir: str | Path | None = None) -> None:
        base = Path(reports_dir) if reports_dir else account_reports_dir(account_id)
        self.reports_dir = base
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def report_path(self, run_uuid: str) -> Path:
        return self.reports_dir / f"plan_{run_uuid}.jsonl"

    def append(self, run_uuid: str, row: dict[str, Any]) -> Path:
        payload = dict(row)
        payload.setdefault("ts", _now_iso())
        target = self.report_path(run_uuid)
        return _SECURE_STORE.append_jsonl(target, payload, encrypt=True)

    def load_report(self, run_uuid: str) -> ReportView:
        target = self.report_path(run_uuid)
        rows = _SECURE_STORE.load_jsonl(target)
        return ReportView(run_uuid=run_uuid, path=target, rows=rows)

    def export_csv(self, run_uuid: str, out_path: str | Path | None = None) -> Path:
        view = self.load_report(run_uuid)
        dest = Path(out_path) if out_path else (self.reports_dir / f"plan_{run_uuid}.csv")
        dest.parent.mkdir(parents=True, exist_ok=True)
        rows = view.rows
        fieldnames = sorted({key for row in rows for key in row.keys()})
        if not fieldnames:
            fieldnames = ["step_index", "contact", "type", "status", "ts", "error"]
        with dest.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return dest

    def list_reports(self) -> list[ReportMeta]:
        reports: list[ReportMeta] = []
        for path in self.reports_dir.glob("plan_*.jsonl"):
            stem = path.stem
            if not stem.startswith("plan_"):
                continue
            run_uuid = stem.removeprefix("plan_").strip()
            if not run_uuid:
                continue
            updated_at = datetime.fromtimestamp(path.stat().st_mtime)
            reports.append(ReportMeta(run_uuid=run_uuid, path=path, updated_at=updated_at))
        return sorted(reports, key=lambda x: x.updated_at, reverse=True)

    def delete_reports(self, run_uuids: list[str]) -> int:
        removed = 0
        seen: set[str] = set()
        for run_uuid_raw in run_uuids:
            run_uuid = str(run_uuid_raw or "").strip()
            if not run_uuid or run_uuid in seen:
                continue
            seen.add(run_uuid)
            jsonl_path = self.report_path(run_uuid)
            csv_path = self.reports_dir / f"plan_{run_uuid}.csv"
            if jsonl_path.exists():
                jsonl_path.unlink(missing_ok=True)
                removed += 1
            if csv_path.exists():
                csv_path.unlink(missing_ok=True)
                removed += 1
        return removed

    def gc(self, *, days: int = 90, now: datetime | None = None) -> int:
        keep_days = max(1, int(days))
        current = now or datetime.now()
        cutoff = current - timedelta(days=keep_days)
        removed = 0
        for path in self.reports_dir.glob("plan_*.*"):
            if path.suffix not in {".jsonl", ".csv"}:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime >= cutoff:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed


__all__ = ["COMPLETE_STATUSES", "ReportMeta", "ReportStore", "ReportView"]
