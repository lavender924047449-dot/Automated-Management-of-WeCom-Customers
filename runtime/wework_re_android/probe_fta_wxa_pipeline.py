"""Step 8.2: attach-only 探针，抓小程序卡片发送链 + thumb 文件时序。

使用方式：
  1) 先手动打开企微并停留在聊天列表
  2) 运行本脚本
  3) 在微信中执行：小程序页面 -> 右上角分享 -> 分享给朋友 -> 选择企微 FTA
  4) 等待脚本超时退出，查看产物：
     - runtime/wework_re_android/artifacts/wxa_pipeline.ndjson
     - app/broadcast/wxa_templates/<template_id>.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import frida

from app.broadcast.wxa_templates import save_template
from runtime.wework_re_android._frida_env import adb_binary

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_FS_POLL_SECONDS = 0.8
DEFAULT_ROOT = "/storage/emulated/0/Android/data/com.tencent.wework/files"
DEFAULT_OUT_NDJSON = "runtime/wework_re_android/artifacts/wxa_pipeline.ndjson"
DEFAULT_FTA_REMOTE_ID = "10006"

JS = r"""
'use strict';
Java.perform(function () {
    var Base64 = Java.use('android.util.Base64');
    var JString = Java.use('java.lang.String');

    function parseConv(convStr) {
        var out = { local: '', remote: '', type: '' };
        var m1 = /Local:\s*(\d+)/.exec(convStr || '');
        var m2 = /remote:\s*(\d+)/.exec(convStr || '');
        var m3 = /type:(\d+)/.exec(convStr || '');
        if (m1) out.local = m1[1];
        if (m2) out.remote = m2[1];
        if (m3) out.type = m3[1];
        return out;
    }

    function parseMType(msgStr) {
        var m = /\|mType\|(\d+)\|/.exec(msgStr || '');
        return m ? parseInt(m[1], 10) : -1;
    }

    function bytesToBase64(bytes) {
        try {
            return String(Base64.encodeToString(bytes, 2));
        } catch (_) {
            return '';
        }
    }

    function bytesToUtf8(bytes) {
        try {
            return String(JString.$new(bytes, 'UTF-8'));
        } catch (_) {
            try {
                return String(JString.$new(bytes));
            } catch (_) {
                return '';
            }
        }
    }

    function primitiveType(t) {
        return t === 'int' || t === 'long' || t === 'float' || t === 'double' || t === 'boolean' ||
               t === 'java.lang.Integer' || t === 'java.lang.Long' || t === 'java.lang.Float' ||
               t === 'java.lang.Double' || t === 'java.lang.Boolean' || t === 'java.lang.String';
    }

    function dumpFlatObject(obj) {
        var out = {};
        if (!obj) return out;
        try {
            var cls = obj.getClass();
            var fields = cls.getDeclaredFields();
            for (var i = 0; i < fields.length; i++) {
                var f = fields[i];
                f.setAccessible(true);
                var n = String(f.getName());
                var t = String(f.getType().getName());
                var v = f.get(obj);
                if (v === null || v === undefined) continue;
                if (t === '[B') {
                    out[n] = { type: t, b64: bytesToBase64(v), utf8: bytesToUtf8(v) };
                } else if (primitiveType(t)) {
                    out[n] = { type: t, value: String(v) };
                } else {
                    out[n] = { type: t, value: String(v) };
                }
            }
        } catch (e) {
            out.__error__ = String(e);
        }
        return out;
    }

    function dumpFileMessage(fm) {
        var out = {};
        if (!fm) return out;
        out.__class__ = String(fm.getClass().getName());
        out.fields = dumpFlatObject(fm);
        return out;
    }

    function dumpMessageInfo(msg) {
        var out = {};
        if (!msg) return out;
        try {
            var info = msg.getInfo();
            if (!info) return out;
            out.__class__ = String(info.getClass().getName());
            out.fields = dumpFlatObject(info);
        } catch (e) {
            out.__error__ = String(e);
        }
        return out;
    }

    function getStack() {
        try {
            var Ex = Java.use('java.lang.Exception');
            var Log = Java.use('android.util.Log');
            return String(Log.getStackTraceString(Ex.$new()));
        } catch (_) {
            return '';
        }
    }

    function emit(event, data) {
        var out = { event: event, tsMs: Date.now() };
        if (data) {
            Object.keys(data).forEach(function (k) { out[k] = data[k]; });
        }
        send(out);
    }

    function hookMethod(klass, method, onCall) {
        try {
            var C = Java.use(klass);
            if (!C[method]) {
                emit('hook_miss', { cls: klass, method: method });
                return;
            }
            C[method].overloads.forEach(function (o, idx) {
                var sig = o.argumentTypes.map(function (t) { return t.className; }).join(',');
                o.implementation = function () {
                    var argv = Array.prototype.slice.call(arguments);
                    try {
                        onCall(sig, idx, argv);
                    } catch (eInner) {
                        emit('hook_handler_error', { cls: klass, method: method, err: String(eInner) });
                    }
                    return o.apply(this, argv);
                };
                emit('hooked', { cls: klass, method: method, idx: idx, sig: sig });
            });
        } catch (e) {
            emit('hook_error', { cls: klass, method: method, err: String(e) });
        }
    }

    hookMethod('h8y', 'V1', function (sig, _idx, argv) {
        emit('call_v1', {
            sig: sig,
            path: String(argv[0] || ''),
            width: String(argv[1] || ''),
            height: String(argv[2] || ''),
            arg3: String(argv[3] || ''),
            arg4: String(argv[4] || ''),
        });
    });

    hookMethod('h8y', 'V8', function (sig, _idx, argv) {
        emit('call_v8', { sig: sig, fileMessage: dumpFileMessage(argv[0]) });
    });

    hookMethod('h8y', 'q8', function (sig, _idx, argv) {
        emit('call_q8', {
            sig: sig,
            convId: String(argv[1] || ''),
            msgTypeArg: String(argv[3] || ''),
            fileMessage: dumpFileMessage(argv[2]),
            stack: getStack(),
        });
    });

    hookMethod('h8y', 'f2', function (sig, _idx, argv) {
        emit('call_f2', {
            sig: sig,
            link: String(argv[0] || ''),
            title: String(argv[1] || ''),
            desc: String(argv[2] || ''),
            imgUrl: String(argv[3] || ''),
        });
    });

    try {
        var CS = Java.use('com.tencent.wework.foundation.logic.ConversationService');
        var sm0 = CS.SendMessage.overload(
            'com.tencent.wework.foundation.model.Conversation',
            'com.tencent.wework.foundation.model.Message',
            'com.tencent.wework.foundation.callback.ISendMessageCallback'
        );
        sm0.implementation = function (conv, msg, cb) {
            var convStr = String(conv || '');
            var msgStr = String(msg || '');
            emit('send_message', {
                mType: parseMType(msgStr),
                conv: parseConv(convStr),
                convRaw: convStr,
                msgRaw: msgStr,
                messageInfoSnapshot: dumpMessageInfo(msg),
            });
            return sm0.call(this, conv, msg, cb);
        };
        emit('hooked', { cls: 'ConversationService', method: 'SendMessage(Conversation,Message,Callback)' });
    } catch (e0) {
        emit('hook_error', { cls: 'ConversationService', method: 'SendMessage#0', err: String(e0) });
    }

    try {
        var CS2 = Java.use('com.tencent.wework.foundation.logic.ConversationService');
        var sm1 = CS2.SendMessage.overload(
            'com.tencent.wework.foundation.model.ConversationRef',
            'com.tencent.wework.foundation.model.Message',
            'com.tencent.wework.foundation.callback.ISendMessageCallback'
        );
        sm1.implementation = function (convRef, msg, cb) {
            var refStr = String(convRef || '');
            var msgStr = String(msg || '');
            emit('send_message_ref', {
                mType: parseMType(msgStr),
                convRefRaw: refStr,
                msgRaw: msgStr,
                messageInfoSnapshot: dumpMessageInfo(msg),
            });
            return sm1.call(this, convRef, msg, cb);
        };
        emit('hooked', { cls: 'ConversationService', method: 'SendMessage(ConversationRef,Message,Callback)' });
    } catch (e1) {
        emit('hook_error', { cls: 'ConversationService', method: 'SendMessage#1', err: String(e1) });
    }

    emit('ready', { msg: 'wxa probe hooks installed' });
});
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe FTA wxa card pipeline")
    p.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    p.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    p.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    p.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--fs-poll-seconds", type=float, default=DEFAULT_FS_POLL_SECONDS)
    p.add_argument("--wework-files-root", default=DEFAULT_ROOT)
    p.add_argument("--out-ndjson", default=DEFAULT_OUT_NDJSON)
    p.add_argument("--fta-remote-id", default=DEFAULT_FTA_REMOTE_ID)
    p.add_argument("--account-id", default=None, help="按账号隔离；缺省走当前活动账号")
    p.add_argument(
        "--commit",
        action="store_true",
        help="直接落已确认目录（危险：绕过 GUI 命名确认）。默认写 _pending/ 待 GUI 命名后 commit。",
    )
    return p.parse_args()


