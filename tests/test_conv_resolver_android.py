from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from app.broadcast.labels import LocalLabelStore
from runtime.wework_re_android.conv_resolver import ConversationResolver, parse_hits, parse_rows, parse_label_map


def _ok(stdout: str) -> CompletedProcess[str]:
    return CompletedProcess(args=["python"], returncode=0, stdout=stdout, stderr="")


def test_parse_hits_extracts_name_members():
    out = (
        "[HIT] local=7685582584742596232 remote=7881300363276969 "
        "name='海鸟与鱼' members=['海鸟与鱼'] labels=['新客']\n"
    )
    hits = parse_hits(out)
    assert len(hits) == 1
    assert hits[0].conv_local == "7685582584742596232"
    assert hits[0].name == "海鸟与鱼"
    assert hits[0].members == ("海鸟与鱼",)
    assert hits[0].labels == ("新客",)


def test_resolve_prefers_exact_match_and_uses_cache():
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _ok(
            "\n".join(
                [
                    "[HIT] local=1001 remote=1 name='海鸟与鱼-测试' members=['a']",
                    "[HIT] local=1002 remote=2 name='海鸟与鱼' members=['b']",
                ]
            )
        )

    resolver = ConversationResolver(run_fn=fake_run, cache_ttl_seconds=60.0, time_fn=lambda: 100.0)
    assert resolver.resolve("海鸟与鱼") == "1002"
    assert resolver.resolve("海鸟与鱼") == "1002"
    assert len(calls) == 1


def test_resolve_raises_on_ambiguous_match():
    def fake_run(_cmd, **_kwargs):
        return _ok(
            "\n".join(
                [
                    "[HIT] local=2001 remote=1 name='张三-华东' members=['张三']",
                    "[HIT] local=2002 remote=2 name='张三-华南' members=['张三']",
                ]
            )
        )

    resolver = ConversationResolver(run_fn=fake_run)
    with pytest.raises(LookupError, match="匹配不唯一"):
        resolver.resolve("张三")


def test_resolve_raises_on_not_found():
    resolver = ConversationResolver(run_fn=lambda _cmd, **_kwargs: _ok("[now] total=0"))
    with pytest.raises(LookupError, match="未找到联系人"):
        resolver.resolve("不存在")


def test_resolve_by_label_returns_all_unique_conv_local(tmp_path):
    def fake_run(_cmd, **_kwargs):
        return _ok(
            "\n".join(
                [
                    "[HIT] local=3001 remote=1 name='A' members=['a'] labels=['新客','重点']",
                    "[HIT] local=3002 remote=2 name='B' members=['b'] labels=['新客']",
                    "[HIT] local=3001 remote=1 name='A' members=['a'] labels=['新客']",
                ]
            )
        )

    resolver = ConversationResolver(
        run_fn=fake_run,
        local_label_path=tmp_path / "empty_labels.json",
    )
    assert resolver.resolve_by_label("新客") == ["3001", "3002"]


def test_resolve_by_label_fallbacks_to_local_store(tmp_path):
    local_path = tmp_path / "customer_labels.json"
    store = LocalLabelStore(path=local_path)
    store.set_labels("8888", ["新客"])
    store.save()

    resolver = ConversationResolver(
        run_fn=lambda _cmd, **_kwargs: _ok("[now] total=0"),
        local_label_path=local_path,
    )
    assert resolver.resolve_by_label("新客") == ["8888"]


def test_resolve_by_label_fallbacks_when_frida_query_fails(tmp_path):
    local_path = tmp_path / "customer_labels.json"
    store = LocalLabelStore(path=local_path)
    store.set_labels("7685582584742596232", ["测试5a"])
    store.save()

    def fake_run(_cmd, **_kwargs):
        return CompletedProcess(args=["python"], returncode=1, stdout="", stderr="no pid")

    resolver = ConversationResolver(run_fn=fake_run, local_label_path=local_path)
    assert resolver.resolve_by_label("测试5a") == ["7685582584742596232"]


def test_resolve_by_label_merges_alpha_and_beta(tmp_path):
    local_path = tmp_path / "customer_labels.json"
    store = LocalLabelStore(path=local_path)
    store.set_labels("3001", ["新客"])
    store.set_labels("9009", ["新客"])
    store.save()

    def fake_run(_cmd, **_kwargs):
        return _ok("[HIT] local=3001 remote=1 name='A' members=['a'] labels=['新客']")

    resolver = ConversationResolver(run_fn=fake_run, local_label_path=local_path)
    assert resolver.resolve_by_label("新客") == ["3001", "9009"]


def test_resolve_by_label_raises_when_alpha_and_beta_empty(tmp_path):
    resolver = ConversationResolver(
        run_fn=lambda _cmd, **_kwargs: _ok("[now] total=0"),
        local_label_path=tmp_path / "empty_labels.json",
    )
    with pytest.raises(LookupError, match="未找到标签"):
        resolver.resolve_by_label("新客")


# ==================== Step 2: 多标签解析测试 ====================


def test_parse_rows_preserves_multiple_label_names():
    """两个 labelNames 都应进入 ConversationHit.labels。"""
    import json

    row = json.dumps({
        "t": "row", "tag": "now", "i": 0,
        "local": "1001", "remote": "2001", "type": 0,
        "peerName": "客户A", "ourRemark": "备注A",
        "userName": "", "corpName": "", "groupCandidates": {},
        "labelIds": ["111", "222"],
        "labelNames": ["VIP客户", "重点跟进"],
        "raw": "",
    })
    hits = parse_rows(row)
    assert len(hits) == 1
    assert "VIP客户" in hits[0].labels
    assert "重点跟进" in hits[0].labels
    assert len(hits[0].labels) == 2


def test_parse_rows_drops_numeric_only_label_ids():
    """纯数字 label id 在没有名字时不展示成标签文案。"""
    import json

    row = json.dumps({
        "t": "row", "tag": "now", "i": 0,
        "local": "1001", "remote": "2001", "type": 0,
        "peerName": "客户B", "ourRemark": "",
        "userName": "", "corpName": "", "groupCandidates": {},
        "labelIds": ["999"],
        "labelNames": [],
        "raw": "",
    })
    hits = parse_rows(row)
    assert len(hits) == 1
    assert hits[0].labels == ()
    assert hits[0].has_external_label is True
    assert hits[0].corp_name == ""


def test_parse_rows_uses_account_label_map_fallback():
    """当 labelNames 为空时，account_label_map 提供的名字应被使用。"""
    import json

    row = json.dumps({
        "t": "row", "tag": "now", "i": 0,
        "local": "1001", "remote": "2001", "type": 0,
        "peerName": "客户C", "ourRemark": "",
        "userName": "", "corpName": "", "groupCandidates": {},
        "labelIds": ["333"],
        "labelNames": [],
        "raw": "",
    })
    acct_map = {"333": "大客户"}
    hits = parse_rows(row, account_label_map=acct_map)
    assert len(hits) == 1
    assert "大客户" in hits[0].labels


def test_parse_label_map_extracts_from_output():
    """parse_label_map 应正确提取 t:'label_map' 事件。"""
    import json

    line = json.dumps({"t": "label_map", "map": {"111": "VIP", "222": "新客"}})
    result = parse_label_map(line)
    assert result == {"111": "VIP", "222": "新客"}


def test_parse_label_map_returns_empty_on_no_event():
    """没有 label_map 事件时返回空字典。"""
    result = parse_label_map("[now] total=5\n")
    assert result == {}
