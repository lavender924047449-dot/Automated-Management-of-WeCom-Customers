from runtime.wework_re_android.fta_capture_lite import JS, MTYPE_OA, MTYPE_WXA


def test_mini_program_mtype_is_not_classified_as_oa() -> None:
    assert 78 not in MTYPE_OA
    assert MTYPE_WXA == {78}
    assert MTYPE_OA == {13}
    assert "mType === 13 || mType === 78" not in JS
    assert "event: 'fta_wxa_seen'" in JS
    assert "event: 'fta_text_seen'" in JS
    assert "mType === 0 || mType === 1 || mType === 2" in JS
    assert "if (ct !== 13 && ct !== 78) return null;" not in JS
    assert "if (ct !== 13) return null;" in JS