def find_pid(adb_serial: str, process_name: str) -> int | None:
    raw = subprocess.check_output([adb_binary(), "-s", adb_serial, "shell", "ps -A -o PID,NAME"]).decode("ascii", "replace")
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == process_name:
            return int(parts[0])
    return None


def poll_thumb_fs(adb_serial: str, base_root: str) -> dict[str, int]:
    dirs = [
        f"{base_root}/tempimagecache",
        f"{base_root}/uploadTempMidbimage",
        f"{base_root}/uploadTempThumbimage",
    ]
    out: dict[str, int] = {}
    for d in dirs:
        try:
            raw = subprocess.check_output(
                [adb_binary(), "-s", adb_serial, "shell", "ls", "-l", d],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "replace")
        except subprocess.CalledProcessError:
            continue
        for line in raw.splitlines():
            row = line.strip()
            if not row or row.startswith("total"):
                continue
            parts = row.split()
            if len(parts) < 8 or not parts[0].startswith("-"):
                continue
            try:
                size = int(parts[4])
            except ValueError:
                continue
            out[f"{d}/{parts[-1]}"] = size
    return out


def _event_key(payload: dict[str, Any]) -> str:
    conv = payload.get("conv", {}) if isinstance(payload.get("conv"), dict) else {}
    conv_local = str(conv.get("local", "")).strip()
    mtype = str(payload.get("mType", ""))
    raw = str(payload.get("msgRaw", ""))
    lid = ""
    if "|lId|" in raw:
        try:
            lid = raw.split("|lId|", 1)[1].split("|", 1)[0].strip()
        except Exception:
            lid = ""
    return f"{conv_local}:{mtype}:{lid}"


