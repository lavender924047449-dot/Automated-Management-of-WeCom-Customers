"""按账号保存最近一次执行的 Plan 指针，便于重启/切账号后恢复重试上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.broadcast.paths import data_root_for
from app.security import get_secure_store

_SECURE_STORE = get_secure_store()


@dataclass
class LastRunSnapshot:
    run_uuid: str
    plan: dict[str, Any]
    status: str = "unknown"
    updated_at: str = ""


class LastRunStore:
    def __init__(self, *, account_id: str) -> None:
        self.account_id = account_id.strip() or "default"
        self.path = data_root_for(self.account_id) / "last_run.json"

    def save(self, *, run_uuid: str, plan: dict[str, Any], status: str) -> Path:
        payload = {
            "run_uuid": run_uuid.strip(),
            "plan": dict(plan),
            "status": status.strip() or "unknown",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _SECURE_STORE.save_json(self.path, payload, encrypt=True)
        return self.path

    def load(self) -> LastRunSnapshot | None:
        if not self.path.exists():
            return None
        raw = _SECURE_STORE.load_json(self.path, default={})
        if not isinstance(raw, dict):
            return None
        run_uuid = str(raw.get("run_uuid", "")).strip()
        plan = raw.get("plan")
        if not run_uuid or not isinstance(plan, dict):
            return None
        return LastRunSnapshot(
            run_uuid=run_uuid,
            plan=dict(plan),
            status=str(raw.get("status", "")).strip() or "unknown",
            updated_at=str(raw.get("updated_at", "")).strip(),
        )


__all__ = ["LastRunSnapshot", "LastRunStore"]
