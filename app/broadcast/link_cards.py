"""企微文档、视频号、公众号都走 LinkMessage，这里按链接和来源把它们分开。"""

from __future__ import annotations

LINK_CARD_KINDS = ("oa", "doc", "channel")

_CHANNEL_URL_MARKS = (
    "channels.weixin.qq.com",
    "finder.video",
    "findermp",
    "weixin://channels",
    "/sph/",
)
_DOC_URL_MARKS = (
    "doc.weixin.qq.com",
    "docs.qq.com",
    "docqq.com",
    "wedoc",
    "tencentdoc",
)
_DOC_NAMES = ("腾讯文档", "企业微信文档", "企微文档", "微文档")


def classify_link_card(
    *,
    url: str = "",
    title: str = "",
    desc: str = "",
    appname: str = "",
    card_kind: str = "",
) -> str:
    """返回 oa / doc / channel。已写入的 card_kind 优先，避免改名后被重新猜错。"""
    stored = str(card_kind or "").strip().lower()
    if stored in LINK_CARD_KINDS:
        return stored
    link = str(url or "").strip().lower()
    source = str(appname or "").strip()
    if any(mark in link for mark in _CHANNEL_URL_MARKS) or "视频号" in source:
        return "channel"
    if any(mark in link for mark in _DOC_URL_MARKS) or any(name in source for name in _DOC_NAMES):
        return "doc"
    blob = f"{title} {desc}"
    if "视频号" in blob and "视频号" not in source and not link:
        return "channel"
    return "oa"


def card_kind_of(payload: dict | None) -> str:
    data = payload if isinstance(payload, dict) else {}
    return classify_link_card(
        url=str(data.get("url") or data.get("link") or ""),
        title=str(data.get("title") or ""),
        desc=str(data.get("desc") or data.get("digest") or ""),
        appname=str(data.get("sourcename") or data.get("appname") or ""),
        card_kind=str(data.get("card_kind") or ""),
    )


def is_link_card(kind: str) -> bool:
    return str(kind or "").strip().lower() in LINK_CARD_KINDS
