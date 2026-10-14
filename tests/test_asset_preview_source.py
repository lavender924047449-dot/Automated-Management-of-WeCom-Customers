from __future__ import annotations

from pathlib import Path

from app.broadcast.asset_preview_source import asset_preview_payload, lookup_asset_name


def _bind(monkeypatch, module, root: Path) -> None:
    monkeypatch.setattr(module, "data_root_for", lambda _account_id=None: root)


def test_named_cards_images_and_wxa_preview_from_library(tmp_path: Path, monkeypatch) -> None:
    from app.broadcast import image_templates, oa_templates, wxa_templates

    _bind(monkeypatch, oa_templates, tmp_path)
    _bind(monkeypatch, image_templates, tmp_path)
    _bind(monkeypatch, wxa_templates, tmp_path)
    account_id = "acc"
    oa_templates.save_template(
        {
            "name": "春季视频",
            "title": "视频标题",
            "url": "https://channels.weixin.qq.com/spring",
            "card_kind": "channel",
            "desc": "看一看",
            "sourcename": "视频号",
        },
        template_id="ch-1",
        account_id=account_id,
    )
    oa_templates.save_template(
        {
            "name": "产品说明",
            "title": "说明文档",
            "url": "https://doc.weixin.qq.com/doc/1",
            "card_kind": "doc",
            "desc": "内部文档",
        },
        template_id="doc-1",
        account_id=account_id,
    )
    image_templates.save_template(
        {"name": "海报", "path": "D:/assets/poster.png"},
        template_id="img-1",
        account_id=account_id,
    )
    wxa_templates.save_template(
        {"name": "报名小程序", "title": "马上报名", "appName": "活动", "username": "gh_demo"},
        template_id="wxa-1",
        account_id=account_id,
    )

    channel = asset_preview_payload(account_id, {"type": "channel", "item_id": "ch-1"})
    assert channel["asset_type"] == "channel"
    assert channel["name"] == "春季视频"
    assert channel["oa_title"] == "视频标题"
    assert "channels.weixin.qq.com" in channel["oa_url"]

    doc = asset_preview_payload(account_id, {"type": "doc", "template_id": "doc-1", "display_name": "未命名"})
    assert doc["name"] == "产品说明"
    assert doc["asset_type"] == "doc"

    image = asset_preview_payload(account_id, {"type": "image", "item_id": "img-1"})
    assert image["name"] == "海报"
    assert image["path"] == "D:/assets/poster.png"

    wxa = asset_preview_payload(account_id, {"type": "wxa", "template_id": "wxa-1"})
    assert wxa["name"] == "报名小程序"
    assert wxa["wxa_title"] == "马上报名"
    assert wxa["wxa_app"] == "活动"

    assert lookup_asset_name(account_id, "channel", item_id="ch-1") == "春季视频"
    assert lookup_asset_name(account_id, "image", item_id="img-1") == "海报"


def test_notice_and_at_preview_use_body() -> None:
    notice = asset_preview_payload("acc", {"type": "notice", "body": "今晚开会"})
    mention = asset_preview_payload("acc", {"type": "at", "body": "请看公告"})
    assert notice["asset_type"] == "notice"
    assert notice["variants"] == ["今晚开会"]
    assert mention["asset_type"] == "at"
    assert mention["variants"] == ["请看公告"]
