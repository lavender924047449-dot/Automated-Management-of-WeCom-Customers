from app.broadcast.link_cards import card_kind_of, classify_link_card


def test_channel_url_is_not_oa() -> None:
    kind = classify_link_card(url="https://channels.weixin.qq.com/finder/abc", appname="某账号")
    assert kind == "channel"


def test_doc_url_is_not_oa() -> None:
    kind = classify_link_card(url="https://doc.weixin.qq.com/doc/w3_abc", title="周报")
    assert kind == "doc"


def test_mp_article_stays_oa() -> None:
    kind = classify_link_card(url="https://mp.weixin.qq.com/s/abc", appname="行业资讯")
    assert kind == "oa"


def test_stored_kind_wins_over_url() -> None:
    payload = {
        "card_kind": "doc",
        "url": "https://mp.weixin.qq.com/s/abc",
        "title": "文档",
    }
    assert card_kind_of(payload) == "doc"
