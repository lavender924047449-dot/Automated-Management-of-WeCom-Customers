"""只读列出 ConversationService 的方法签名，不调用未验证方法。"""

from __future__ import annotations

import json
import subprocess
import sys

from runtime.wework_re_android._frida_env import import_frida

ADB_SERIAL = "127.0.0.1:16384"
FRIDA_REMOTE = "127.0.0.1:27042"
PROCESS = "com.tencent.wework"

JS = r"""
'use strict';
Java.perform(function () {
    var names = [
        'com.tencent.wework.foundation.callback.ICommonConversationOperateCallback',
        'com.tencent.wework.foundation.callback.ICreateConversationCallback'
    ];
    names.forEach(function (className) {
        try {
            var cls = Java.use(className);
            var methods = cls.class.getDeclaredMethods();
            var rows = [];
            for (var i = 0; i < methods.length; i++) {
                rows.push(String(methods[i].toString()));
            }
            rows.sort();
            send({cls: className, ok: true, methods: rows});
        } catch (e) {
            send({cls: className, ok: false, error: String(e)});
        }
    });
});
"""


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
    found: list[dict] = []

    def on_message(message, _data) -> None:
        if message.get("type") == "send":
            found.append(message.get("payload") or {})
        elif message.get("type") == "error":
            print(message.get("stack") or message, file=sys.stderr)

    script.on("message", on_message)
    script.load()
    # Java.perform 是同步把结果 send 出来的，稍等消息泵即可。
    import time

    time.sleep(2.5)
    session.detach()
    keywords = ("create", "obtain", "conv", "session", "remote", "user", "chat")
    for row in found:
        methods = row.get("methods") or []
        picked = [
            item
            for item in methods
            if any(word in item.lower() for word in keywords)
        ]
        show = methods if len(methods) <= 40 else picked
        print(json.dumps({"cls": row.get("cls"), "ok": row.get("ok"), "error": row.get("error"), "count": len(methods), "methods": show}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
