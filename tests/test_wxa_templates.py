from __future__ import annotations

from pathlib import Path

from app.broadcast import wxa_templates


def _bind_tmp(monkeypatch, tmp_path: Path) -> None:
    """把 wxa_templates 的账号根目录重定向到临时路径。"""

    def fake_root(_account_id):
        return tmp_path

    monkeypatch.setattr(wxa_templates, "data_root_for", fake_root)


def test_save_and_load_template(tmp_path: Path, monkeypatch):
    _bind_tmp(monkeypatch, tmp_path)

    out = wxa_templates.save_template({"msgType": 99, "title": "腾讯文档"}, template_id="wxa-1")
    assert out.exists()

    loaded = wxa_templates.load_template("wxa-1")
    assert loaded["template_id"] == "wxa-1"
    assert loaded["msgType"] == 99
    assert loaded["title"] == "腾讯文档"


def test_list_templates_sorted(tmp_path: Path, monkeypatch):
    _bind_tmp(monkeypatch, tmp_path)

    wxa_templates.save_template({"msgType": 1}, template_id="b")
    wxa_templates.save_template({"msgType": 2}, template_id="a")
    names = [p.name for p in wxa_templates.list_templates()]
    assert names == ["a.json", "b.json"]


def test_pending_and_commit(tmp_path: Path, monkeypatch):
    _bind_tmp(monkeypatch, tmp_path)

    pending_path = wxa_templates.save_template({"msgType": 3, "title": "候选"}, template_id="p1", pending=True)
    assert pending_path.parent.name == wxa_templates.PENDING_SUBDIR
    assert [p.name for p in wxa_templates.list_pending()] == ["p1.json"]
    # 未 commit 时不应出现在正式列表
    assert wxa_templates.list_templates() == []

    committed = wxa_templates.commit_pending(pending_path, name="用户命名", tags=["a", "b", "a"])
    assert committed.exists()
    assert not pending_path.exists()
    assert wxa_templates.list_pending() == []
    loaded = wxa_templates.load_template("p1")
    assert loaded["name"] == "用户命名"
    assert loaded["tags"] == ["a", "b"]


def test_discard_pending(tmp_path: Path, monkeypatch):
    _bind_tmp(monkeypatch, tmp_path)

    pending_path = wxa_templates.save_template({"msgType": 3}, template_id="dx", pending=True)
    assert pending_path.exists()
    wxa_templates.discard_pending(pending_path)
    assert not pending_path.exists()
    assert wxa_templates.list_pending() == []
