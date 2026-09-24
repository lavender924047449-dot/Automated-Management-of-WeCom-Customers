"""Step 12.1: 探测当前企微账号身份（vid/gid）。"""

from __future__ import annotations

import argparse
import codecs
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app.broadcast.paths import DEFAULT_ACCOUNT_ID, normalize_account_id
from runtime.wework_re_android._frida_env import adb_binary, import_frida

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_OUT_JSON = "runtime/wework_re_android/artifacts/account_identity.json"

JS = r"""
'use strict';
Java.perform(function () {
    var MAX_VAL_LEN = 320;

    function emit(event, payload) {
        var out = { event: event, tsMs: Date.now() };
        if (payload) {
            Object.keys(payload).forEach(function (k) { out[k] = payload[k]; });
        }
        send(out);
    }

    function safeStr(v) {
        if (v === null || v === undefined) return '';
        try {
            var s = String(v);
            if (s.length > MAX_VAL_LEN) return s.slice(0, MAX_VAL_LEN) + '...';
            return s;
        } catch (_) {
            return '<toString_error>';
        }
    }

    function inspectFields(obj, depth, outRows) {
        if (!obj || depth > 1) return;
        try {
            var cls = obj.getClass();
            var fields = cls.getDeclaredFields();
            for (var i = 0; i < fields.length && i < 40; i++) {
                var f = fields[i];
                f.setAccessible(true);
                var name = safeStr(f.getName());
                if (!name) continue;
                var value = null;
                try { value = f.get(obj); } catch (_) { continue; }
                if (value === null || value === undefined) continue;
                outRows.push({ key: name, value: safeStr(value), source: safeStr(cls.getName()) });
                var low = name.toLowerCase();
                var shouldDive =
                    depth === 0 &&
                    (low.indexOf('user') >= 0 || low.indexOf('profile') >= 0 || low.indexOf('login') >= 0);
                if (shouldDive) {
                    inspectFields(value, depth + 1, outRows);
                }
            }
        } catch (_) {}
    }

    function callNoArgMethod(holder, methodName, isStatic, className, outRows) {
        if (!holder || !holder[methodName] || !holder[methodName].overloads) return;
        var ovs = holder[methodName].overloads;
        for (var i = 0; i < ovs.length; i++) {
            var ov = ovs[i];
            if (ov.argumentTypes.length !== 0) continue;
            try {
                var ret = ov.call(holder);
                if (ret === null || ret === undefined) continue;
                var key = className + '#' + methodName + (isStatic ? '[static]' : '[inst]');
                outRows.push({ key: key, value: safeStr(ret), source: className });
                inspectFields(ret, 0, outRows);
            } catch (_) {}
        }
    }

    function snapshotClass(className) {
        var outRows = [];
        try {
            var C = Java.use(className);
            var staticMethods = [
                'getService', 'getInstance', 'instance',
                'getCurrentUser', 'getLoginUser', 'currentUser',
                'getProfile', 'getUserProfile'
            ];
            for (var i = 0; i < staticMethods.length; i++) {
                callNoArgMethod(C, staticMethods[i], true, className, outRows);
            }

            var svc = null;
            try {
                if (C.getService && C.getService.overloads.length > 0) {
                    svc = C.getService();
                } else if (C.getInstance && C.getInstance.overloads.length > 0) {
                    svc = C.getInstance();
                }
            } catch (_) {}
            if (svc) {
                inspectFields(svc, 0, outRows);
            }
        } catch (_) {}
        return outRows;
    }

    function hookClass(className) {
        var hooked = 0;
        try {
            var C = Java.use(className);
            var names = ['getCurrentUser', 'getLoginUser', 'getProfile', 'getUserProfile'];
            for (var ni = 0; ni < names.length; ni++) {
                var name = names[ni];
                if (!C[name] || !C[name].overloads) continue;
                var ovs = C[name].overloads;
                for (var oi = 0; oi < ovs.length; oi++) {
                    var ov = ovs[oi];
                    if (ov.argumentTypes.length !== 0) continue;
                    (function (overload, cName, mName, idx) {
                        overload.implementation = function () {
                            var ret = overload.apply(this, arguments);
                            var rows = [];
                            if (ret !== null && ret !== undefined) {
                                rows.push({ key: cName + '#' + mName + '#' + idx, value: safeStr(ret), source: cName });
                                inspectFields(ret, 0, rows);
                            }
                            emit('hook_hit', { className: cName, method: mName, fields: rows });
                            return ret;
                        };
                    })(ov, className, name, oi);
                    hooked += 1;
                }
            }
        } catch (_) {}
        return hooked;
    }

    function enumerateCandidates() {
        var all = Java.enumerateLoadedClassesSync();
        var out = [];
        for (var i = 0; i < all.length; i++) {
            var n = String(all[i] || '');
            if (n.indexOf('com.tencent.wework') !== 0) continue;
            if (
                n.indexOf('LoginService') >= 0 ||
                n.indexOf('UserProfile') >= 0 ||
                n.indexOf('ProfileService') >= 0
            ) {
                out.push(n);
            }
        }
        return out;
    }

    function collectGrandProfile() {
        var rows = [];
        try {
            var GPS = Java.use('com.tencent.wework.foundation.logic.GrandProfileService');
            var svc = GPS.getService();
            if (!svc) return rows;
            var getters = [
                'getGid', 'GetGidRealName', 'GetGidEmail',
                'GetCorpInfoList', 'GetShownCorpList', 'GetLoginState', 'GetLoginType'
            ];
            for (var i = 0; i < getters.length; i++) {
                var name = getters[i];
                if (!svc[name]) continue;
                try {
                    rows.push({ key: name, value: safeStr(svc[name]()), source: 'GrandProfileService' });
                } catch (_) {}
            }
        } catch (_) {}
        return rows;
    }

    rpc.exports = {
        collect: function () {
            var pairs = collectGrandProfile();
            var fixed = [
                'com.tencent.wework.foundation.logic.LoginService',
                'com.tencent.wework.foundation.logic.ProfileService',
                'com.tencent.wework.foundation.logic.UserProfileService'
            ];
            var scanned = enumerateCandidates();
            var uniq = {};
            var candidates = [];
            for (var i = 0; i < fixed.length; i++) {
                if (!uniq[fixed[i]]) {
                    uniq[fixed[i]] = true;
                    candidates.push(fixed[i]);
                }
            }
            for (var j = 0; j < scanned.length; j++) {
                if (!uniq[scanned[j]]) {
                    uniq[scanned[j]] = true;
                    candidates.push(scanned[j]);
                }
            }
            var hooked = 0;
            for (var k = 0; k < candidates.length; k++) {
                var cls = candidates[k];
                var rows = snapshotClass(cls);
                for (var r = 0; r < rows.length; r++) pairs.push(rows[r]);
                hooked += hookClass(cls);
            }
            return { candidates: candidates, pairs: pairs, hookCount: hooked };
        }
    };

    emit('ready', { msg: 'account identity probe ready' });
});
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe account identity by LoginService/UserProfile")
    p.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    p.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    p.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    p.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--out-json", default=DEFAULT_OUT_JSON)
    p.add_argument("--once", action="store_true", help="只采集快照后退出")
    p.add_argument("--account-id", default="", help="优先按该 vid 匹配个人显示名")
    return p.parse_args()


def find_pid(adb_serial: str, process_name: str) -> int | None:
    raw = subprocess.check_output([adb_binary(), "-s", adb_serial, "shell", "ps -A -o PID,NAME"]).decode("ascii", "replace")
    for line in raw.splitlines():
        p = line.split()
        if len(p) >= 2 and p[1] == process_name:
            return int(p[0])
    return None


def _corp_blocks(pairs: list[dict[str, str]]) -> list[str]:
    blob = ""
    for row in pairs:
        if str(row.get("key", "")) in {"GetCorpInfoList", "GetShownCorpList"}:
            blob += "\n" + str(row.get("value", ""))
    if not blob.strip():
        return []
    return re.split(r"\ncorps\s*<", blob)


def _decode_proto_escaped(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if "\\" not in value:
        return value
    # proto 文本 dump 里两种转义并存：
    #   \u8303\u65b0\u6021  → unicode_escape
    #   \350\214\203        → codecs.escape_decode（UTF-8 字节八进制）
    if re.search(r"\\u[0-9a-fA-F]{4}", value):
        try:
            return value.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
    if re.search(r"\\[0-7]{3}", value):
        try:
            raw = codecs.escape_decode(value)[0]
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                # 企微 proto text dump 可能走系统编码（常见 GBK/GB18030）。
                return raw.decode("gb18030", errors="replace")
        except Exception:
            pass
    return value


def _pick_proto_name(block: str) -> str:
    for field in ("real_name", "name"):
        match = re.search(rf"{field}:\s*\"((?:\\.|[^\"])*)\"", block)
        if not match:
            continue
        name = _decode_proto_escaped(match.group(1))
        if _is_name_candidate(name):
            return name
    return ""


def _extract_ids(pairs: list[dict[str, str]]) -> tuple[str, str]:
    vid = ""
    gid = ""

    def pick(text: str, key: str) -> str:
        m = re.search(rf"{key}\s*[:=]\s*(\d{{5,}})", text, flags=re.IGNORECASE)
        return m.group(1) if m else ""

    for row in pairs:
        key = str(row.get("key", ""))
        low_key = key.lower()
        val = str(row.get("value", ""))
        source = str(row.get("source", "")).lower()
        blob = f"{key} {val} {source}"
        if key == "getGid" and not gid:
            gid = val.strip()
        if not vid:
            if "vid" in low_key:
                m = re.search(r"(\d{5,})", val)
                if m:
                    vid = m.group(1)
            if not vid:
                vid = pick(blob, "vid")
        if not gid:
            if "gid" in low_key:
                m = re.search(r"(\d{5,})", val)
                if m:
                    gid = m.group(1)
            if not gid:
                gid = pick(blob, "gid")

    for block in _corp_blocks(pairs):
        if "vid_has_logined: true" not in block:
            continue
        match = re.search(r"(?m)^\s*vid:\s*(\d{5,})\s*$", block)
        if match:
            vid = match.group(1)
            break

    return vid, gid


def _is_name_candidate(text: str) -> bool:
    value = text.strip().strip("\"' ")
    if not value:
        return False
    if len(value) < 2 or len(value) > 40:
        return False
    if value.isdigit():
        return False
    low = value.lower()
    blocked = ("com.tencent", "service", "profile", "login", "null", "true", "false", "@")
    if any(x in low for x in blocked):
        return False
    return True


def _extract_display_name(pairs: list[dict[str, str]], *, vid: str = "") -> str:
    target_vid = str(vid or "").strip()
    if target_vid:
        for block in _corp_blocks(pairs):
            if not re.search(rf"(?m)^\s*vid:\s*{re.escape(target_vid)}\s*$", block):
                continue
            name = _pick_proto_name(block)
            if name:
                return name

    for row in pairs:
        if str(row.get("key", "")) == "GetGidRealName":
            name = _decode_proto_escaped(str(row.get("value", "")))
            if _is_name_candidate(name):
                return name

    name_tokens = ("nickname", "nick_name", "nick", "display", "remark", "realname", "zhname", "name")
    patterns = [
        r"nick_name\s*[:=]\s*\"([^\"]+)\"",
        r"display_name\s*[:=]\s*\"([^\"]+)\"",
        r"real_remark\s*[:=]\s*\"([^\"]+)\"",
        r"real_name\s*[:=]\s*\"((?:\\.|[^\"])*)\"",
        r"name\s*[:=]\s*\"((?:\\.|[^\"])*)\"",
    ]
    winners: list[tuple[int, str]] = []
    for row in pairs:
        key = str(row.get("key", "")).strip()
        low_key = key.lower()
        val = str(row.get("value", "")).strip()
        source = str(row.get("source", "")).strip()
        blob = f"{key} {val} {source}"

        # 1) 直接字段命中
        if any(token in low_key for token in name_tokens) and _is_name_candidate(val):
            score = 100
            if any("\u4e00" <= ch <= "\u9fff" for ch in val):
                score += 40
            if "nick" in low_key or "display" in low_key or "remark" in low_key:
                score += 20
            winners.append((score, val.strip().strip("\"' ")))

        # 2) 从 proto/raw 文本里提取
        for pattern in patterns:
            for hit in re.findall(pattern, blob, flags=re.IGNORECASE):
                name = _decode_proto_escaped(str(hit).strip())
                if not _is_name_candidate(name):
                    continue
                score = 80
                if any("\u4e00" <= ch <= "\u9fff" for ch in name):
                    score += 30
                winners.append((score, name))

    if not winners:
        return ""
    winners.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return winners[0][1]


def main() -> int:
    args = parse_args()
    pid = find_pid(args.adb_serial, args.process_name)
    if pid is None:
        print(f"[probe_account_identity] process not found: {args.process_name}")
        return 1

    frida = import_frida()
    dev = frida.get_device_manager().add_remote_device(args.frida_remote)
    sess = dev.attach(pid)
    script = sess.create_script(JS)
    logs: list[dict[str, Any]] = []
    has_error = {"flag": False}

    def on_message(msg: dict[str, Any], _data: Any) -> None:
        if msg.get("type") == "error":
            has_error["flag"] = True
            logs.append({"event": "script_error", "stack": msg.get("stack", str(msg)), "tsMs": int(time.time() * 1000)})
            return
        if msg.get("type") != "send":
            return
        payload = msg.get("payload", {})
        if isinstance(payload, dict):
            logs.append(payload)

    script.on("message", on_message)
    script.load()
    snapshot = script.exports_sync.collect()

    deadline = time.time() + max(0.0, float(args.timeout_seconds))
    while not args.once and time.time() < deadline and not has_error["flag"]:
        time.sleep(0.5)

    sess.detach()

    pairs = list(snapshot.get("pairs", [])) if isinstance(snapshot, dict) else []
    try:
        from app.broadcast.account import read_vid_from_wework_prefs

        vid_from_adb = read_vid_from_wework_prefs()
    except Exception:
        vid_from_adb = ""
    vid, gid = _extract_ids(pairs)
    target_vid = str(args.account_id or "").strip() or vid_from_adb or vid
    if not vid and target_vid:
        vid = target_vid
    display_name = _extract_display_name(pairs, vid=target_vid)
    raw_id = target_vid or gid or DEFAULT_ACCOUNT_ID
    account_id = normalize_account_id(raw_id)

    result = {
        "pid": pid,
        "vid": vid,
        "gid": gid,
        "account_id": account_id,
        "display_name": display_name,
        "candidates": snapshot.get("candidates", []) if isinstance(snapshot, dict) else [],
        "pair_count": len(pairs),
        "hook_count": int(snapshot.get("hookCount", 0)) if isinstance(snapshot, dict) else 0,
        "logs": logs,
        "collected_at": int(time.time() * 1000),
    }

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"vid": vid, "gid": gid, "account_id": account_id, "display_name": display_name}, ensure_ascii=False))
    return 2 if has_error["flag"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
