"""观察 @所有人 和群公告的真实函数入口。只挂观察，不主动发送。

用法（企微保持前台，探针起来后再手动操作）:
  python -m runtime.wework_re_android.probe_at_notice --timeout 180

需要你手动做两件事:
  1. 在测试群输入框打 @，点「所有人」，发一条短消息。
  2. 打开同一群的群详情 → 群公告 → 写一句短公告 → 发布。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from runtime.wework_re_android._frida_env import adb_binary, import_frida

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_OUT_JSON = "runtime/wework_re_android/artifacts/at_notice_capture.json"

JS = r"""
'use strict';
Java.perform(function () {
    function emit(event, payload) {
        var out = { event: event, tsMs: Date.now() };
        if (payload) {
            Object.keys(payload).forEach(function (k) { out[k] = payload[k]; });
        }
        send(out);
    }

    function safe(v) {
        try {
            if (v === null || v === undefined) return '';
            var s = String(v);
            if (s.length > 1200) return s.slice(0, 1200);
            return s;
        } catch (e) {
            return '<err>';
        }
    }

    function field(obj, name) {
        if (!obj) return null;
        try {
            var box = obj[name];
            if (box !== null && box !== undefined && box.value !== undefined) return box.value;
            return box;
        } catch (_) {
            return null;
        }
    }

    function utf8(bytes) {
        if (!bytes) return '';
        try {
            return Java.use('java.lang.String').$new(bytes, 'UTF-8').toString();
        } catch (_) {
            return '';
        }
    }

    function parseRichParts(rich) {
        var parts = [];
        var At = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessage$AtMessage');
        var Text = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessage$TextMessage');
        var messages = field(rich, 'messages');
        if (!messages) return parts;
        for (var i = 0; i < messages.length; i++) {
            var part = messages[i];
            if (!part) continue;
            var ctype = field(part, 'contentType');
            var data = field(part, 'data');
            var row = { index: i, contentType: ctype };
            if (Number(ctype) === 5 && data) {
                var at = At.parseFrom(data);
                row.uin = safe(field(at, 'uin'));
                row.name = utf8(field(at, 'name'));
            } else if (data) {
                try {
                    var text = Text.parseFrom(data);
                    row.text = utf8(field(text, 'content'));
                } catch (_) {
                    row.dataLen = data.length;
                }
            }
            parts.push(row);
        }
        return parts;
    }

    function parseRich(contentOrObj) {
        if (!contentOrObj) return [];
        try {
            var className = '';
            try { className = String(contentOrObj.getClass().getName()); } catch (_) {}
            if (className.indexOf('RichMessage') >= 0) {
                return parseRichParts(contentOrObj);
            }
            var Rich = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$RichMessage');
            return parseRichParts(Rich.parseFrom(contentOrObj));
        } catch (e) {
            return [{ error: safe(e) }];
        }
    }

    function dumpMessage(msg) {
        var info = null;
        try { info = msg.getInfo(); } catch (_) {}
        var contentType = info ? field(info, 'contentType') : null;
        var content = info ? field(info, 'content') : null;
        var row = {
            preview: safe(msg),
            contentType: contentType
        };
        if (Number(contentType) === 0 || Number(contentType) === 2) {
            row.parts = parseRich(content);
        }
        return row;
    }

    function dumpNotice(roomDoc) {
        var items = [];
        try {
            var arr = field(roomDoc, 'items');
            if (arr) {
                for (var i = 0; i < arr.length; i++) {
                    var item = arr[i];
                    if (!item) continue;
                    items.push({
                        type: field(item, 'type'),
                        text: safe(field(item, 'text')),
                        url: safe(field(item, 'url')),
                        mentionFromVid: safe(field(item, 'mentionFromVid')),
                        mentionToVid: safe(field(item, 'mentionToVid'))
                    });
                }
            }
        } catch (e) {
            items.push({ error: safe(e) });
        }
        return {
            senderVid: safe(field(roomDoc, 'senderVid')),
            items: items
        };
    }

    function hookNamed(className, methodName, dumper) {
        var cls;
        try {
            cls = Java.use(className);
        } catch (e) {
            emit('hook_miss', { className: className, methodName: methodName, error: safe(e) });
            return;
        }
        if (!cls[methodName] || !cls[methodName].overloads) {
            emit('hook_miss', { className: className, methodName: methodName, error: 'no method' });
            return;
        }
        var overs = cls[methodName].overloads;
        for (var i = 0; i < overs.length; i++) {
            (function (ov) {
                var sig = '';
                try {
                    sig = ov.argumentTypes.map(function (t) { return t.className; }).join(', ');
                } catch (_) {}
                ov.implementation = function () {
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    try {
                        dumper(methodName, sig, argv);
                    } catch (eDump) {
                        emit('dump_error', { methodName: methodName, error: safe(eDump) });
                    }
                    return ov.apply(this, argv);
                };
            })(overs[i]);
        }
        emit('hook_ok', { className: className, methodName: methodName, overloads: overs.length });
    }

    var CS = 'com.tencent.wework.foundation.logic.ConversationService';

    hookNamed(CS, 'SendMessage', function (methodName, sig, argv) {
        emit('send_message', {
            method: methodName,
            sig: sig,
            conv: safe(argv[0]),
            message: dumpMessage(argv[1])
        });
    });

    hookNamed(CS, 'SendMessagesSerial', function (methodName, sig, argv) {
        var dumped = [];
        try {
            var arr = argv[1];
            if (arr) {
                for (var i = 0; i < arr.length && i < 8; i++) dumped.push(dumpMessage(arr[i]));
            }
        } catch (_) {}
        emit('send_messages_serial', {
            method: methodName,
            sig: sig,
            conv: safe(argv[0]),
            messages: dumped
        });
    });

    hookNamed(CS, 'SetGroupNotification', function (methodName, sig, argv) {
        emit('set_group_notification', {
            method: methodName,
            sig: sig,
            conv: safe(argv[0]),
            rich: parseRich(argv[1]),
            flag: safe(argv[2])
        });
    });

    hookNamed(CS, 'SetGroupNotificationWithTicket', function (methodName, sig, argv) {
        emit('set_group_notification_ticket', {
            method: methodName,
            sig: sig,
            conv: safe(argv[0]),
            rich: parseRich(argv[1]),
            flag: safe(argv[2])
        });
    });

    hookNamed(CS, 'SetNewGroupNotification', function (methodName, sig, argv) {
        emit('set_new_group_notification', {
            method: methodName,
            sig: sig,
            conv: safe(argv[0]),
            notice: dumpNotice(argv[1]),
            flag: safe(argv[2])
        });
    });

    emit('ready', { note: 'hooks installed' });
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
    parser = argparse.ArgumentParser(description="观察企微 @所有人 与群公告发布入口")
    parser.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    parser.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--out", default=DEFAULT_OUT_JSON)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pid = find_pid(args.adb_serial, args.process_name)
    if pid is None:
        print(f"[probe_at_notice] 找不到进程 {args.process_name}，请先打开企微")
        return 1

    frida = import_frida()
    device = frida.get_device_manager().add_remote_device(args.frida_remote)
    session = device.attach(pid)
    script = session.create_script(JS)
    logs: list[dict[str, Any]] = []

    def on_message(msg: dict[str, Any], _data: Any) -> None:
        if msg.get("type") == "error":
            row = {"event": "script_error", "stack": msg.get("stack", str(msg)), "tsMs": int(time.time() * 1000)}
            logs.append(row)
            print("[probe_at_notice] script_error")
            print(msg.get("stack", msg))
            return
        if msg.get("type") != "send":
            return
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            return
        logs.append(payload)
        event = str(payload.get("event", ""))
        print(f"[probe_at_notice] {event}", flush=True)
        if event not in {"hook_ok", "hook_miss", "ready"}:
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)

    script.on("message", on_message)
    script.load()
    print("[probe_at_notice] 已挂上。请现在手动：1) @所有人并发一条短消息  2) 发布一条短群公告", flush=True)
    deadline = time.time() + max(1.0, float(args.timeout))
    try:
        while time.time() < deadline:
            time.sleep(0.4)
    finally:
        try:
            session.detach()
        except Exception:
            pass

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"pid": pid, "logs": logs, "collected_at": int(time.time() * 1000)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    interesting = [row for row in logs if str(row.get("event", "")).startswith(("send_", "set_"))]
    print(f"[probe_at_notice] 写入 {out}，命中 {len(interesting)} 条发送/公告")
    return 0 if interesting else 2


if __name__ == "__main__":
    raise SystemExit(main())
