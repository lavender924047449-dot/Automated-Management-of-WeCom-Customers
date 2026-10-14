from gui.asset_order import newest_first


def test_newest_material_comes_first() -> None:
    rows = [
        {"name": "旧", "extra": {"entered_at": "2026-01-01T08:00:00"}},
        {"name": "新", "extra": {"entered_at": "2026-10-14T16:00:00"}},
        {"name": "中", "updated_at": "2026-05-01T12:00:00"},
        {"name": "没有时间"},
    ]
    ordered = [row["name"] for row in newest_first(rows)]
    assert ordered == ["新", "中", "旧", "没有时间"]
