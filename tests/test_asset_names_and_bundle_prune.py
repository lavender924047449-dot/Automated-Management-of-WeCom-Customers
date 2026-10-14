from __future__ import annotations

from pathlib import Path

from app.broadcast.asset_bundles import (
    AssetBundle,
    BundleItem,
    list_bundles,
    prune_missing_items,
    remove_referenced_items,
    save_bundle,
)
from app.broadcast.asset_names import asset_name_conflict, bundle_name_conflict, sequence_step_removed
from app.broadcast.library import VoiceLibrary


def _use_tmp_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.broadcast.paths.resolve_path", lambda p: tmp_path / Path(p))


def test_same_kind_names_conflict_but_cross_kind_and_bundles_do_not(monkeypatch, tmp_path: Path) -> None:
    _use_tmp_data(monkeypatch, tmp_path)
    aid = "acct_names"
    lib = VoiceLibrary.load(account_id=aid)
    lib.add_text_template(name="问候", variants=["a"], template_id="t1", kind="text")
    lib.add_link_template(name="问候", url="https://example", template_id="l1")
    lib.add(file="a.silk", duration=3, note="语音问候")
    lib.save()
    save_bundle(
        AssetBundle(id="b1", name="问候", items=[BundleItem(asset_type="text", item_id="t1")]),
        account_id=aid,
    )

    assert asset_name_conflict(aid, "text", "问候")
    assert asset_name_conflict(aid, "text", "问候", exclude_item_id="t1") is None
    assert asset_name_conflict(aid, "link", "问候", exclude_item_id="l1") is None
    assert asset_name_conflict(aid, "voice", "问候") is None
    assert asset_name_conflict(aid, "text", "未命名 #001") is None
    assert bundle_name_conflict(aid, "问候")
    assert bundle_name_conflict(aid, "问候", exclude_id="b1") is None
    assert bundle_name_conflict(aid, "另一个包") is None


def test_prune_removes_missing_assets_and_deletes_empty_bundles(monkeypatch, tmp_path: Path) -> None:
    _use_tmp_data(monkeypatch, tmp_path)
    aid = "acct_prune"
    save_bundle(
        AssetBundle(
            id="empty_me",
            name="只剩过期素材",
            items=[BundleItem(asset_type="voice", item_id="gone")],
        ),
        account_id=aid,
    )
    save_bundle(
        AssetBundle(
            id="keep_me",
            name="还剩一条",
            items=[
                BundleItem(asset_type="text", item_id="keep"),
                BundleItem(asset_type="wxa", item_id="old_link"),
            ],
        ),
        account_id=aid,
    )

    removed_items, removed_bundles = prune_missing_items({("text", "keep"), ("wxa", "old_link")}, account_id=aid)

    assert ("voice", "gone") in removed_items
    assert removed_bundles == ["empty_me"]
    left = list_bundles(aid)
    assert [row.id for row in left] == ["keep_me"]
    assert [(row.asset_type, row.item_id) for row in left[0].items] == [("text", "keep"), ("wxa", "old_link")]

    removed_again, bundles_again = remove_referenced_items([("text", "keep")], account_id=aid)
    assert ("text", "keep") in removed_again
    assert bundles_again == []
    left = list_bundles(aid)
    assert len(left) == 1
    assert [row.item_id for row in left[0].items] == ["old_link"]


def test_sequence_step_removed_matches_asset_and_deleted_bundle_only() -> None:
    keys = {("voice", "v1"), ("text", "t1"), ("link", "l1")}
    paths = {"/tmp/a.png"}
    bundles = {"b_empty"}
    assert sequence_step_removed({"type": "voice", "voice_id": "v1"}, keys, paths, bundles)
    assert sequence_step_removed({"type": "text", "template_id": "t1", "text_kind": "text"}, keys, paths, bundles)
    assert sequence_step_removed({"type": "text", "template_id": "l1", "text_kind": "link"}, keys, paths, bundles)
    assert not sequence_step_removed({"type": "text", "body": "临时文字", "text_kind": "text"}, keys, paths, bundles)
    assert sequence_step_removed({"type": "image", "path": "/tmp/a.png"}, keys, paths, bundles)
    assert sequence_step_removed({"type": "bundle", "bundle_id": "b_empty"}, keys, paths, bundles)
    assert not sequence_step_removed({"type": "bundle", "bundle_id": "b_keep"}, keys, paths, bundles)
    assert not sequence_step_removed({"_row_type": "gap", "gap": 3}, keys, paths, bundles)
