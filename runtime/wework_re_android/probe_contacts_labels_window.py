"""60 秒 attach-only 观测窗：通讯录缓存 + 「我的客户 → 标签」页面。

只做三件事：
1. 列出 ContactService / UserLabelService 上已有的方法名（不调用未验证方法）。
2. 读一次已经验证过的同步接口：GetCachedContactList、GetAllLabelIdsHasContact。
3. 在窗口内记录 Activity.onResume，方便对照用户点开的页面。

不调用 GetCacheLabelFullInfoById，也不猜测 SyncContactCache。
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
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_OUT = "runtime/wework_re_android/artifacts/contacts_labels_window.ndjson"

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

    function safe(fn) {
        try { return fn(); } catch (e) { return null; }
    }

    function methodNames(className) {
        var cls = Java.use(className);
        var methods = cls.class.getDeclaredMethods();
        var names = [];
        var seen = {};
        for (var i = 0; i < methods.length; i++) {
            var name = String(methods[i].getName());
            if (seen[name]) continue;
            seen[name] = true;
            names.push(name);
        }
        names.sort();
        return names;
    }

    function snapshot(tag) {
        var row = { tag: tag, cachedContacts: -1, labelIds: -1 };
        var CS = safe(function () {
            return Java.use('com.tencent.wework.foundation.logic.ContactService').getService();
        });
        if (!CS) {
            emit('snapshot', row);
            return;
        }
        var cached = safe(function () { return CS.GetCachedContactList(); });
        row.cachedContacts = cached ? cached.length : 0;
        var ids = safe(function () { return CS.GetAllLabelIdsHasContact(1); });
        row.labelIds = ids ? ids.length : 0;
        emit('snapshot', row);
    }

    ['com.tencent.wework.foundation.logic.ContactService',
     'com.tencent.wework.foundation.logic.UserLabelService'].forEach(function (cls) {
        try {
            emit('methods', { cls: cls, names: methodNames(cls) });
        } catch (e) {
            emit('methods_error', { cls: cls, error: String(e) });
        }
    });

    snapshot('open');

    var Activity = Java.use('android.app.Activity');
    var orig = Activity.onResume;
    orig.implementation = function () {
        var name = '';
        try { name = String(this.getClass().getName()); } catch (_) {}
        if (name.indexOf('tencent.wework') >= 0) {
            emit('resume', { activity: name });
        }
        return orig.call(this);
    };

    emit('window_open', { seconds: __WINDOW__ });
});
""".replace("__WINDOW__", str(int(DEFAULT_WINDOW_SECONDS)))


def find_pid(adb_serial: str, process_name: str) -> int | None:
    raw = subprocess.check_output(
        [adb_binary(), "-s", adb_serial, "shell", "pidof", process_name],
        stderr=subprocess.STDOUT,
    ).decode("utf-8", "replace")
    text = raw.strip().split()
    if not text:
        return None
    try:
        return int(text[0])
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="60s attach-only contacts/labels observation window")
    parser.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    parser.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    pid = find_pid(args.adb_serial, args.process_name)
    if pid is None:
        print(f"[probe] process not found: {args.process_name}")
        return 1

    frida = import_frida()
    device = frida.get_device_manager().add_remote_device(args.frida_remote)
    session = device.attach(pid)
    script = session.create_script(JS.replace("__WINDOW__", str(int(args.window_seconds))))
    logs: list[dict[str, Any]] = []

    def on_message(msg: dict[str, Any], _data: Any) -> None:
        if msg.get("type") == "error":
            row = {"event": "script_error", "stack": msg.get("stack", str(msg)), "tsMs": int(time.time() * 1000)}
            logs.append(row)
            print(json.dumps(row, ensure_ascii=False))
            return
        if msg.get("type") != "send":
            return
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return
        logs.append(payload)
        event = str(payload.get("event", ""))
        if event == "methods":
            names = payload.get("names") or []
            interesting = [
                n for n in names
                if any(k in str(n) for k in ("Label", "Contact", "Sync", "Fetch", "Cache", "Refresh"))
            ]
            print(json.dumps({
                "event": "methods",
                "cls": payload.get("cls"),
                "interesting": interesting,
                "total": len(names),
            }, ensure_ascii=False))
            return
        if event == "resume":
            print(json.dumps(payload, ensure_ascii=False))
            return
        print(json.dumps(payload, ensure_ascii=False))

    script.on("message", on_message)
    script.load()
    print(f"[probe] WINDOW_OPEN pid={pid} seconds={args.window_seconds}", flush=True)
    time.sleep(max(1.0, float(args.window_seconds)))
    session.detach()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in logs:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[probe] WINDOW_CLOSED events={len(logs)} out={out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
