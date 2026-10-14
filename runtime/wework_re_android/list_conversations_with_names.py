"""List all conversations with resolved display names (uses Conversation.getDefaultName + members)."""

import argparse
import sys
import time

from app.bootstrap.win_subprocess import check_output_hidden
from runtime.wework_re_android._frida_env import adb_binary, import_frida


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--keyword", default="")
    p.add_argument("--wait-seconds", type=float, default=8.0)
    p.add_argument("--refresh", action="store_true", help="also call FetchSessionList before dump")
    return p.parse_args()


def find_pid(serial, name):
    raw = check_output_hidden([adb_binary(), "-s", serial, "shell", "ps -A -o PID,NAME"]).decode("ascii", "replace")
    for line in raw.splitlines():
        p = line.split()
        if len(p) >= 2 and p[1] == name:
            return int(p[0])
    return None


def build_js(keyword: str, refresh: bool) -> str:
    kw = keyword.replace("\\", "\\\\").replace("'", "\\'")
    ref = "true" if refresh else "false"
    return (r"""
'use strict';
Java.perform(function () {
    var ConvSvc = Java.use('com.tencent.wework.foundation.logic.ConversationService');
    var IFetch = Java.use('com.tencent.wework.foundation.callback.IFetchSessionListCallback');
    var keyword = '__KW__';
    var doRefresh = __REF__;
    var KEEP = [];
    var LABEL_NAMES_BY_ID = {};

    function safe(fn) { try { return fn(); } catch (e) { return null; } }
    function sSafe(v) { if (v === null || v === undefined) return ''; try { return String(v).trim(); } catch (_) { return ''; } }
    function pushUnique(items, value) {
        var s = sSafe(value);
        if (!s) return;
        if (items.indexOf(s) < 0) items.push(s);
    }

    function readVarint(bytes, pos, end) {
        var shift = 0;
        var value = 0;
        while (pos < end) {
            var b = bytes[pos] & 0xff;
            pos += 1;
            value += (b & 0x7f) * Math.pow(2, shift);
            if ((b & 0x80) === 0) return { value: value, pos: pos };
            shift += 7;
        }
        return null;
    }

    function readUtf8(bytes, start, len) {
        if (len <= 0) return '';
        var arr = [];
        for (var i = 0; i < len; i++) {
            var b = bytes[start + i] & 0xff;
            arr.push(b > 127 ? b - 256 : b);
        }
        try {
            return String(Java.use('java.lang.String').$new(Java.array('byte', arr), 'UTF-8')).trim();
        } catch (_) {
            return '';
        }
    }

    function firstLabelNameInMessage(bytes, start, end) {
        var pos = start;
        while (pos < end) {
            var key = readVarint(bytes, pos, end);
            if (!key) break;
            pos = key.pos;
            var field = key.value >> 3;
            var wt = key.value & 7;
            if (wt === 0) {
                var vv = readVarint(bytes, pos, end);
                if (!vv) break;
                pos = vv.pos;
            } else if (wt === 2) {
                var ln = readVarint(bytes, pos, end);
                if (!ln) break;
                pos = ln.pos;
                var next = pos + ln.value;
                if (next > end) break;
                if (field === 3) {
                    var text = readUtf8(bytes, pos, ln.value);
                    if (text) return text;
                }
                var nested = firstLabelNameInMessage(bytes, pos, next);
                if (nested) return nested;
                pos = next;
            } else if (wt === 1) {
                pos += 8;
            } else if (wt === 5) {
                pos += 4;
            } else {
                break;
            }
        }
        return '';
    }

    function parseLabelNamesInOrder(bytes) {
        var out = [];
        if (!bytes) return out;
        var pos = 0;
        var end = bytes.length;
        while (pos < end) {
            var key = readVarint(bytes, pos, end);
            if (!key) break;
            pos = key.pos;
            var field = key.value >> 3;
            var wt = key.value & 7;
            if (wt === 2) {
                var ln = readVarint(bytes, pos, end);
                if (!ln) break;
                pos = ln.pos;
                var next = pos + ln.value;
                if (next > end) break;
                if (field === 1) out.push(firstLabelNameInMessage(bytes, pos, next));
                pos = next;
            } else if (wt === 0) {
                var vv = readVarint(bytes, pos, end);
                if (!vv) break;
                pos = vv.pos;
            } else if (wt === 1) {
                pos += 8;
            } else if (wt === 5) {
                pos += 4;
            } else {
                break;
            }
        }
        return out;
    }

    function refreshLabelNameMap(done) {
        var calledDone = false;
        function finish() {
            if (calledDone) return;
            calledDone = true;
            // Step 2: 发送完整的 id→中文名 映射表，供 Python 侧写入账号缓存
            send({t: 'label_map', map: LABEL_NAMES_BY_ID});
            done();
        }
        setTimeout(finish, 3500);
        try {
            var ULS = Java.use('com.tencent.wework.foundation.logic.UserLabelService').getService();
            if (!ULS) {
                finish();
                return;
            }
            var used = safe(function () { return ULS.GetUsedLabelIds(); });
            var idTexts = [];
            var idValues = [];
            var idSeen = {};
            var JLong = Java.use('java.lang.Long');
            function pushLabelId(raw) {
                var text = String(raw || '').trim();
                if (!text || idSeen[text]) return;
                var parsed = safe(function () { return JLong.parseLong(text); });
                if (parsed === null) return;
                idSeen[text] = true;
                idTexts.push(text);
                idValues.push(parsed);
            }
            for (var i = 0; used && i < used.length; i++) pushLabelId(used[i]);
            // GetUsedLabelIds 在部分机型上为空，但 scene=1 的客户标签 id 已经在 LABELS_BY_REMOTE 里。
            Object.keys(LABELS_BY_REMOTE).forEach(function (remote) {
                var arr = LABELS_BY_REMOTE[remote] || [];
                for (var k = 0; k < arr.length; k++) pushLabelId(arr[k]);
            });
            send({t: 'log', msg: 'label name lookup ids=' + idTexts.length});
            if (!idValues.length) {
                finish();
                return;
            }
            var ids = Java.array('long', idValues);
            var CB = Java.registerClass({
                name: 'com.cursor.LabelNameCacheCB' + Date.now(),
                implements: [Java.use('com.tencent.wework.foundation.callback.ICommonCallback')],
                methods: {
                    call: [{returnType: 'void', argumentTypes: ['int', 'long', 'long', '[B'], implementation: function (code, _a, _b, data) {
                        if (code === 0 && data) {
                            var names = parseLabelNamesInOrder(data);
                            for (var j = 0; j < idTexts.length && j < names.length; j++) {
                                if (names[j]) LABEL_NAMES_BY_ID[idTexts[j]] = names[j];
                            }
                            send({t: 'log', msg: 'LABEL_NAMES_BY_ID size=' + Object.keys(LABEL_NAMES_BY_ID).length});
                        }
                        finish();
                    }}]
                }
            });
            var cb = CB.$new();
            KEEP.push(cb);
            Java.scheduleOnMainThread(function () {
                try { ULS.GetCacheLabelFullInfoById(ids, cb); }
                catch (_) { finish(); }
            });
        } catch (_) {
            finish();
        }
    }

    // ==================== 一次性副本：好友快照 + 客户标签映射 ====================
    // SOP §4/§5.1：备注走 GetCachedContactList().getRealRemark；标签走 GetAllLabelIdsHasContact + GetLabelContactsByLabelId
    var CS = null;
    try { CS = Java.use('com.tencent.wework.foundation.logic.ContactService').getService(); } catch (_) {}

    function sleepMs(ms) {
        try {
            var Thread = Java.use('java.lang.Thread');
            Thread.sleep.overload('long').call(Thread, ms);
        } catch (_) {}
    }

    // 预热：缓存为空时试一次已有的同步入口，再短暂等待后重读。
    // 只在空缓存时等待，避免每次刷新都空转。
    if (CS) {
        var firstTry = safe(function () { return CS.GetCachedContactList(); });
        if (!firstTry || firstTry.length === 0) {
            var syncMethods = ['SyncContactCache', 'SyncAllContact', 'FetchContactList', 'RefreshContactList'];
            for (var sm = 0; sm < syncMethods.length; sm++) {
                try {
                    var fn = CS[syncMethods[sm]];
                    if (typeof fn === 'function') {
                        fn.call(CS);
                        send({t: 'log', msg: 'contact preheat: called ' + syncMethods[sm]});
                        break;
                    }
                } catch (_) {}
            }
            sleepMs(1200);
            send({t: 'log', msg: 'contact preheat: waited 1200ms'});
        }
    }

    // remoteId → { remark, displayName, name, corpName }
    var USER_BY_REMOTE = {};
    if (CS) {
        var cached = safe(function () { return CS.GetCachedContactList(); });
        if (cached) {
            for (var ci = 0; ci < cached.length; ci++) {
                var u = cached[ci];
                if (!u) continue;
                var r = safe(function () { return String(u.getRemoteId()); });
                if (!r) continue;
                USER_BY_REMOTE[r] = {
                    remark:      sSafe(safe(function () { return u.getRealRemark(); })),
                    displayName: sSafe(safe(function () { return u.getDisplayName(); })),
                    name:        sSafe(safe(function () { return u.getName(); })),
                    corpName:    sSafe(safe(function () { return u.getCorpName(); }))
                };
            }
        }
        send({t: 'log', msg: 'USER_BY_REMOTE size=' + Object.keys(USER_BY_REMOTE).length});
    }

    // remoteId → [labelId,...]（外部客户标签，scene=1；若未来要加公司内标签再加 scene 0/2）
    var LABELS_BY_REMOTE = {};
    function buildLabelsByRemote() {
        var out = {};
        if (!CS) return out;
        var JLong = Java.use('java.lang.Long');
        var ids = safe(function () { return CS.GetAllLabelIdsHasContact(1); });
        if (!ids) return out;
        for (var li = 0; li < ids.length; li++) {
            var lidStr = String(ids[li]);
            var lid = safe(function () { return JLong.parseLong(lidStr); });
            if (lid === null) continue;
            var users = safe(function () { return CS.GetLabelContactsByLabelId(lid, 1); });
            if (!users) continue;
            for (var uu = 0; uu < users.length; uu++) {
                var user = users[uu];
                if (!user) continue;
                var r = safe(function () { return String(user.getRemoteId()); });
                if (!r) continue;
                if (!out[r]) out[r] = [];
                if (out[r].indexOf(lidStr) < 0) out[r].push(lidStr);
            }
        }
        return out;
    }
    LABELS_BY_REMOTE = buildLabelsByRemote();
    if (CS && Object.keys(LABELS_BY_REMOTE).length === 0) {
        // 标签索引偶尔晚于联系人缓存。只在首次为空时再等一次，不循环重试。
        sleepMs(1200);
        LABELS_BY_REMOTE = buildLabelsByRemote();
        send({t: 'log', msg: 'label index retry after 1200ms'});
    }
    send({t: 'log', msg: 'LABELS_BY_REMOTE size=' + Object.keys(LABELS_BY_REMOTE).length});

    function decodeProtoEscaped(raw) {
        var JavaString = Java.use('java.lang.String');
        var out = '';
        var bytes = [];
        function flushBytes() {
            if (!bytes.length) return;
            var arr = Java.array('byte', bytes.map(function (b) { return b > 127 ? b - 256 : b; }));
            out += String(JavaString.$new(arr, 'UTF-8'));
            bytes = [];
        }
        for (var i = 0; i < raw.length; i++) {
            var ch = raw.charAt(i);
            if (ch !== '\\') {
                flushBytes();
                out += ch;
                continue;
            }
            var next = raw.charAt(i + 1);
            if (next === 'u' && i + 5 < raw.length) {
                flushBytes();
                var hex = raw.substr(i + 2, 4);
                var code = parseInt(hex, 16);
                if (!isNaN(code)) {
                    out += String.fromCharCode(code);
                    i += 5;
                    continue;
                }
            }
            if (/[0-7]/.test(next)) {
                var oct = next;
                var j = i + 2;
                while (j < raw.length && oct.length < 3 && /[0-7]/.test(raw.charAt(j))) {
                    oct += raw.charAt(j);
                    j++;
                }
                bytes.push(parseInt(oct, 8));
                i = j - 1;
                continue;
            }
            flushBytes();
            if (next === 'n') out += '\n';
            else if (next === 'r') out += '\r';
            else if (next === 't') out += '\t';
            else out += next || ch;
            i++;
        }
        flushBytes();
        return out.trim();
    }

    function pickProtoString(text, field, topLevelOnly) {
        var re = topLevelOnly
            ? new RegExp('(?:^|\\n)' + field + ':\\s*"((?:\\\\.|[^"])*)"')
            : new RegExp('(?:^|\\n)\\s*' + field + ':\\s*"((?:\\\\.|[^"])*)"');
        var m = re.exec(text);
        return m ? decodeProtoEscaped(m[1]) : '';
    }

    // ==================== 群名/群备注：解析 ConversationInfo protobuf 文本 + getter 兜底 ====================
    function collectGroupCandidates(conv) {
        var out = {};
        var info = safe(function () { return conv.getInfo(); });
        if (!info) return out;
        var infoText = sSafe(info);
        var roomRemark = pickProtoString(infoText, 'roomname_remark', false);
        var roomName = pickProtoString(infoText, 'name', true);
        var defaultName = pickProtoString(infoText, 'default_name', false);
        if (roomRemark) out.protoRoomRemark = roomRemark;
        if (roomName) out.protoRoomName = roomName;
        if (defaultName) out.protoDefaultName = defaultName;
        try {
            var methods = info.getClass().getMethods();
            for (var i = 0; i < methods.length; i++) {
                var m = methods[i];
                var name = String(m.getName() || '');
                if (m.getParameterTypes().length !== 0) continue;
                if (String(m.getReturnType()) !== 'class java.lang.String') continue;
                var low = name.toLowerCase();
                // 只留可能是"名字/备注"的候选，避免炸出几十条
                if (low.indexOf('name') < 0 && low.indexOf('remark') < 0 && low.indexOf('title') < 0 && low.indexOf('subject') < 0) continue;
                var val = null;
                try { val = m.invoke(info, []); } catch (_) { continue; }
                var s = sSafe(val);
                if (!s) continue;
                out[name] = s;
            }
        } catch (_) {}
        return out;
    }

    function dumpAll(tag) {
        var svc = ConvSvc.getService();
        if (!svc) { send({t: 'log', msg: 'ConvSvc.getService() == null (tag=' + tag + ')'}); return; }
        var arr = null;
        try { arr = svc.GetConversationList(); }
        catch (e) { send({t: 'log', msg: 'GetConversationList throw: ' + e + ' (tag=' + tag + ')'}); return; }
        var n = arr ? arr.length : 0;
        send({t: 'total', tag: tag, n: n});
        for (var i = 0; i < n; i++) {
            var c = arr[i];
            if (!c) continue;
            // conv_local 提取：**优先** String(c) 里的 `Local: <num>`（5.0.10 实测：
            // c.getInfo().getLocalId() 和 c.getLocalId() 返回的其实是 remote id，不能信）
            var rawStr = String(c);
            var localId = (function () {
                var m = /Local:\s*(\d+)/.exec(rawStr) || /localId[=:]\s*(\d+)/.exec(rawStr) || /local_id[=:]\s*(\d+)/.exec(rawStr);
                return m ? m[1] : '';
            })()
                || safe(function () { return String(c.getInfo().getLocalId()); })
                || safe(function () { return String(c.getLocalId()); })
                || '?';
            var remoteId = safe(function () { return String(c.getRemoteId()); })
                || (function () { var m = /remote:\s*(\d+)/.exec(rawStr); return m ? m[1] : ''; })()
                || '?';
            // 客户方的"原始名字"（单聊=对方微信名；群聊=拼接成员名）
            var peerName = sSafe(safe(function () { return c.getDefaultName(true); }));

            // 会话类型（从 raw 里抽）：0=外部单聊、1=外部群、3=系统服务号、其他=?
            var typeMatch = /type:\s*(\d+)/.exec(rawStr);
            var convType = typeMatch ? parseInt(typeMatch[1], 10) : -1;

            // 我们打的备注（SOP §4 主链）：GetCachedContactList → getRealRemark
            var userSnap = USER_BY_REMOTE[remoteId] || null;
            var ourRemark = userSnap ? (userSnap.remark || userSnap.displayName || '') : '';
            if (!ourRemark && convType === 0) {
                var infoForRemark = safe(function () { return c.getInfo(); });
                var defaultFromInfo = infoForRemark ? pickProtoString(sSafe(infoForRemark), 'default_name', false) : '';
                if (defaultFromInfo && defaultFromInfo !== peerName) ourRemark = defaultFromInfo;
            }

            // 群名/群备注候选（仅 type=1 群时展开）
            var groupCandidates = (convType === 1) ? collectGroupCandidates(c) : {};

            // 标签 id 列表（SOP §5.1 α 主链）——由 Python 侧读 customer_label_names.json 转成中文
            var labelIds = LABELS_BY_REMOTE[remoteId] ? LABELS_BY_REMOTE[remoteId].slice() : [];
            var labelNames = [];
            for (var lni = 0; lni < labelIds.length; lni++) {
                var lname = LABEL_NAMES_BY_ID[labelIds[lni]] || '';
                if (lname && labelNames.indexOf(lname) < 0) labelNames.push(lname);
            }

            send({
                t: 'row',
                tag: tag, i: i,
                local: localId, remote: remoteId,
                type: convType,
                peerName: peerName,              // 对方原始名字（客户微信名/群成员拼接）
                ourRemark: ourRemark,            // 我们的备注（getRealRemark）
                userName: userSnap ? userSnap.name : '',
                corpName: userSnap ? userSnap.corpName : '',
                groupCandidates: groupCandidates, // 群候选：{getName: ..., getRemark: ..., ...}
                labelIds: labelIds,              // Long id 字符串数组，Python 侧转中文
                labelNames: labelNames,          // 主线程标签缓存解析出的中文名
                hasExternalLabel: labelIds.length > 0,  // scene=1 标签命中，分类用，不依赖中文名是否解析成功
                raw: rawStr
            });
            if (keyword && (peerName.indexOf(keyword) >= 0 || ourRemark.indexOf(keyword) >= 0 || rawStr.indexOf(keyword) >= 0)) {
                send({t: 'hit', local: localId, remote: remoteId, type: convType, peerName: peerName, ourRemark: ourRemark, groupCandidates: groupCandidates, labelIds: labelIds, labelNames: labelNames});
            }
        }
    }

    // ==================== 好友快照输出 ====================
    // Step 1 补全：把 USER_BY_REMOTE 里的好友也输出成 t:'friend' 行，
    // Python 侧 contacts.py 可以直接合并，不必再起第二个 list_all_contacts 子进程。
    // 注意：代码里没有 Python 侧条数上限。现状上限等于 GetCachedContactList()
    // 的本地好友缓存长度（取决于企微是否加载过通讯录，上面已做预热）。
    function dumpFriends() {
        var remotes = Object.keys(USER_BY_REMOTE);
        send({t: 'friends_total', n: remotes.length});
        for (var fi = 0; fi < remotes.length; fi++) {
            var r = remotes[fi];
            var snap = USER_BY_REMOTE[r];
            var friendLabelIds = LABELS_BY_REMOTE[r] ? LABELS_BY_REMOTE[r].slice() : [];
            var friendLabelNames = [];
            for (var fli = 0; fli < friendLabelIds.length; fli++) {
                var flname = LABEL_NAMES_BY_ID[friendLabelIds[fli]] || '';
                if (flname && friendLabelNames.indexOf(flname) < 0) friendLabelNames.push(flname);
            }
            send({
                t: 'friend',
                remote: r,
                remark: snap.remark || '',
                displayName: snap.displayName || '',
                name: snap.name || '',
                corpName: snap.corpName || '',
                labelIds: friendLabelIds,
                labelNames: friendLabelNames,
                hasExternalLabel: friendLabelIds.length > 0
            });
        }
    }

    refreshLabelNameMap(function () {
        // 先 dump 一次（拿本地缓存），保证即使 refresh 回调不来也有结果。
        Java.scheduleOnMainThread(function () {
            dumpAll('now');
            dumpFriends();
        });

        if (doRefresh) {
            var CB = Java.registerClass({
                name: 'com.cursor.FetchSessionListCB2',
                implements: [IFetch],
                methods: {
                    onResult: [{returnType: 'void', argumentTypes: [], implementation: function () {
                        send({t: 'log', msg: 'FetchSessionList onResult'});
                        Java.scheduleOnMainThread(function () { dumpAll('after'); });
                    }}]
                }
            });
            Java.scheduleOnMainThread(function () {
                try {
                    ConvSvc.getService().FetchSessionList(CB.$new(), false);
                    send({t: 'log', msg: 'FetchSessionList called'});
                } catch (e) { send({t: 'log', msg: 'fetch err ' + e}); }
            });
        }
    });
});
""".replace("__KW__", kw).replace("__REF__", ref))


