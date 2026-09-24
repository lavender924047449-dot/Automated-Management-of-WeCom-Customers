"""列出企微本地缓存的全部好友（含未开过对话的）。

**已验证 API**（2026-09-17，5.0.10）：
    ContactService.GetCachedContactList()
        → 无参、同步、返回 User 数组
        → 每个 User 可读 getRemoteId / getName / getRealRemark / getDisplayName / getCorpId

**覆盖面**：企微内部维护的本地好友缓存。命中数 = 企微当前进程 cache 里已加载的好友数量。
若为空，请先在 MuMu 里点开一次企微「通讯录」页触发拉取，再重跑本脚本。

输出：stdout 逐行 `[USER] remote=... name='...' remark='...' corpId=...`
      配合 `--out` 落 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from app.config import resolve_path
from app.bootstrap.win_subprocess import check_output_hidden
from runtime.wework_re_android._frida_env import adb_binary, import_frida

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_TIMEOUT_SECONDS = 8.0

JS = r"""
'use strict';
Java.perform(function () {
    function safe(fn) { try { return fn(); } catch (e) { return null; } }
    function s(v) { if (v === null || v === undefined) return ''; try { return String(v); } catch (_) { return ''; } }

    function snap(u) {
        return {
            remoteId: safe(function () { return s(u.getRemoteId()); }),
            name: safe(function () { return s(u.getName()); }),
            zhName: safe(function () { return s(u.getZhName()); }),
            englishName: safe(function () { return s(u.getEnglishName()); }),
            realRemark: safe(function () { return s(u.getRealRemark()); }),
            displayName: safe(function () { return s(u.getDisplayName()); }),
            corpId: safe(function () { return s(u.getCorpId()); }),
            corpName: safe(function () { return s(u.getCorpName()); }),
            isWeixinXidUser: safe(function () { return s(u.isWeixinXidUser()); })
        };
    }

    try {
        var CS = Java.use('com.tencent.wework.foundation.logic.ContactService').getService();
        var arr = CS.GetCachedContactList();
        var n = arr ? arr.length : 0;
        send({t: 'total', n: n});
        for (var i = 0; i < n; i++) {
            var u = arr[i];
            if (!u) continue;
            send({t: 'user', i: i, data: snap(u)});
        }
        send({t: 'done'});
    } catch (e) {
        send({t: 'err', msg: s(e)});
    }
});
"""


def find_pid(adb_serial: str, process_name: str) -> int | None:
    raw = check_output_hidden(
        [adb_binary(), "-s", adb_serial, "shell", "ps -A -o PID,NAME"]
    ).decode("ascii", "replace")
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == process_name:
            return int(parts[0])
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="List cached wework contacts (含未开过对话的好友)")
    p.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    p.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    p.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    p.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--out", default="", help="额外落一份 JSON")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    pid = find_pid(args.adb_serial, args.process_name)
    if pid is None:
        print(f"[all_contacts] process not found: {args.process_name}", file=sys.stderr)
        return 1

    frida = import_frida()
    dev = frida.get_device_manager().add_remote_device(args.frida_remote)
    sess = dev.attach(pid)
    script = sess.create_script(JS)

    users: list[dict[str, Any]] = []
    done = {"flag": False}
    total = {"n": -1}
    err = {"msg": ""}

    def on_msg(msg: dict[str, Any], _data: Any) -> None:
        if msg.get("type") == "error":
            err["msg"] = str(msg.get("stack") or msg)
            done["flag"] = True
            return
        if msg.get("type") != "send":
            return
        p = msg.get("payload") or {}
        t = p.get("t")
        if t == "total":
            total["n"] = int(p.get("n", 0))
            print(f"[total] {total['n']}", file=sys.stderr)
        elif t == "user":
            data = p.get("data") or {}
            users.append(data)
            print(
                f"[USER] remote={data.get('remoteId','')} name={data.get('name','')!r} "
                f"remark={data.get('realRemark','')!r} corpName={data.get('corpName','')!r}"
            )
        elif t == "done":
            done["flag"] = True
        elif t == "err":
            err["msg"] = str(p.get("msg", ""))
            done["flag"] = True

    script.on("message", on_msg)
    script.load()

    deadline = time.time() + max(1.0, args.timeout_seconds)
    while not done["flag"] and time.time() < deadline:
        time.sleep(0.1)
    try:
        sess.detach()
    except Exception:
        pass

    if err["msg"]:
        print(f"[err] {err['msg']}", file=sys.stderr)
        return 2

    if args.out:
        out_path = resolve_path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({"total": len(users), "users": users}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return 0 if users else 3


if __name__ == "__main__":
    raise SystemExit(main())
