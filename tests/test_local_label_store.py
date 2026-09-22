from __future__ import annotations

from app.broadcast.labels import LocalLabelStore


def test_local_label_store_roundtrip(tmp_path):
    path = tmp_path / "labels.json"
    store = LocalLabelStore(path=path)
    store.set_labels("1001", ["新客", "重点", "新客"])
    store.add_label("1002", "老客")
    store.save()

    loaded = LocalLabelStore.load(path)
    assert loaded.labels_for("1001") == ["新客", "重点"]
    assert loaded.labels_for("1002") == ["老客"]


def test_local_label_store_resolve_tag(tmp_path):
    path = tmp_path / "labels.json"
    store = LocalLabelStore(path=path)
    store.set_labels("1001", ["新客", "华东"])
    store.set_labels("1002", ["老客"])

    assert store.resolve("新客") == ["1001"]


def test_local_label_store_rows_and_remove_conv(tmp_path):
    path = tmp_path / "labels.json"
    store = LocalLabelStore(path=path)
    store.set_labels("1001", ["新客"])
    store.set_labels("1002", ["老客"])

    assert set(store.rows().keys()) == {"1001", "1002"}
    assert store.remove_conv("1001") is True
    assert store.remove_conv("1001") is False
    assert store.rows() == {"1002": ["老客"]}
