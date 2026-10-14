"""诊断：把隐藏单聊取消隐藏并写入界面会话缓存。不发消息。"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from runtime.wework_re_android._frida_env import import_frida

ADB_SERIAL = "127.0.0.1:16384"
FRIDA_REMOTE = "127.0.0.1:27042"
PROCESS = "com.tencent.wework"
TARGET_REMOTE = "7881300363276969"

JS = r"""
'use strict';
Java.perform(function () {
    var target = '__REMOTE__';
    function safe(fn) { try { return fn(); } catch (e) { return null; } }
    var api = null;

    var contacts = Java.use('com.tencent.wework.foundation.logic.ContactService').getService();
    var arr = contacts.GetCachedContactList();
    var user = null;
    var n = arr ? arr.length : 0;
    for (var i = 0; i < n; i++) {
        var u = arr[i];
        if (!u) continue;
        var remote = safe(function () { return String(u.getRemoteId()); });
        if (remote === target) {
            user = u;
            break;
        }
    }
    if (!user) {
        send({t: 'miss', cached: n});
        return;
    }
    send({t: 'found', remote: target, name: safe(function () { return String(user.getDisplayName()); })});

    var CB = Java.registerClass({
        name: 'com.cursor.CreateConvCB' + Date.now(),
        implements: [Java.use('com.tencent.wework.foundation.callback.ICommonConversationOperateCallback')],
        methods: {
            onResult: function (code, conv, errMsg) {
                var state = {};
                try {
                    if (conv) {
                        var svc = Java.use('com.tencent.wework.foundation.logic.ConversationService').getService();
                        var info = conv.getInfo();
                        var hidden = info.getClass().getDeclaredField('hidden');
                        hidden.setAccessible(true);
                        state.hiddenBefore = String(hidden.get(info));
                        hidden.set(info, Java.use('java.lang.Boolean').valueOf(false));
                        conv.setInfo(info);
                        svc.SaveConversation(conv);
                        var info2 = conv.getInfo();
                        state.hiddenAfter = String(hidden.get(info2));
                        state.show = svc.ShouldShowInList(conv, false);
                        var idField = info2.getClass().getDeclaredField('id');
                        idField.setAccessible(true);
                        var localRaw = idField.get(info2);
                        state.local = String(localRaw);
                        var convId = new Int64(String(localRaw));
                        state.api = api ? true : false;
                        if (api) {
                            var impl = Java.cast(api, Java.use('com.tencent.wework.msg.temp.ConversationApiImpl'));
                            var convArr = Java.array('com.tencent.wework.foundation.model.Conversation', [conv]);
                            impl.updateConversationCache.overload('[Lcom.tencent.wework.foundation.model.Conversation;', 'boolean').call(impl, convArr, true);
                            state.cacheUpdated = true;
                            var item = impl.getConversationItem.overload('long').call(impl, convId);
                            state.item = item ? String(item) : '';
                        }
                    }
                } catch (e) {
                    state.err = String(e);
                }
                send({
                    t: 'result',
                    code: code,
                    err: errMsg ? String(errMsg) : '',
                    conv: conv ? String(conv) : '',
                    state: state
                });
            }
        }
    });

    function startCreate() {
        Java.scheduleOnMainThread(function () {
            try {
                var svc = Java.use('com.tencent.wework.foundation.logic.ConversationService').getService();
                var users = Java.array('com.tencent.wework.foundation.model.User', [user]);
                svc.CreateConversation.overload(
                    'java.lang.String',
                    '[Lcom.tencent.wework.foundation.model.User;',
                    'com.tencent.wework.foundation.model.Conversation',
                    'com.tencent.wework.foundation.callback.ICommonConversationOperateCallback'
                ).call(svc, '', users, null, CB.$new());
                send({t: 'called'});
            } catch (e) {
                send({t: 'call_err', msg: String(e)});
            }
        });
    }
    Java.choose('com.tencent.wework.msg.temp.ConversationApiImpl', {
        onMatch: function (inst) { if (!api) api = inst; },
        onComplete: function () {
            send({t: 'api', ok: api ? true : false});
            startCreate();
        }
    });
});
""".replace("__REMOTE__", TARGET_REMOTE)


def find_pid() -> int:
    raw = subprocess.check_output(
        ["adb", "-s", ADB_SERIAL, "shell", "ps -A -o PID,NAME"],
        text=True,
        errors="replace",
    )
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == PROCESS:
            return int(parts[0])
    raise SystemExit("no wework pid")


def main() -> int:
    frida = import_frida()
    pid = find_pid()
    device = frida.get_device_manager().add_remote_device(FRIDA_REMOTE)
    session = device.attach(pid)
    script = session.create_script(JS)
    rows: list[dict] = []

    def on_message(message, _data) -> None:
        if message.get("type") == "send":
            payload = message.get("payload") or {}
            rows.append(payload)
            print(json.dumps(payload, ensure_ascii=False), flush=True)
        elif message.get("type") == "error":
            print(message.get("stack") or message, file=sys.stderr, flush=True)

    script.on("message", on_message)
    script.load()
    time.sleep(12)
    session.detach()
    return 0 if any(row.get("t") == "result" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
