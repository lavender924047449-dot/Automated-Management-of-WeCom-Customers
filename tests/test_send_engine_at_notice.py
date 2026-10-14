"""@所有人 / 群公告 接入 send_engine 的脚本与参数。"""

from runtime.wework_re_android.send_engine import build_js, parse_args


def test_parse_at_and_notice_bodies():
    at_args = parse_args(["at", "--conv-local", "1001", "--body=--大家好"])
    notice_args = parse_args(["notice", "--conv-local", "1001", "--body", "本周公告"])
    assert at_args.action == "at"
    assert at_args.body == "--大家好"
    assert notice_args.action == "notice"
    assert notice_args.body == "本周公告"


def test_at_script_uses_verified_wire_and_waits_for_callback():
    js = build_js(action="at", conv_local="1001", body="大家好")
    assert "function sendAtOrNotice" in js
    assert r"\u6240\u6709\u4eba " in js
    assert "SetGroupNotification" in js
    assert "SendMessage" in js
    assert "EngineNoticeCb' + Date.now()" not in js
    assert "com.testvoice.EngineNoticeCb" in js
    assert "com.testvoice.EngineAtCb" in js
    branch = js.split("action === 'at' || action === 'notice'", 1)[1]
    assert "sendAtOrNotice(action, String(convId), '大家好');" in branch
    assert branch.index("return;") < branch.index("action=' + action + ' return='")
