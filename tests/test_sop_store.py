from __future__ import annotations

from pathlib import Path

from app.broadcast.sop_store import (
    SopTemplate,
    delete_sop,
    list_sops,
    load_sop,
    resolve_contacts_by_labels,
    save_sop,
)


def test_sop_store_crud(monkeypatch, tmp_path: Path):
    sop_file = tmp_path / "sop_templates.json"
    monkeypatch.setattr("app.broadcast.sop_store.sop_path", lambda _aid: sop_file)

    sop = SopTemplate(
        name="首轮跟进",
        labels=["新客", "高意向"],
        sequence=[{"type": "text", "body": "你好"}],
        msg_gap_seconds=(10, 20),
        contact_gap_seconds=(60, 120),
        perturb=True,
    )
    saved = save_sop(sop, account_id="acct_a")
    assert saved.id

    rows = list_sops("acct_a")
    assert len(rows) == 1
    assert rows[0].name == "首轮跟进"

    loaded = load_sop(saved.id, account_id="acct_a")
    assert loaded.labels == ["新客", "高意向"]

    assert delete_sop(saved.id, account_id="acct_a") is True
    assert list_sops("acct_a") == []
    assert delete_sop(saved.id, account_id="acct_a") is False


def test_resolve_contacts_by_labels_merge_alpha_beta(monkeypatch):
    class _FakeLabelStore:
        def rows(self) -> dict[str, list[str]]:
            return {
                "1002": ["新客"],
                "1003": ["沉默客户", "高意向"],
            }

    monkeypatch.setattr("app.broadcast.sop_store.LocalLabelStore.load", lambda account_id: _FakeLabelStore())

    rows = [
        {"conv_local": "1001", "display_name": "海鸟与鱼", "labels": ["高意向"]},
        {"conv_local": "1002", "display_name": "笑看人生", "labels": []},
        {"conv_local": "1003", "display_name": "广州客户群", "labels": ["群聊"]},
    ]
    contacts = resolve_contacts_by_labels(["新客", "高意向"], rows, account_id="acct_a")
    assert [x.conv_local for x in contacts] == ["1003", "1001", "1002"]
    assert [x.display_name for x in contacts] == ["广州客户群", "海鸟与鱼", "笑看人生"]
