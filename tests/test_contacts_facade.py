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
            conv_type=2,
        ),
        ConversationHit(
            conv_local="1004",
            remote="r4",
            name="备注里的同事",
            members=("备注里的同事",),
            labels=(),
            conv_type=0,
            has_external_label=True,
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
    assert len(rows) == 4
    assert rows[0].display_name == "海鸟与鱼"
    assert rows[0].category == "外部客户"
    assert rows[0].labels == ["大客户", "重点客户", "内部推荐"]
    assert rows[1].category == "内部同事"
    # 单聊即使名字里带「同事」，scene=1 标签命中也归外部客户
    assert rows[2].category == "外部客户"
    assert rows[3].category == "外部群"


def test_friend_empty_corp_is_external_customer(monkeypatch):
    friends = [
        {
            "remote": "7881001",
            "displayName": "外部微信好友",
            "name": "外部微信好友",
            "corpName": "",
            "labelIds": [],
            "labelNames": [],
        },
        {
            "remote": "7881002",
            "displayName": "某公司客户",
            "name": "某公司客户",
            "corpName": "其他公司",
            "labelIds": ["1"],
            "labelNames": ["VIP"],
            "hasExternalLabel": True,
        },
        {
            "remote": "1688001",
            "displayName": "市场部同事B",
            "name": "市场部同事B",
            "corpName": "",
            "labelIds": [],
            "labelNames": [],
        },
    ]
    monkeypatch.setattr(
        "app.broadcast.contacts.list_conversations_and_friends",
        lambda *, refresh: ([], friends, {}),
    )
    monkeypatch.setattr("app.broadcast.contacts.LocalLabelStore.load", lambda account_id: _FakeLabelStore())

    rows = refresh_contacts("acct_1", refresh=True, include_cached_friends=True)
    by_name = {row.display_name: row.category for row in rows}
    assert by_name["外部微信好友"] == "外部客户"
    assert by_name["某公司客户"] == "外部客户"
    assert by_name["市场部同事B"] == "内部同事"
