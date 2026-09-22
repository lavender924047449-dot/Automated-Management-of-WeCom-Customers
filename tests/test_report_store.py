from __future__ import annotations

from datetime import datetime, timedelta

from app.broadcast.report_store import ReportStore


def test_load_report_and_resume_cursor(tmp_path):
    store = ReportStore(reports_dir=tmp_path / "reports")
    run_uuid = "u1"
    store.append(
        run_uuid,
        {
            "step_index": 0,
            "contact": "A",
            "type": "text",
            "status": "sent",
            "error": "",
            "attempt": 1,
        },
    )
    store.append(
        run_uuid,
        {
            "step_index": 1,
            "contact": "A",
            "type": "text",
            "status": "failed",
            "error": "x",
            "attempt": 3,
        },
    )
    view = store.load_report(run_uuid)
    assert len(view.rows) == 2
    assert view.last_completed_step_index() == 0
    assert view.next_resume_step_index() == 1


def test_export_csv_and_gc(tmp_path):
    store = ReportStore(reports_dir=tmp_path / "reports")
    run_uuid = "u2"
    path = store.append(
        run_uuid,
        {
            "step_index": 0,
            "contact": "B",
            "type": "image",
            "status": "sent",
            "error": "",
            "attempt": 1,
        },
    )
    csv_path = store.export_csv(run_uuid)
    assert csv_path.exists()
    body = csv_path.read_text(encoding="utf-8")
    assert "step_index" in body
    assert "image" in body

    old = datetime.now() - timedelta(days=120)
    old_ts = old.timestamp()
    path.touch()
    csv_path.touch()
    # 让文件变旧，触发 90 天 gc
    import os

    os.utime(path, (old_ts, old_ts))
    os.utime(csv_path, (old_ts, old_ts))
    removed = store.gc(days=90)
    assert removed == 2


def test_delete_reports_remove_jsonl_and_csv(tmp_path):
    store = ReportStore(reports_dir=tmp_path / "reports")
    run_keep = "u_keep"
    run_del = "u_del"
    store.append(run_keep, {"step_index": 0, "status": "sent", "type": "text"})
    store.append(run_del, {"step_index": 0, "status": "sent", "type": "text"})
    csv_del = store.export_csv(run_del)
    assert store.report_path(run_del).exists()
    assert csv_del.exists()

    removed = store.delete_reports([run_del, run_del, "", "  "])

    assert removed == 2
    assert not store.report_path(run_del).exists()
    assert not csv_del.exists()
    assert store.report_path(run_keep).exists()