import json as _json


def _emit_stdout_json(payload: dict) -> None:
    # 单行 JSON，含真实 \n 的字段 (name/members/raw) 会被 json.dumps 转义成 \\n
    # 保证下游 parse 一行一条、不会因换行被拆散。
    sys.stdout.buffer.write((_json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def on_msg(m, _):
    if m.get("type") == "send":
        p = m["payload"] or {}
        t = p.get("t")
        if t in ("row", "hit", "friend", "label_map"):
            _emit_stdout_json(p)
        elif t in ("total", "friends_total"):
            line = f"[{p['tag']}] total={p['n']}"
            sys.stderr.write(line + "\n"); sys.stderr.flush()
            _emit_stdout_json(p)
        elif t == "log":
            line = f"[log] {p.get('msg')}"
            sys.stderr.write(line + "\n"); sys.stderr.flush()
            _emit_stdout_json(p)
        else:
            _emit_stdout_json(p)
    elif m.get("type") == "error":
        sys.stderr.write(f"[ERR] {m.get('stack')}\n")
        sys.stderr.flush()


def main():
    args = parse_args()
    pid = find_pid("127.0.0.1:16384", "com.tencent.wework")
    if pid is None:
        print("no pid")
        sys.exit(1)
    frida = import_frida()
    dev = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
    sess = dev.attach(pid)
    sc = sess.create_script(build_js(args.keyword, args.refresh))
    sc.on("message", on_msg)
    sc.load()
    time.sleep(args.wait_seconds)
    sess.detach()


if __name__ == "__main__":
    main()