def build_template(payload: dict[str, Any]) -> dict[str, Any]:
    conv = payload.get("conv", {}) if isinstance(payload.get("conv"), dict) else {}
    conv_local = str(conv.get("local", "")).strip()
    conv_remote = str(conv.get("remote", "")).strip()
    return {
        "template_id": str(uuid.uuid4()),
        "msgType": int(payload.get("mType", -1) or -1),
        "convLocal": conv_local,
        "convRemote": conv_remote,
        "captured_from": {
            "event": payload.get("event", ""),
            "convRaw": payload.get("convRaw", ""),
            "msgRaw": payload.get("msgRaw", ""),
        },
        "messageInfoSnapshot": payload.get("messageInfoSnapshot", {}),
    }


def main() -> int:
    args = parse_args()
    pid = find_pid(args.adb_serial, args.process_name)
    if pid is None:
        print(f"[probe_wxa] process not found: {args.process_name}")
        return 1

    out_path = Path(args.out_ndjson)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_f = out_path.open("w", encoding="utf-8")
    templates: list[Path] = []
    saved_keys: set[str] = set()

    def emit_local(event: dict[str, Any]) -> None:
        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
        out_f.flush()
        print("[probe_wxa]", json.dumps(event, ensure_ascii=False))

    dev = frida.get_device_manager().add_remote_device(args.frida_remote)
    sess = dev.attach(pid)
    has_error = {"flag": False}

    def on_message(msg: dict[str, Any], _data: Any) -> None:
        if msg.get("type") == "error":
            has_error["flag"] = True
            emit_local({"event": "script_error", "tsMs": int(time.time() * 1000), "stack": msg.get("stack", str(msg))})
            return
        if msg.get("type") != "send":
            return
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            return
        emit_local(payload)
        if payload.get("event") != "send_message":
            return
        conv = payload.get("conv", {}) if isinstance(payload.get("conv"), dict) else {}
        if str(conv.get("remote", "")).strip() != str(args.fta_remote_id).strip():
            return
        key = _event_key(payload)
        if key in saved_keys:
            return
        tpl = build_template(payload)
        tpl_path = save_template(
            tpl,
            template_id=tpl["template_id"],
            account_id=args.account_id,
            pending=not args.commit,
        )
        templates.append(tpl_path)
        saved_keys.add(key)
        emit_local(
            {
                "event": "template_saved",
                "template_id": tpl["template_id"],
                "path": str(tpl_path),
                "msgType": tpl["msgType"],
                "convLocal": tpl["convLocal"],
            }
        )

    sc = sess.create_script(JS)
    sc.on("message", on_message)
    sc.load()

    emit_local(
        {
            "event": "probe_start",
            "tsMs": int(time.time() * 1000),
            "pid": pid,
            "timeoutSeconds": args.timeout_seconds,
            "fsPollSeconds": args.fs_poll_seconds,
            "outNdjson": str(out_path),
            "ftaRemoteId": args.fta_remote_id,
        }
    )
    print("[probe_wxa] now manually share ONE mini-program card to FTA")

    prev = poll_thumb_fs(args.adb_serial, args.wework_files_root)
    deadline = time.time() + args.timeout_seconds
    while time.time() < deadline and not has_error["flag"]:
        time.sleep(args.fs_poll_seconds)
        cur = poll_thumb_fs(args.adb_serial, args.wework_files_root)
        for p, size in cur.items():
            old = prev.get(p)
            if old is None:
                emit_local({"event": "fs_new", "tsMs": int(time.time() * 1000), "path": p, "size": size})
            elif old != size:
                emit_local(
                    {
                        "event": "fs_size_changed",
                        "tsMs": int(time.time() * 1000),
                        "path": p,
                        "oldSize": old,
                        "newSize": size,
                    }
                )
        for p in prev.keys() - cur.keys():
            emit_local({"event": "fs_gone", "tsMs": int(time.time() * 1000), "path": p})
        prev = cur

    sess.detach()
    out_f.close()
    if templates:
        print(f"[probe_wxa] saved templates: {', '.join(str(p) for p in templates)}")
    else:
        print("[probe_wxa] no template captured")
    print(f"[probe_wxa] done. artifacts: {out_path}")
    return 2 if has_error["flag"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
