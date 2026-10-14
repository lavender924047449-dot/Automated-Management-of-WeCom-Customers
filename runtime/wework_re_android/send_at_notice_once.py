"""按已捕获的入口，向指定群发一条 @所有人 和一条群公告。

  .venv\\Scripts\\python.exe -u -m runtime.wework_re_android.send_at_notice_once --adb-serial emulator-5554
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from runtime.wework_re_android._frida_env import adb_binary, import_frida

DEFAULT_ADB_SERIAL = "emulator-5554"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_GROUP = "测试群000"
DEFAULT_AT_TEXT = "测试at"
DEFAULT_NOTICE_TEXT = "测试公告"

JS = r"""
'use strict';
Java.perform(function () {
    var targetName = __TARGET__;
    var atText = __AT_TEXT__;
    var noticeText = __NOTICE_TEXT__;
    var skipNotice = __SKIP_NOTICE__;
    var KEEP = [];

    function emit(event, payload) {
        var out = { event: event };
        if (payload) Object.keys(payload).forEach(function (k) { out[k] = payload[k]; });
        send(out);
    }

    function utf8(s) {
        return Java.use('java.lang.String').$new(s).getBytes('UTF-8');
    }

    function setInt(obj, name, n) {
        var f = obj.getClass().getDeclaredField(name);
        f.setAccessible(true);
        f.setInt(obj, n);
    }

    function setObj(obj, name, value) {
        try {
            obj[name].value = value;
            return;
        } catch (eValue) {
            var f = obj.getClass().getDeclaredField(name);
            f.setAccessible(true);
            var Field = Java.use('java.lang.reflect.Field');
            Java.cast(f, Field).set.overload('java.lang.Object', 'java.lang.Object').call(f, obj, value);
        }
    }

    function textPart(body) {
        var Text = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessage$TextMessage');
        var Part = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$Message');
        var Nano = Java.use('com.google.protobuf.nano.MessageNano');
        var text = Text.$new();
        setObj(text, 'content', utf8(body));
        var part = Part.$new();
        setInt(part, 'contentType', 0);
        setObj(part, 'data', Nano.toByteArray(text));
        return part;
    }

    function atAllPart() {
        var At = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessage$AtMessage');
        var Part = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$Message');
        var Nano = Java.use('com.google.protobuf.nano.MessageNano');
        var at = At.$new();
        setObj(at, 'name', utf8('所有人 '));
        var part = Part.$new();
        setInt(part, 'contentType', 5);
        setObj(part, 'data', Nano.toByteArray(at));
        return part;
    }

    function richMessage(parts) {
        var Rich = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$RichMessage');
        var rich = Rich.$new();
        var arr = Java.array('com.tencent.wework.foundation.model.pb.WwRichmessageBase$Message', parts);
        setObj(rich, 'messages', arr);
        return rich;
    }

    function decodeProtoEscaped(s) {
        var bytes = [];
        var out = '';
        var i = 0;
        function flush() {
            if (!bytes.length) return;
            var arr = [];
            for (var k = 0; k < bytes.length; k++) arr.push(bytes[k] > 127 ? bytes[k] - 256 : bytes[k]);
            try {
                out += String(Java.use('java.lang.String').$new(Java.array('byte', arr), 'UTF-8'));
            } catch (_) {
                out += String.fromCharCode.apply(null, bytes);
            }
            bytes = [];
        }
        while (i < s.length) {
            var ch = s.charAt(i);
            if (ch === '\\' && /^[0-7]{3}/.test(s.substr(i + 1, 3))) {
                bytes.push(parseInt(s.substr(i + 1, 3), 8));
                i += 4;
                continue;
            }
            if (ch === '\\' && s.charAt(i + 1) === 'u' && /^[0-9a-fA-F]{4}/.test(s.substr(i + 2, 4))) {
                flush();
                out += String.fromCharCode(parseInt(s.substr(i + 2, 4), 16));
                i += 6;
                continue;
            }
            flush();
            out += ch;
            i += 1;
        }
        flush();
        return out;
    }

    function namesOf(conv) {
        var bag = [];
        function add(v) {
            var s = '';
            try { s = decodeProtoEscaped(String(v || '').trim()); } catch (_) { return; }
            if (s && bag.indexOf(s) < 0) bag.push(s);
        }
        try { add(conv.getDefaultName(true)); } catch (_) {}
        try { add(conv.getDefaultName(false)); } catch (_) {}
        try {
            var info = String(conv.getInfo());
            var re = /(?:^|\n)\s*(?:name|roomname_remark|default_name):\s*"((?:\\.|[^"])*)"/g;
            var m;
            while ((m = re.exec(info))) add(m[1].replace(/\\"/g, '"'));
        } catch (_) {}
        return bag;
    }

    function findGroup(list) {
        var exact = null;
        var fuzzy = null;
        var groups = [];
        for (var i = 0; i < list.length; i++) {
            var conv = list[i];
            if (!conv) continue;
            var raw = String(conv);
            var typeMatch = /type:\s*(\d+)/.exec(raw);
            var convType = typeMatch ? parseInt(typeMatch[1], 10) : -1;
            var names = namesOf(conv);
            var infoText = '';
            try { infoText = String(conv.getInfo()); } catch (_) {}
            var hay = names.join('\n') + '\n' + infoText;
            if (convType === 1 || hay.indexOf('测试') >= 0 || hay.indexOf('000') >= 0) {
                groups.push({
                    type: convType,
                    names: names,
                    local: (String(conv).match(/Local:\s*(\d+)/) || ['', ''])[1],
                    info: infoText.slice(0, 4000)
                });
            }
            if (hay.indexOf(targetName) >= 0) {
                var exactHit = false;
                for (var n = 0; n < names.length; n++) {
                    if (names[n] === targetName) exactHit = true;
                }
                if (exactHit) exact = conv;
                else if (!fuzzy) fuzzy = conv;
            }
        }
        return { conv: exact || fuzzy, groups: groups };
    }

    Java.scheduleOnMainThread(function () {
        try {
            var CS = Java.use('com.tencent.wework.foundation.logic.ConversationService');
            var svc = CS.getService();
            var list = svc.GetConversationList();
            if (!list) {
                emit('fail', { stage: 'list', error: 'GetConversationList null' });
                return;
            }
            var found = findGroup(list);
            if (!found.conv) {
                emit('fail', { stage: 'find', error: 'group not found', total: list.length, groups: found.groups.slice(0, 20) });
                return;
            }
            var conv = found.conv;
            emit('found', { conv: String(conv) });

            var SendCb = Java.registerClass({
                name: 'com.testvoice.AtSendCallback' + Date.now(),
                implements: [Java.use('com.tencent.wework.foundation.callback.ISendMessageCallback')],
                methods: {
                    onProgress: function () {},
                    onResult: function (code, convBack, message) {
                        emit('at_result', { code: code, conv: String(convBack || ''), message: String(message || '') });
                    }
                }
            });
            var NoticeCb = Java.registerClass({
                name: 'com.testvoice.NoticeCallback' + Date.now(),
                implements: [Java.use('com.tencent.wework.foundation.callback.IConversationOperateCallback2')],
                methods: {
                    onResult: function (code, text, convBack) {
                        emit('notice_result', { code: code, text: String(text || ''), conv: String(convBack || '') });
                    }
                }
            });
            var sendCb = SendCb.$new();
            var noticeCb = NoticeCb.$new();
            KEEP.push(sendCb);
            KEEP.push(noticeCb);

            var Nano = Java.use('com.google.protobuf.nano.MessageNano');
            var WwMsg = Java.use('com.tencent.wework.foundation.model.pb.WwMessage$Message');
            var Message = Java.use('com.tencent.wework.foundation.model.Message');
            var info = WwMsg.$new();
            setInt(info, 'contentType', 0);
            var atRich = richMessage([atAllPart(), textPart(atText)]);
            var checked = [];
            try {
                var RichCls = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$RichMessage');
                var AtCls = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessage$AtMessage');
                var parsed = RichCls.parseFrom(Java.use('com.google.protobuf.nano.MessageNano').toByteArray(atRich));
                var parts = parsed.messages.value;
                for (var pi = 0; parts && pi < parts.length; pi++) {
                    var ctype = parts[pi].contentType.value;
                    var row = { contentType: ctype };
                    if (Number(ctype) === 5) {
                        var atParsed = AtCls.parseFrom(parts[pi].data.value);
                        row.uin = String(atParsed.uin.value);
                        row.name = String(Java.use('java.lang.String').$new(atParsed.name.value, 'UTF-8'));
                    }
                    checked.push(row);
                }
            } catch (eCheck) {
                checked.push({ error: String(eCheck) });
            }
            emit('at_preview', { parts: checked });
            setObj(info, 'content', Nano.toByteArray(atRich));
            var msg = Message.NewMessage();
            msg.setInfo(info);
            svc.SendMessage.overload(
                'com.tencent.wework.foundation.model.Conversation',
                'com.tencent.wework.foundation.model.Message',
                'com.tencent.wework.foundation.callback.ISendMessageCallback'
            ).call(svc, conv, msg, sendCb);
            emit('at_dispatched', { text: atText });

            if (!skipNotice) {
                var noticeRich = richMessage([textPart(noticeText)]);
                svc.SetGroupNotification(conv, noticeRich, true, noticeCb);
                emit('notice_dispatched', { text: noticeText });
            } else {
                emit('notice_skipped', {});
            }
        } catch (e) {
            emit('fail', { stage: 'send', error: String(e) });
        }
    });
});
"""


def find_pid(adb_serial: str, process_name: str) -> int | None:
    raw = subprocess.check_output(
        [adb_binary(), "-s", adb_serial, "shell", "ps -A -o PID,NAME"],
        stderr=subprocess.STDOUT,
    ).decode("utf-8", "replace")
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == process_name:
            return int(parts[0])
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="向指定群发送一次 @所有人 和一条群公告")
    parser.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    parser.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--at-text", default=DEFAULT_AT_TEXT)
    parser.add_argument("--notice-text", default=DEFAULT_NOTICE_TEXT)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--skip-notice", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    adb = adb_binary()
    subprocess.check_call([adb, "-s", args.adb_serial, "forward", "tcp:27042", "tcp:27042"])
    pid = find_pid(args.adb_serial, args.process_name)
    if pid is None:
        print(f"[send_at_notice_once] 找不到进程 {args.process_name}", flush=True)
        return 1

    js = (
        JS.replace("__TARGET__", json.dumps(args.group, ensure_ascii=False))
        .replace("__AT_TEXT__", json.dumps(args.at_text, ensure_ascii=False))
        .replace("__NOTICE_TEXT__", json.dumps(args.notice_text, ensure_ascii=False))
        .replace("__SKIP_NOTICE__", "true" if args.skip_notice else "false")
    )
    frida = import_frida()
    device = frida.get_device_manager().add_remote_device(args.frida_remote)
    session = device.attach(pid)
    script = session.create_script(js)
    logs: list[dict[str, Any]] = []
    done = {"at": False, "notice": False, "fail": False}

    def on_message(msg: dict[str, Any], _data: Any) -> None:
        if msg.get("type") == "error":
            print("[send_at_notice_once] script_error", flush=True)
            print(msg.get("stack", msg), flush=True)
            done["fail"] = True
            return
        if msg.get("type") != "send":
            return
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            return
        logs.append(payload)
        out = Path("runtime/wework_re_android/artifacts/at_notice_send_log.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=True)[:2000], flush=True)
        event = str(payload.get("event", ""))
        if event == "at_result":
            done["at"] = True
        elif event in {"notice_result", "notice_skipped"}:
            done["notice"] = True
        elif event == "fail":
            done["fail"] = True

    script.on("message", on_message)
    script.load()
    deadline = time.time() + max(5.0, float(args.timeout))
    while time.time() < deadline and not done["fail"] and not (done["at"] and done["notice"]):
        time.sleep(0.2)
    time.sleep(0.4)
    try:
        session.detach()
    except Exception:
        pass
    if done["fail"] or not done["at"] or not done["notice"]:
        return 2
    at_code = next((row.get("code") for row in logs if row.get("event") == "at_result"), None)
    notice_code = next((row.get("code") for row in logs if row.get("event") == "notice_result"), None)
    notice_ok = args.skip_notice or notice_code == 0
    return 0 if at_code == 0 and notice_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
