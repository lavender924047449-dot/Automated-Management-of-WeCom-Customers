from __future__ import annotations

from runtime.wework_re_android.conv_resolver import ConversationHit

from app.broadcast.contacts import refresh_contacts


class _FakeLabelStore:
    def labels_for(self, conv_local: str) -> list[str]:
        if conv_local == "1001":
            return ["重点客户", "内部推荐"]
        return []


def test_refresh_contacts_merges_labels_and_infers_category(monkeypatch):
    hits = [
        ConversationHit(
            conv_local="1001",
            remote="r1",
            name="海鸟与鱼",
            members=("海鸟与鱼",),
            labels=("大客户",),
        ),
        ConversationHit(
            conv_local="1002",
            remote="r2",
            name="市场部同事A",
            members=("市场部同事A",),
            labels=(),
        ),
        ConversationHit(
            conv_local="1003",
            remote="r3",
            name="广州客户群",
            members=("A", "B"),
            labels=(),
        ),
    ]
    monkeypatch.setattr("app.broadcast.contacts.list_conversations", lambda *, refresh: hits)
    monkeypatch.setattr("app.broadcast.contacts.LocalLabelStore.load", lambda account_id: _FakeLabelStore())

    rows = refresh_contacts("acct_1", refresh=True)
    assert len(rows) == 3
    assert rows[0].display_name == "海鸟与鱼"
    assert rows[0].category == "外部客户"
    assert rows[0].labels == ["大客户", "重点客户", "内部推荐"]
    assert rows[1].category == "内部同事"
    assert rows[2].category == "外部群"
