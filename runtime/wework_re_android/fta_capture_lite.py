"""Step F1: FTA 采集 · lite 版（hook 零 I/O + Python 侧异步复制）。

与 `fta_capture.py`（已删除）的差异：
- JS 侧：删除 copySilk 与 dumpHead，hook 内只做事件 send。
- Python 侧：on_message 只做路由，文件复制走 adb shell cp（adbd 进程执行），
  DPAPI 写盘丢 ThreadPoolExecutor。
- silk 依然保留在设备 /sdcard/Test-Voice/voice_lib/，索引 file 字段语义
  与旧脚本一致（发送时直接引用设备端路径，符合架构决策 #3）。

F1.4/F1.5 变更（v0.3）：
- 去掉"FTA 文字 → note"关联逻辑（决策 F8：备注命名归属 GUI 双击）。
  ``--note-window-seconds`` 参数保留兼容位但内部忽略，不破坏外部调用者。
- 观察模式扩到全素材类型（决策 F10/F11）：命中 remote==FTA 的任意 mType 都
  额外上报一次 ``fta_asset_seen`` 事件；Python 侧对应 ``asset_observed``，
  只写 stdout jsonl、不落库，为 F3.5 按类型入库设计收集样本。

F2.1/F2.2 变更（v0.4）：
- 所有 stdout jsonl 事件带 ``sv:1``（schema 版本），详见
  ``runtime/wework_re_android/notes/fta_capture_protocol.md``。
- 新增 ``--account-id``：自动派生 voice 索引路径（``account_voice_index_path``），
  与其他账号级存储对齐；``--index-path`` 若显式给出则优先生效（保持兼容）。
- 所有诊断 print（``[fta_lite] hooked ...``）改走 **stderr**，stdout 只走 jsonl，
  方便 ``CaptureClient`` 通过 stdout 尾读单一事件流。

F3.3/F3.4 变更（v0.5）：
- attach 改走 :class:`FridaSession`；主循环内每 ``--heartbeat-seconds`` 做一次
  心跳（默认 15s）。失败 → 发 ``heartbeat_lost`` → 调 ``session.reconnect(retries=3)``。
  重连成功 → 重新 create_script + on_message + load，发 ``reconnected``；
  重连失败 → 发 ``stopped reason=reconnect_failed`` 后退出 1。
- ``atexit`` 注册收尾 detach，防 os._exit / 未捕获异常导致 wework 端残留 script。
- SIGTERM → 转 ``KeyboardInterrupt``，让 finally 块的 detach + metrics dump 正常跑。

CLI 参数（保持向后兼容）：
- --emit-jsonl / --no-emit-jsonl : 事件走 stdout jsonl（默认开启）
- --dry-copy                     : 只 send 事件不实际 cp（对比 hook 纯开销）
- --metrics-out                  : 退出时把 hook 耗时统计落 json
- --note-window-seconds          : 兼容位（F1.4 起已忽略）
- --account-id                   : 与 GUI 账号隔离对齐，派生索引路径（F2.2）
- --heartbeat-seconds            : 心跳周期（0 关闭），默认 15s（F3.3）
- --reconnect-retries            : 重连尝试次数，默认 3（F3.3）
"""

from __future__ import annotations

import argparse
import atexit
import codecs
import concurrent.futures as cf
import json
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bootstrap.win_subprocess import install_global_no_window_patch
from app.broadcast.paths import account_voice_index_path
from app.security import get_secure_store
from runtime.wework_re_android._frida_env import adb_binary

# 采集进程会多次调用 adb.exe。父界面的无窗口补丁不会带进这个子进程。
install_global_no_window_patch()

# stdout jsonl schema 版本；破坏性变更需 ++，详见 notes/fta_capture_protocol.md
EVENT_SCHEMA_VERSION = 1

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_FTA_REMOTE_ID = "10006"  # PLAN Step 2.5 实测
DEFAULT_VOICE_LIB_ROOT = "/sdcard/Test-Voice/voice_lib"
DEFAULT_IMAGE_LIB_ROOT = "/sdcard/Test-Voice/image_lib"
DEFAULT_VIDEO_LIB_ROOT = "/sdcard/Test-Voice/video_lib"
# msg.toString() 前 N 字节作为观察样本（够看结构，不至于把 frida 消息队列撑爆）
ASSET_MSG_PREVIEW_BYTES = 500
_SECURE_STORE = get_secure_store()

# F3.5 mType 分派表（合并 F1.7 实测 + 各 probe 的历史覆盖，union 更保险）
MTYPE_VOICE = {9}
MTYPE_IMAGE = {7, 14}
MTYPE_VIDEO = {5, 23, 43, 62}  # F3.5 修：当前企微 GUI 发视频实际 mType=5（真机联调实测，
# 见 runtime/artifacts/fta_lite_last.jsonl），旧 {23,43,62} 属于历史 probe 覆盖，
# 不删是为了兼容不同版本；5 不与 voice/image/oa 冲突。
MTYPE_OA = {13}
MTYPE_WXA = {78}
# 上游 hook 关联窗口：F1.7 实测 voice = 10s；probe 侧 video/oa = 15s
UPSTREAM_WINDOW_MS_DEFAULT = 15000


# ---------------------------------------------------------------------------
# Frida JS — 极简：只 hook + send，任何 I/O 都不做
# ---------------------------------------------------------------------------
JS = r"""
'use strict';

var FTA_REMOTE_ID = '__FTA_REMOTE_ID__';
var MSG_PREVIEW_BYTES = __MSG_PREVIEW_BYTES__;

function parseConvIds(convStr) {
    var local = '';
    var remote = '';
    var mLocal = /Local:\s*(\d+)/.exec(convStr);
    var mRemote = /remote:\s*(\d+)/.exec(convStr);
    if (mLocal) local = mLocal[1];
    if (mRemote) remote = mRemote[1];
    return { local: local, remote: remote };
}

function parseMsgType(msgStr) {
    var m = /\|mType\|(\d+)\|/.exec(msgStr);
    return m ? parseInt(m[1], 10) : -1;
}

function parseLId(msgStr) {
    var m = /\|lId\|(\d+)\|/.exec(msgStr);
    return m ? m[1] : '';
}

Java.perform(function () {
    // F3.5 上游 hook 环形缓冲：4 个 pending 队列，SendMessage 按 mType 分派到对应队列
    var pendingVoices = [];
    var pendingImages = [];
    var pendingVideos = [];
    var pendingOas = [];

    var UPSTREAM_WINDOW_MS = __UPSTREAM_WINDOW_MS__;

    // F3.5 P0 修：企微内部对同一消息会同步重试 SendMessage 数百次（发图片 = 300+ 次）。
    // 双层去重，缺一不可：
    //   1. seenMsgHc：Java Message 的 identityHashCode —— 覆盖 lId 未回填的早期重试；
    //   2. seenLIds：解析 msgStr 里的 lId —— 覆盖 wework 重新 new Message 但 lId 不变的场景。
    // 任一命中即早返回；两者都 10s gc。
    // 血泪教训：lite_diag3.log 一张图触发 309 次事件说明单靠 lId 不够，
    // 部分早期重试的 Message 里 lId 还没写好（parseLId 返回空）→ 只能兜到 hc。
    var seenLIds = {};
    var seenMsgHc = {};
    var LID_GC_MS = 10000;

    function gc(nowMs) {
        // voice 沿用 10s 窗口（F1.7 实测足够）；其他类型走统一 UPSTREAM_WINDOW_MS
        while (pendingVoices.length > 0 && (nowMs - pendingVoices[0].tsMs) > 10000) {
            pendingVoices.shift();
        }
        while (pendingImages.length > 0 && (nowMs - pendingImages[0].tsMs) > UPSTREAM_WINDOW_MS) {
            pendingImages.shift();
        }
        while (pendingVideos.length > 0 && (nowMs - pendingVideos[0].tsMs) > UPSTREAM_WINDOW_MS) {
            pendingVideos.shift();
        }
        while (pendingOas.length > 0 && (nowMs - pendingOas[0].tsMs) > UPSTREAM_WINDOW_MS) {
            pendingOas.shift();
        }
        // seenLIds / seenMsgHc gc
        var keys = Object.keys(seenLIds);
        for (var ki = 0; ki < keys.length; ki++) {
            if (nowMs - seenLIds[keys[ki]] > LID_GC_MS) delete seenLIds[keys[ki]];
        }
        var hcKeys = Object.keys(seenMsgHc);
        for (var hi = 0; hi < hcKeys.length; hi++) {
            if (nowMs - seenMsgHc[hcKeys[hi]] > LID_GC_MS) delete seenMsgHc[hcKeys[hi]];
        }
    }

    function latestInWindow(buf, nowMs, windowMs) {
        for (var i = buf.length - 1; i >= 0; i--) {
            if (nowMs - buf[i].tsMs <= windowMs) return buf[i];
        }
        return null;
    }

    function latestVideoInWindow(buf, nowMs, windowMs) {
        // 优先 D8（含 thumb 路径），fallback 到 V1 兜底
        var v1Hit = null;
        for (var i = buf.length - 1; i >= 0; i--) {
            if (nowMs - buf[i].tsMs > windowMs) break;
            if (buf[i].from === 'D8') return buf[i];
            if (!v1Hit) v1Hit = buf[i];
        }
        return v1Hit;
    }

    var h8y = null;
    try { h8y = Java.use('h8y'); } catch (eH8y) {
        send({ event: 'error', stage: 'java_use_h8y', message: String(eH8y) });
    }
    // 缓存 java.lang.System 供 identityHashCode 用（每次 SendMessage 都调，
    // 避免反复 Java.use 的开销）
    var JSystem = null;
    try { JSystem = Java.use('java.lang.System'); } catch (eSys) { /* ignore */ }
    var JString = null;
    try { JString = Java.use('java.lang.String'); } catch (eStr) { /* ignore */ }
    var JFile = null;
    try { JFile = Java.use('java.io.File'); } catch (eFile) { /* ignore */ }
    // LinkMessage proto —— 公众号卡片 content 字节里承载 title/description/link/image。
    // 之前对 content blob 做正则只能抓到 ASCII 的 URL，UTF-8 的 title/公众号名(description)
    // 全部丢失。这里直接按 proto 解析，拿到干净字段。
    var LinkMsgProto = null;
    try { LinkMsgProto = Java.use('com.tencent.wework.foundation.model.pb.LinkMessageProto$LinkMessage'); }
    catch (eLmp) { /* 老版本类名可能不同，失败则回退正则 */ }

    function bytesToUtf8(b) {
        if (b === null || b === undefined) return '';
        try { return JString.$new(b, 'UTF-8').toString(); } catch (_) { return ''; }
    }

    function readContentType(info) {
        if (!info) return -1;
        try { return info.contentType.value | 0; } catch (_) {}
        try { return parseInt(String(info.contentType), 10) || -1; } catch (_) {}
        return -1;
    }

    function toJsBytes(javaBytes) {
        if (!javaBytes) return [];
        var n = 0;
        try { n = javaBytes.length | 0; } catch (_) { return []; }
        var out = new Array(n);
        for (var i = 0; i < n; i++) {
            var b = javaBytes[i];
            out[i] = (b < 0) ? (b + 256) : (b & 255);
        }
        return out;
    }

    function jsBytesToJava(list) {
        var out = Java.array('byte', Array(list.length).fill(0));
        for (var i = 0; i < list.length; i++) {
            var v = list[i] & 255;
            out[i] = v > 127 ? v - 256 : v;
        }
        return out;
    }

    function readVarint(bytes, offset) {
        var value = 0;
        var shift = 0;
        var i = offset;
        while (i < bytes.length && shift <= 28) {
            var b = bytes[i] & 255;
            i++;
            value += (b & 127) * Math.pow(2, shift);
            if ((b & 128) === 0) return { value: value, offset: i };
            shift += 7;
        }
        return null;
    }

    function protoLenFields(bytes, fieldNum) {
        var found = [];
        var i = 0;
        while (i < bytes.length) {
            var key = readVarint(bytes, i);
            if (!key) break;
            i = key.offset;
            var field = Math.floor(key.value / 8);
            var wire = key.value % 8;
            if (wire === 0) {
                var n = readVarint(bytes, i);
                if (!n) break;
                i = n.offset;
            } else if (wire === 1) {
                i += 8;
            } else if (wire === 5) {
                i += 4;
            } else if (wire === 2) {
                var ln = readVarint(bytes, i);
                if (!ln) break;
                i = ln.offset;
                var size = ln.value;
                if (size < 0 || i + size > bytes.length) break;
                if (field === fieldNum) found.push(bytes.slice(i, i + size));
                i += size;
            } else {
                break;
            }
        }
        return found;
    }

    function findWeappBody(bytes) {
        if (!bytes || !bytes.length) return null;
        var wrappers = protoLenFields(bytes, 10);
        for (var i = 0; i < wrappers.length; i++) {
            var inner = protoLenFields(wrappers[i], 107);
            if (inner.length && inner[0].length >= 8) return inner[0];
        }
        var direct = protoLenFields(bytes, 107);
        if (direct.length && direct[0].length >= 8) return direct[0];
        return null;
    }

    function readDeclaredText(obj, name) {
        if (!obj) return '';
        try {
            var f = obj.getClass().getDeclaredField(name);
            f.setAccessible(true);
            var v = f.get(obj);
            if (v === null || v === undefined) return '';
            var t = String(f.getType().getName());
            if (t === '[B') {
                try { return String(JString.$new(v, 'UTF-8')); } catch (_) { return ''; }
            }
            return String(v);
        } catch (_) {
            return '';
        }
    }

    // contentType 78：WwMessage 字段 10 里的字段 107 才是 WeAppMessage。
    // 不能按 LinkMessage 解析，否则会把小程序收成公众号卡片。
    function parseWxaCard(msg, force) {
        if (!msg) return null;
        var info = null;
        try { info = msg.getInfo(); } catch (_) { return null; }
        if (!info) return null;
        var ct = readContentType(info);
        if (ct === 13) return null;
        if (ct !== 78 && !force) return null;
        var raw = null;
        try {
            var MN = Java.use('com.google.protobuf.nano.MessageNano');
            var toBytes = MN.toByteArray.overload('com.google.protobuf.nano.MessageNano');
            raw = toBytes.call(MN, info);
        } catch (_) { raw = null; }
        var body = findWeappBody(toJsBytes(raw));
        if (!body) {
            try { body = findWeappBody(toJsBytes(info.content.value)); } catch (_) { body = null; }
        }
        if (!body || body.length < 8) return null;
        var javaBody = jsBytesToJava(body);
        var b64 = '';
        try {
            var Base64 = Java.use('android.util.Base64');
            b64 = String(Base64.encodeToString(javaBody, 2));
        } catch (_) {
            return null;
        }
        var card = {
            weappBodyB64: b64,
            username: '',
            pagepath: '',
            title: '',
            appName: '',
            weappIconUrl: '',
            desc: '',
            type: '2'
        };
        try {
            var WeApp = Java.use('com.tencent.wework.foundation.model.pb.WeAppMessageProto$WeAppMessage');
            var app = WeApp.parseFrom.overload('[B').call(WeApp, javaBody);
            card.username = readDeclaredText(app, 'username');
            card.pagepath = readDeclaredText(app, 'pagepath');
            card.title = readDeclaredText(app, 'title');
            card.appName = readDeclaredText(app, 'appName');
            card.weappIconUrl = readDeclaredText(app, 'weappIconUrl');
            card.desc = readDeclaredText(app, 'desc');
            var typeText = readDeclaredText(app, 'type');
            if (typeText) card.type = typeText;
        } catch (_) {}
        return card;
    }

    // 从 Message 直接解析公众号 LinkMessage 干净字段；失败返回 null。
    // 只接受 contentType 13。78 是小程序，走 parseWxaCard。
    function parseOaLinkMessage(msg) {
        if (!LinkMsgProto || !msg) return null;
        try {
            var info = msg.getInfo();
            if (!info) return null;
            var ct = readContentType(info);
            if (ct !== 13) return null;
            var content = info.content.value;
            if (!content) return null;
            var lm = LinkMsgProto.parseFrom(content);
            return {
                link: bytesToUtf8(lm.linkUrl.value),
                title: bytesToUtf8(lm.title.value),
                desc: bytesToUtf8(lm.description.value),
                imgUrl: bytesToUtf8(lm.imageUrl.value),
                appname: String(lm.appname.value || '')
            };
        } catch (_) { return null; }
    }

    // 企微文字：mType 0/1 的 content 是 TextMessage（field 1 = 正文）；
    // mType 2 是 RichMessage，正文嵌在子消息的 field 2 里。
    function looksLikeHumanText(s) {
        if (!s) return false;
        var t = String(s);
        if (!t || t.length > 8000) return false;
        if (t.indexOf('\uFFFD') >= 0 || t.indexOf('com.tencent.') >= 0) return false;
        var visible = 0;
        for (var i = 0; i < t.length; i++) {
            var c = t.charCodeAt(i);
            if (c === 0) return false;
            if (c < 32 && c !== 9 && c !== 10 && c !== 13) return false;
            if (c > 32) visible++;
        }
        return visible > 0;
    }

    function utf8FromJsBytes(list) {
        if (!list || !list.length || !JString) return '';
        try { return String(JString.$new(jsBytesToJava(list), 'UTF-8')); } catch (_) { return ''; }
    }

    function textFromKnownProto(content) {
        var names = [
            'com.tencent.wework.foundation.model.pb.WwRichmessage$TextMessage',
            'com.tencent.wework.foundation.model.pb.WwRichmessageBase$TextMessage'
        ];
        for (var i = 0; i < names.length; i++) {
            try {
                var TM = Java.use(names[i]);
                var tm = TM.parseFrom.overload('[B').call(TM, content);
                var text = readDeclaredText(tm, 'content') || readDeclaredText(tm, 'content_');
                text = String(text || '').replace(/^\s+|\s+$/g, '');
                if (looksLikeHumanText(text)) return text.length > 4000 ? text.substring(0, 4000) : text;
            } catch (_) {}
        }
        return '';
    }

    function textIfFieldOne(bytes) {
        if (!bytes || !bytes.length || (bytes[0] & 7) !== 2) return '';
        var parts = protoLenFields(bytes, 1);
        if (parts.length !== 1) return '';
        var text = String(utf8FromJsBytes(parts[0]) || '').replace(/^\s+|\s+$/g, '');
        if (!looksLikeHumanText(text)) return '';
        return text.length > 4000 ? text.substring(0, 4000) : text;
    }

    function extractTextPieces(bytes, depth) {
        if (!bytes || !bytes.length || depth > 6) return [];
        var direct = textIfFieldOne(bytes);
        if (direct) return [direct];
        var pieces = [];
        var i = 0;
        while (i < bytes.length) {
            var key = readVarint(bytes, i);
            if (!key) return pieces;
            i = key.offset;
            var wire = key.value % 8;
            if (wire === 0) {
                var n = readVarint(bytes, i);
                if (!n) return pieces;
                i = n.offset;
            } else if (wire === 1) {
                if (i + 8 > bytes.length) return pieces;
                i += 8;
            } else if (wire === 5) {
                if (i + 4 > bytes.length) return pieces;
                i += 4;
            } else if (wire === 2) {
                var ln = readVarint(bytes, i);
                if (!ln) return pieces;
                i = ln.offset;
                var size = ln.value;
                if (size < 0 || i + size > bytes.length) return pieces;
                var chunk = bytes.slice(i, i + size);
                i += size;
                var nested = extractTextPieces(chunk, depth + 1);
                for (var p = 0; p < nested.length; p++) pieces.push(nested[p]);
            } else {
                return pieces;
            }
        }
        return pieces;
    }

    function extractPlainText(msg) {
        if (!msg) return '';
        var info = null;
        try { info = msg.getInfo(); } catch (_) { return ''; }
        if (!info) return '';
        var content = null;
        try { content = info.content.value; } catch (_) { content = null; }
        if (!content) return '';
        var fromClass = textFromKnownProto(content);
        if (fromClass) return fromClass;
        var pieces = extractTextPieces(toJsBytes(content), 0);
        if (!pieces.length) return '';
        var joined = '';
        for (var pi = 0; pi < pieces.length; pi++) joined += pieces[pi];
        joined = joined.replace(/^\s+|\s+$/g, '');
        if (!looksLikeHumanText(joined)) return '';
        return joined.length > 4000 ? joined.substring(0, 4000) : joined;
    }

    function emitPlainText(ids, msg, mType, t0) {
        var textBody = extractPlainText(msg);
        if (!textBody) {
            send({ event: 'dispatch_miss', kind: 'text', mType: mType, tsMs: t0 });
            return;
        }
        send({
            event: 'fta_text_seen',
            convLocal: ids.local,
            convRemote: ids.remote || FTA_REMOTE_ID,
            body: textBody,
            mType: mType,
            tsMs: t0,
            hookElapsedMs: Date.now() - t0
        });
    }

    // 路径 → 素材类型；被多个 hook 复用（U1/V1/q8）
    function classifyMediaPath(p) {
        var lp = String(p || '').toLowerCase();
        if (!lp) return 'unknown';
        if (lp.indexOf('.silk') >= 0) return 'voice';
        if (lp.indexOf('.mp4') >= 0 || lp.indexOf('.mov') >= 0 || lp.indexOf('.avi') >= 0 ||
            lp.indexOf('.mkv') >= 0 || lp.indexOf('.m4v') >= 0) return 'video';
        if (lp.indexOf('.png') >= 0 || lp.indexOf('.jpg') >= 0 || lp.indexOf('.jpeg') >= 0 ||
            lp.indexOf('.webp') >= 0 || lp.indexOf('.gif') >= 0 || lp.indexOf('.bmp') >= 0) return 'image';
        if (lp.indexOf('tempimagecache') >= 0 && lp.indexOf('video_thumb') < 0) return 'image';
        // imagecache/imagemsg2/<md5>.<n>：mType=14 GUI 发图的全尺寸图（扩展名是数字）
        if (lp.indexOf('imagecache') >= 0 && lp.indexOf('video_thumb') < 0) return 'image';
        if (lp.indexOf('filecache') >= 0) return 'video';
        return 'unknown';
    }

    function isDevicePath(p) {
        var s = String(p || '');
        return s.indexOf('/storage/emulated/0/') === 0 || s.indexOf('/sdcard/') === 0;
    }

    function findRecentImageCachePath(nowMs) {
        if (!JFile) return '';
        var roots = [
            '/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache',
            '/sdcard/Android/data/com.tencent.wework/files/tempimagecache'
        ];
        var bestPath = '';
        var bestScore = 1e18;
        var seen = 0;

        function visit(file, depth) {
            if (!file || depth < 0 || seen > 1500) return;
            seen++;
            try {
                if (!file.exists()) return;
                var path = String(file.getAbsolutePath());
                var lp = path.toLowerCase();
                if (file.isDirectory()) {
                    if (lp.indexOf('video_thumb') >= 0) return;
                    var children = file.listFiles();
                    if (!children) return;
                    for (var i = 0; i < children.length; i++) visit(children[i], depth - 1);
                    return;
                }
                if (lp.indexOf('video_thumb') >= 0) return;
                var looksImage = lp.indexOf('_compress') >= 0 ||
                    lp.indexOf('.png') >= 0 || lp.indexOf('.jpg') >= 0 ||
                    lp.indexOf('.jpeg') >= 0 || lp.indexOf('.webp') >= 0 ||
                    lp.indexOf('.gif') >= 0 || lp.indexOf('.bmp') >= 0;
                if (!looksImage) return;
                var mt = Number(file.lastModified());
                if (!mt) return;
                // 发送前后 90s 内最新/最近的图片缓存，覆盖选择图片后压缩文件稍晚落盘的情况。
                var delta = Math.abs(nowMs - mt);
                if (delta > 90000) return;
                if (delta < bestScore) {
                    bestScore = delta;
                    bestPath = path;
                }
            } catch (_) {}
        }

        for (var r = 0; r < roots.length; r++) {
            try { visit(JFile.$new(roots[r]), 5); } catch (_) {}
        }
        return bestPath;
    }

    function findRecentVideoCachePath(nowMs) {
        // GUI 发视频主链经常绕过 D8/U1/q8，SendMessage 时 Message 里也没有 url/filePath。
        // 笔记 video_send_chain.md §9.2-B：本体落在 filecache/<vid>/.../<md5>.mp4。
        if (!JFile) return '';
        var roots = [
            '/storage/emulated/0/Android/data/com.tencent.wework/files/filecache',
            '/sdcard/Android/data/com.tencent.wework/files/filecache',
            '/storage/emulated/0/Android/data/com.tencent.wework/files/tempvideocache',
            '/sdcard/Android/data/com.tencent.wework/files/tempvideocache'
        ];
        var bestPath = '';
        var bestScore = 1e18;
        var seen = 0;

        function visit(file, depth) {
            if (!file || depth < 0 || seen > 2500) return;
            seen++;
            try {
                if (!file.exists()) return;
                var path = String(file.getAbsolutePath());
                var lp = path.toLowerCase();
                if (file.isDirectory()) {
                    var children = file.listFiles();
                    if (!children) return;
                    for (var i = 0; i < children.length; i++) visit(children[i], depth - 1);
                    return;
                }
                var looksVideo = lp.indexOf('.mp4') >= 0 || lp.indexOf('.mov') >= 0 ||
                    lp.indexOf('.avi') >= 0 || lp.indexOf('.mkv') >= 0 || lp.indexOf('.m4v') >= 0;
                var inFilecache = lp.indexOf('/filecache/') >= 0 || lp.indexOf('/tempvideocache/') >= 0;
                if (!looksVideo && !inFilecache) return;
                var mt = Number(file.lastModified());
                if (!mt) return;
                var delta = Math.abs(nowMs - mt);
                if (delta > 90000) return;
                // 一些版本 filecache 的视频临时文件没有标准扩展名；用大小过滤兜底。
                if (!looksVideo && inFilecache) {
                    var fs = Number(file.length());
                    if (!fs || fs < 100 * 1024) return;
                }
                var score = delta + (looksVideo ? 0 : 4000);
                if (score < bestScore) {
                    bestScore = score;
                    bestPath = path;
                }
            } catch (_) {}
        }

        for (var r = 0; r < roots.length; r++) {
            try { visit(JFile.$new(roots[r]), 6); } catch (_) {}
        }
        return bestPath;
    }

    function findRecentVideoThumbPath(nowMs) {
        if (!JFile) return '';
        var roots = [
            '/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache',
            '/sdcard/Android/data/com.tencent.wework/files/tempimagecache'
        ];
        var bestPath = '';
        var bestScore = 1e18;
        var seen = 0;

        function visit(file, depth) {
            if (!file || depth < 0 || seen > 1500) return;
            seen++;
            try {
                if (!file.exists()) return;
                var path = String(file.getAbsolutePath());
                var lp = path.toLowerCase();
                if (file.isDirectory()) {
                    var children = file.listFiles();
                    if (!children) return;
                    for (var i = 0; i < children.length; i++) visit(children[i], depth - 1);
                    return;
                }
                if (lp.indexOf('video_thumb') < 0) return;
                var mt = Number(file.lastModified());
                if (!mt) return;
                var delta = Math.abs(nowMs - mt);
                if (delta > 90000) return;
                if (delta < bestScore) {
                    bestScore = delta;
                    bestPath = path;
                }
            } catch (_) {}
        }

        for (var r = 0; r < roots.length; r++) {
            try { visit(JFile.$new(roots[r]), 6); } catch (_) {}
        }
        return bestPath;
    }

    // 从 FileMessage / VideoMessage / LinkMessage 反射拿本地路径
    // q8 分发时用；字段候选覆盖 image/video/file 三种消息类。失败返回空串。
    function extractFilePathFromMessage(fm) {
        if (!fm) return { path: '', thumbPath: '' };
        var out = { path: '', thumbPath: '' };
        try {
            var cls = fm.getClass();
            var pathCands = ['filePath', 'localPath', 'imagePath', 'imgPath', 'videoPath', 'url', 'rawUrl'];
            var thumbCands = ['thumbPath', 'thumbFilePath', 'previewImgUrl', 'thumbUrl'];
            for (var i = 0; i < pathCands.length; i++) {
                try {
                    var f = cls.getDeclaredField(pathCands[i]);
                    f.setAccessible(true);
                    var v = f.get(fm);
                    if (v) { var s = String(v); if (s && s.indexOf('/') >= 0) { out.path = s; break; } }
                } catch (_) { /* field 不存在，继续下一个 */ }
            }
            for (var j = 0; j < thumbCands.length; j++) {
                try {
                    var tf = cls.getDeclaredField(thumbCands[j]);
                    tf.setAccessible(true);
                    var tv = tf.get(fm);
                    if (tv) { var ts = String(tv); if (ts && ts.indexOf('/') >= 0) { out.thumbPath = ts; break; } }
                } catch (_) { /* 同上 */ }
            }
        } catch (_) { /* ignore */ }
        return out;
    }

    function decodeBytesToText(bytes) {
        if (!bytes || !JString) return '';
        try {
            var utf8 = String(JString.$new(bytes, 'UTF-8'));
            var utf16 = '';
            try { utf16 = String(JString.$new(bytes, 'UTF-16LE')); } catch (_) {}
            if (utf16 && utf16 !== utf8) return utf8 + '\n' + utf16;
            return utf8;
        } catch (_) {
            try {
                return String(JString.$new(bytes));
            } catch (_) {
                return '';
            }
        }
    }

    // v1.3 兜底：从 Message 对象直接反射 —— 无论 GUI 走什么上游构造/分发路径，
    // SendMessage 时 msg 一定持有全部信息。probe_fta_wxa_pipeline 用同样技术抓
    // 到过 OA/wxa 卡片。字段候选覆盖 protobuf 生成的 lite/full 两种形态：
    // 有些字段名带 `_` 后缀（protobuf-java-lite）。
    // 返回 { fields: { name: value... }, sub: { subMsgClass -> { name: value } } }
    function dumpMessageFields(msg, maxFieldLen) {
        var out = { fields: {}, sub: {} };
        if (!msg) return out;
        var LIMIT = maxFieldLen || 300;

        function dumpOne(obj, target) {
            if (!obj) return;
            try {
                var cls = obj.getClass();
                var fields = cls.getDeclaredFields();
                for (var i = 0; i < fields.length; i++) {
                    try {
                        var f = fields[i];
                        var mod = f.getModifiers();
                        // 跳过 static / final 常量（protobuf DEFAULT_INSTANCE 之类噪音）
                        if ((mod & 8) !== 0) continue;  // Modifier.STATIC = 8
                        f.setAccessible(true);
                        var n = String(f.getName());
                        var typeName = '';
                        try { typeName = String(f.getType().getName()); } catch (_) {}
                        var v = f.get(obj);
                        if (v === null || v === undefined) continue;
                        var s = '';
                        if (typeName === '[B') {
                            // 复用已验证 probe 的做法：Message.getInfo() 里的
                            // content/appinfo/extraContent/extras 通常是 byte[]，
                            // 直接 String(v) 只会得到 [B@xxxx，必须转 UTF-8。
                            s = decodeBytesToText(v);
                        }
                        if (!s) s = String(v);
                        if (!s) continue;
                        // 太长截断；跳过 protobuf 内部字段（unknownFields/memoizedSize 等）
                        if (n.indexOf('memoized') === 0 || n === 'unknownFields' || n === 'bitField0_') continue;
                        target[n] = s.length > LIMIT ? s.substring(0, LIMIT) : s;
                        // 图片 mType=14 的本地路径常藏在 mediaProgressList 的元素字段里；
                        // 顶层 String(v) 只会输出对象摘要，必须展开一层列表成员。
                        if (n === 'mediaProgressList') dumpNested(v, target, n, 2);
                    } catch (_) {}
                }
            } catch (_) {}
        }

        function dumpNested(obj, target, prefix, depth) {
            if (!obj || depth <= 0) return;
            try {
                if (typeof obj.size === 'function' && typeof obj.get === 'function') {
                    var size = 0;
                    try { size = Math.min(5, obj.size() | 0); } catch (_) { size = 0; }
                    for (var li = 0; li < size; li++) {
                        try { dumpNested(obj.get(li), target, prefix + '[' + li + ']', depth - 1); } catch (_) {}
                    }
                    return;
                }
                var cls = obj.getClass();
                var clsName = '';
                try { clsName = String(cls.getName()); } catch (_) {}
                // Java 数组（含 protobuf 偶尔露出的 Message[]）没有 size()/get()。
                if (clsName && clsName.charAt(0) === '[') {
                    var arrLen = 0;
                    try { arrLen = Math.min(5, obj.length | 0); } catch (_) { arrLen = 0; }
                    for (var ai = 0; ai < arrLen; ai++) {
                        try { dumpNested(obj[ai], target, prefix + '[' + ai + ']', depth - 1); } catch (_) {}
                    }
                    return;
                }
                var fields = cls.getDeclaredFields();
                for (var i = 0; i < fields.length; i++) {
                    try {
                        var f = fields[i];
                        var mod = f.getModifiers();
                        if ((mod & 8) !== 0) continue;
                        f.setAccessible(true);
                        var n = String(f.getName());
                        if (n.indexOf('memoized') === 0 || n === 'unknownFields' || n === 'bitField0_') continue;
                        var typeName = '';
                        try { typeName = String(f.getType().getName()); } catch (_) {}
                        var v = f.get(obj);
                        if (v === null || v === undefined) continue;
                        var s = typeName === '[B' ? decodeBytesToText(v) : String(v);
                        if (s) {
                            var key = prefix + '.' + n;
                            target[key] = s.length > LIMIT ? s.substring(0, LIMIT) : s;
                        }
                    } catch (_) {}
                }
            } catch (_) {}
        }

        dumpOne(msg, out.fields);
        // Message 通常有 getInfo() / getFileMessage() / getLinkMessage() 等 getter
        // 我们试着拿常见 getter 的返回值再 dump 一层
        var getters = ['getInfo', 'getFileMessage', 'getLinkMessage', 'getVideoMessage',
                       'getImageMessage', 'getAppMessage', 'getMiniProgramMessage'];
        for (var gi = 0; gi < getters.length; gi++) {
            try {
                var fn = msg[getters[gi]];
                if (typeof fn === 'function') {
                    var subObj = fn.call(msg);
                    if (subObj) {
                        var subKey = '';
                        try { subKey = getters[gi] + ':' + String(subObj.getClass().getSimpleName()); }
                        catch (_) { subKey = getters[gi]; }
                        var subTarget = {};
                        dumpOne(subObj, subTarget);
                        if (Object.keys(subTarget).length > 0) out.sub[subKey] = subTarget;
                    }
                }
            } catch (_) {}
        }
        return out;
    }

    // 从 dumpMessageFields 的结果里挑出符合语义的字段（多候选，命中就用）
    function pickFromDump(dump, cands) {
        if (!dump) return '';
        // 先在顶层 fields 里找
        for (var i = 0; i < cands.length; i++) {
            var v = dump.fields[cands[i]];
            if (v && String(v).length > 0) return String(v);
            // protobuf-java-lite 字段名带下划线后缀
            v = dump.fields[cands[i] + '_'];
            if (v && String(v).length > 0) return String(v);
        }
        // 再遍历所有 sub 结构
        var subKeys = Object.keys(dump.sub || {});
        for (var s = 0; s < subKeys.length; s++) {
            var subFields = dump.sub[subKeys[s]];
            for (var j = 0; j < cands.length; j++) {
                var sv = subFields[cands[j]];
                if (sv && String(sv).length > 0) return String(sv);
                sv = subFields[cands[j] + '_'];
                if (sv && String(sv).length > 0) return String(sv);
            }
        }
        return '';
    }

    function collectDumpTexts(dump) {
        var texts = [];
        function add(v) {
            if (!v) return;
            var s = String(v);
            if (!s || s.indexOf('[B@') === 0) return;
            texts.push(s);
        }
        Object.keys(dump.fields || {}).forEach(function (k) { add(dump.fields[k]); });
        Object.keys(dump.sub || {}).forEach(function (sk) {
            var fields = dump.sub[sk] || {};
            Object.keys(fields).forEach(function (k) { add(fields[k]); });
        });
        return texts;
    }

    function firstRegex(texts, regex) {
        for (var i = 0; i < texts.length; i++) {
            var m = regex.exec(texts[i]);
            if (m) return String(m[1] || m[0] || '');
        }
        return '';
    }

    function xmlText(texts, tag) {
        var re = new RegExp('<' + tag + '>(?:<!\\\\[CDATA\\\\[)?([\\\\s\\\\S]{1,300}?)(?:\\\\]\\\\]>)?</' + tag + '>', 'i');
        var v = firstRegex(texts, re);
        if (!v) return '';
        return v.replace(/^\s+|\s+$/g, '');
    }

    function scanMediaProgressListForVideo(dump) {
        // mType=23 等场景：视频本地路径常藏在 Message.getInfo().mediaProgressList 元素字段里，
        // 与 mType=14 图片同源；顶层 url/rawUrl 往往为空。
        var pathCands = ['filePath', 'localPath', 'url', 'rawUrl', 'videoPath'];
        var absVideoRe = /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]+?\.(?:mp4|mov|avi|mkv|m4v))/i;
        var absFilecacheRe = /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]*?filecache[^\s"'<>\\\x00]+)/i;

        function scanFields(fields) {
            if (!fields) return '';
            var keys = Object.keys(fields);
            for (var i = 0; i < keys.length; i++) {
                var k = keys[i];
                var v = String(fields[k] || '');
                if (!v) continue;
                if (k.indexOf('mediaProgressList') >= 0) {
                    for (var j = 0; j < pathCands.length; j++) {
                        if (k.indexOf(pathCands[j]) >= 0) {
                            var m1 = absVideoRe.exec(v);
                            if (m1) return String(m1[1] || m1[0] || '');
                            var m2 = absFilecacheRe.exec(v);
                            if (m2) return String(m2[1] || m2[0] || '');
                        }
                    }
                    var m3 = absVideoRe.exec(v);
                    if (m3) return String(m3[1] || m3[0] || '');
                    var m4 = absFilecacheRe.exec(v);
                    if (m4) return String(m4[1] || m4[0] || '');
                }
            }
            return '';
        }

        var found = scanFields(dump.fields);
        if (found) return found;
        var subKeys = Object.keys(dump.sub || {});
        for (var s = 0; s < subKeys.length; s++) {
            found = scanFields(dump.sub[subKeys[s]]);
            if (found) return found;
        }
        return '';
    }

    function extractVideoFromDump(dump) {
        var out = {
            videoPath: pickFromDump(dump, ['url', 'rawUrl', 'filePath', 'videoPath', 'localPath']),
            thumbPath: pickFromDump(dump, ['previewImgUrl', 'thumbPath', 'thumbFilePath', 'thumbUrl'])
        };
        var texts = collectDumpTexts(dump);
        if (!out.videoPath) {
            out.videoPath = scanMediaProgressListForVideo(dump);
        }
        if (!out.videoPath) {
            out.videoPath = firstRegex(texts, /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]+?\.(?:mp4|mov|avi|mkv|m4v))/i);
        }
        if (!out.videoPath) {
            // 部分版本仅暴露 app 私有目录路径：/data/user/0/com.tencent.wework/files/share/...
            out.videoPath = firstRegex(texts, /(\/data\/user\/0\/com\.tencent\.wework\/files\/[^\s"'<>\\\x00]+?\.(?:mp4|mov|avi|mkv|m4v))/i);
        }
        if (!out.videoPath) {
            // 文件名含中文时，mInfo 文本里常被转成八进制字节串（\347\273\203...）。
            out.videoPath = firstRegex(
                texts,
                /(\/data\/user\/0\/com\.tencent\.wework\/files\/(?:\\[0-7]{3}|[^\s"'<>\x00])+?\.(?:mp4|mov|avi|mkv|m4v))/i
            );
        }
        if (!out.videoPath) {
            out.videoPath = firstRegex(texts, /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]*?filecache[^\s"'<>\\\x00]+)/i);
        }
        if (!out.videoPath) {
            var rel = firstRegex(texts, /(filecache\/[^\s"'<>\\\x00]+?\.(?:mp4|mov|avi|mkv|m4v))/i);
            if (rel) {
                out.videoPath = '/storage/emulated/0/Android/data/com.tencent.wework/files/' + rel;
            }
        }
        if (!out.thumbPath) {
            // 企微视频首帧常见为 video_thumb/*.wwdata，也可能是 jpg/png。
            out.thumbPath = firstRegex(texts, /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]*?(?:video_thumb|thumb)[^\s"'<>\\\x00]*?\.(?:wwdata|jpg|jpeg|png))/i);
        }
        if (out.videoPath && !isDevicePath(out.videoPath)) out.videoPath = '';
        return out;
    }

    function extractImageFromDump(dump) {
        var out = { imagePath: '' };
        var texts = collectDumpTexts(dump);
        // 1) 主图（最优先）：mType=14 GUI 发图会把全尺寸图写到
        //    .../files/imagecache/imagemsg2/<shard>/<md5>.<n>，扩展名是数字（如 .0），
        //    实为 JPEG。真机实测：该文件在 SendMessage 时刻新建，是发送对应的真实图片。
        //    路径存在 Message.content 的 protobuf 文本里（mInfo.toString 可见）。
        out.imagePath = firstRegex(texts, /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]*?imagecache\/imagemsg2\/[^\s"'<>\\\x00]+)/i);
        // 2) 退一步：imagecache 下任意绝对路径（排除视频首帧 video_thumb）
        if (!out.imagePath) {
            var p2 = firstRegex(texts, /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]*?imagecache[^\s"'<>\\\x00]+)/i);
            if (p2 && p2.toLowerCase().indexOf('video_thumb') < 0) out.imagePath = p2;
        }
        // 3) 常规图片扩展名绝对路径
        if (!out.imagePath) {
            out.imagePath = firstRegex(texts, /(\/(?:storage\/emulated\/0|sdcard)\/[^\s"'<>\\\x00]+?\.(?:png|jpg|jpeg|webp|gif|bmp))/i);
        }
        // 4) 命名字段兜底（部分构造路径会填 filePath/localPath）
        if (!out.imagePath) {
            var pf = pickFromDump(dump, ['filePath', 'imagePath', 'imgPath', 'localPath', 'url', 'rawUrl']);
            if (pf) out.imagePath = pf;
        }
        if (out.imagePath && !isDevicePath(out.imagePath)) out.imagePath = '';
        if (out.imagePath && classifyMediaPath(out.imagePath) === 'video') out.imagePath = '';
        return out;
    }

    function extractOaFromDump(dump) {
        var out = {
            link: pickFromDump(dump, ['url', 'linkUrl', 'jumpUrl']),
            title: pickFromDump(dump, ['title']),
            desc: pickFromDump(dump, ['desc', 'description', 'digest']),
            imgUrl: pickFromDump(dump, ['previewImgUrl', 'thumbUrl', 'imgUrl', 'iconUrl'])
        };
        var texts = collectDumpTexts(dump);
        if (!out.link) out.link = firstRegex(texts, /(https?:\/\/[^\s"'<>\\\x00]+)/i);
        if (!out.title) out.title = xmlText(texts, 'title');
        if (!out.desc) out.desc = xmlText(texts, 'des') || xmlText(texts, 'desc') || xmlText(texts, 'digest');
        if (!out.imgUrl) out.imgUrl = xmlText(texts, 'thumburl') || xmlText(texts, 'thumbUrl');
        if (!out.imgUrl) {
            var allUrls = [];
            for (var i = 0; i < texts.length; i++) {
                var re = /(https?:\/\/[^\s"'<>\\\x00]+)/ig;
                var m = null;
                while ((m = re.exec(texts[i])) !== null) allUrls.push(String(m[1] || m[0] || ''));
            }
            for (var u = 0; u < allUrls.length; u++) {
                if (allUrls[u] !== out.link) { out.imgUrl = allUrls[u]; break; }
            }
        }
        return out;
    }

    // ------------------- voice / video (fallback): h8y.U1 -------------------
    // 签名 (String path, int a, int b, int voiceTime|duration, boolean) —— voice 与 video
    // 的 FileMessage 构造器共用。silk 路径 → pendingVoices；mp4 路径 → pendingVideos。
    // 血泪教训：若不按后缀分派，video 发送时 U1 push 到 voice buffer，SendMessage(mType=23)
    // 看到 pendingVideos 空 → dispatch_miss video。
    try {
        var u1 = h8y.U1.overload('java.lang.String', 'int', 'int', 'int', 'boolean');
        u1.implementation = function (path, a, b, voiceTime, flag) {
            var t0 = Date.now();
            gc(t0);
            var p = String(path || '');
            var cls = classifyMediaPath(p);
            if (p && cls === 'video') {
                pendingVideos.push({ videoPath: p, thumbPath: '', tsMs: t0, from: 'U1' });
                send({ event: 'upstream_seen', kind: 'video', hook: 'h8y.U1', videoPath: p, tsMs: t0 });
            } else {
                // 默认按 voice 处理（.silk 或未知）—— 保持原语义
                pendingVoices.push({ path: p, voiceTime: Number(voiceTime || 0), tsMs: t0 });
            }
            return u1.call(this, path, a, b, voiceTime, flag);
        };
        console.log('[fta_lite] hooked h8y.U1 (voice/video)');
    } catch (eU1) {
        send({ event: 'error', stage: 'hook_u1', message: String(eU1) });
    }

    // ------------------- image fallback: h8y.T1 -------------------
    // T1(String,int,int,int) 也是历史 probe 中出现过的 FileMessage 构造器。
    // 当前图片 mType=14 若绕过 V1，可在这里按路径后缀补 pending。
    try {
        var t1Method = h8y.T1;
        var t1Overloads = t1Method.overloads;
        for (var iT = 0; iT < t1Overloads.length; iT++) {
            (function (o) {
                o.implementation = function () {
                    var t0 = Date.now();
                    gc(t0);
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    var p = argv.length >= 1 ? String(argv[0] || '') : '';
                    var cls = classifyMediaPath(p);
                    if (p && isDevicePath(p) && cls === 'image') {
                        pendingImages.push({ path: p, tsMs: t0 });
                        send({ event: 'upstream_seen', kind: 'image', hook: 'h8y.T1', path: p, tsMs: t0 });
                    }
                    return o.apply(this, argv);
                };
            })(t1Overloads[iT]);
        }
        console.log('[fta_lite] hooked h8y.T1 (image fallback, ' + t1Overloads.length + ' overloads)');
        send({ event: 'hooked', hook: 'h8y.T1', overloads: t1Overloads.length });
    } catch (eT1) {
        send({ event: 'error', stage: 'hook_t1', message: String(eT1) });
    }

    // ------------------- image / video (fallback path): h8y.V1 -------------------
    // 真机实测（notes/image_send_chain.md §2/§4）：GUI 手动发图主链是
    //   h8y.V1(String path, int w, int h, int, boolean, Function2) -> FileMessage
    //   → h8y.V8(fm) → h8y.q8(context, convId, fm, msgType, ...) → SendMessage
    // 旧脚本 hook 的 h8y.f8 只是历史 fallback，GUI 从不走 —— 这是本轮
    // "除 voice 外全接不到" 的根因（lite_diag3.log 里 pendingSize 恒为 0）。
    // V1 同时会被 video 分支复用（q8 fallback），按路径后缀分类入队。
    try {
        var v1Method = h8y.V1;
        var v1Overloads = v1Method.overloads;
        for (var iV = 0; iV < v1Overloads.length; iV++) {
            (function (o) {
                o.implementation = function () {
                    var t0 = Date.now();
                    gc(t0);
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    var p = argv.length >= 1 ? String(argv[0] || '') : '';
                    var cls = classifyMediaPath(p);
                    if (p && cls === 'image') {
                        pendingImages.push({ path: p, tsMs: t0 });
                        send({ event: 'upstream_seen', kind: 'image', hook: 'h8y.V1', path: p, tsMs: t0 });
                    } else if (p && cls === 'video') {
                        // V1 无 thumb 参数，thumb 会在 D8 里补齐；先占位入队，
                        // SendMessage 消费时若 D8 buffer 有更新条目，优先取 D8。
                        pendingVideos.push({ videoPath: p, thumbPath: '', tsMs: t0, from: 'V1' });
                        send({ event: 'upstream_seen', kind: 'video', hook: 'h8y.V1', videoPath: p, tsMs: t0 });
                    }
                    return o.apply(this, argv);
                };
            })(v1Overloads[iV]);
        }
        console.log('[fta_lite] hooked h8y.V1 (image/video, ' + v1Overloads.length + ' overloads)');
        send({ event: 'hooked', hook: 'h8y.V1', overloads: v1Overloads.length });
    } catch (eV1) {
        send({ event: 'error', stage: 'hook_v1', message: String(eV1) });
    }

    // ------------------- image fallback: h8y.W1 -------------------
    // 历史 image probe 证明发图链存在 V1 -> V8 -> W1(List, ...) -> SendMessage。
    // 当前 mType=14 场景 V1/q8 未触发，但 MessageInfo 带 mediaProgressList；
    // 补 hook W1，从 List 中的 FileMessage/Message 元素反射本地图片路径。
    try {
        var w1Method = h8y.W1;
        var w1Overloads = w1Method.overloads;
        for (var iW = 0; iW < w1Overloads.length; iW++) {
            (function (o) {
                o.implementation = function () {
                    var t0 = Date.now();
                    gc(t0);
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    var listObj = argv.length >= 1 ? argv[0] : null;
                    try {
                        if (listObj && typeof listObj.size === 'function' && typeof listObj.get === 'function') {
                            var size = Math.min(10, listObj.size() | 0);
                            for (var wi = 0; wi < size; wi++) {
                                var item = null;
                                try { item = listObj.get(wi); } catch (_) { item = null; }
                                var extracted = extractFilePathFromMessage(item);
                                var p = extracted.path;
                                var cls = classifyMediaPath(p);
                                if (p && isDevicePath(p) && cls === 'image') {
                                    pendingImages.push({ path: p, tsMs: t0 });
                                    send({ event: 'upstream_seen', kind: 'image', hook: 'h8y.W1', path: p, index: wi, tsMs: t0 });
                                }
                            }
                        }
                    } catch (_) {}
                    return o.apply(this, argv);
                };
            })(w1Overloads[iW]);
        }
        console.log('[fta_lite] hooked h8y.W1 (image fallback, ' + w1Overloads.length + ' overloads)');
        send({ event: 'hooked', hook: 'h8y.W1', overloads: w1Overloads.length });
    } catch (eW1) {
        send({ event: 'error', stage: 'hook_w1', message: String(eW1) });
    }

    // ------------------- video (primary path): h8y.D8 -------------------
    // 真机实测（notes/video_send_chain.md §9.3）：视频主发送链是
    //   h8y.D8(Activity, long convId, String videoPath, String, String thumbPath, boolean)
    // 旧 hook 的 h8y.T8 在当前运行时仅存在 T8(List) 重载，与 GUI/引擎路径无关。
    try {
        var d8Method = h8y.D8;
        var d8Overloads = d8Method.overloads;
        for (var iD = 0; iD < d8Overloads.length; iD++) {
            (function (o) {
                o.implementation = function () {
                    var t0 = Date.now();
                    gc(t0);
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    var vpath = argv.length >= 3 ? String(argv[2] || '') : '';
                    var tpath = argv.length >= 5 ? String(argv[4] || '') : '';
                    if (vpath) {
                        pendingVideos.push({ videoPath: vpath, thumbPath: tpath, tsMs: t0, from: 'D8' });
                        send({ event: 'upstream_seen', kind: 'video', hook: 'h8y.D8', videoPath: vpath, thumbPath: tpath, tsMs: t0 });
                    }
                    return o.apply(this, argv);
                };
            })(d8Overloads[iD]);
        }
        console.log('[fta_lite] hooked h8y.D8 (video primary, ' + d8Overloads.length + ' overloads)');
    } catch (eD8) {
        send({ event: 'error', stage: 'hook_d8', message: String(eD8) });
    }

    // ------------------- oa / wxa: h8y.f2 (all overloads) -------------------
    // 已知主签名：(String link, String title, String desc, String img, [B imgData)。
    // GUI 分享公众号/小程序 可能走不同 overload（例如省略 imgData 或换类型）—— 全 hook。
    // 按 argv 长度识别：>=4 args & 前几个是 String → 认为是 link/title/desc/img。
    try {
        var f2Method = h8y.f2;
        var f2Overloads = f2Method.overloads;
        for (var iF2 = 0; iF2 < f2Overloads.length; iF2++) {
            (function (o) {
                o.implementation = function () {
                    var t0 = Date.now();
                    gc(t0);
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    var link = argv.length >= 1 ? String(argv[0] || '') : '';
                    var title = argv.length >= 2 ? String(argv[1] || '') : '';
                    var desc = argv.length >= 3 ? String(argv[2] || '') : '';
                    var img = argv.length >= 4 ? String(argv[3] || '') : '';
                    var hasImgData = argv.length >= 5 && !!argv[4];
                    // 只有当至少有 link/title 之一时才入队，避免无关重载噪音
                    if (link || title) {
                        pendingOas.push({
                            link: link, title: title, desc: desc,
                            imgUrl: img, hasImgData: hasImgData, tsMs: t0
                        });
                        send({ event: 'upstream_seen', kind: 'oa', hook: 'h8y.f2', title: title, link: link, argc: argv.length, tsMs: t0 });
                    }
                    return o.apply(this, argv);
                };
            })(f2Overloads[iF2]);
        }
        console.log('[fta_lite] hooked h8y.f2 (oa/wxa, ' + f2Overloads.length + ' overloads)');
    } catch (eF2) {
        send({ event: 'error', stage: 'hook_f2', message: String(eF2) });
    }

    // ------------------- generic dispatch: h8y.q8 (image/video/file) -------------------
    // q8(Context, long convId, FileMessage, int msgType, zi90, ISendMessageCallback)
    // 是 FileMessage 的通用分发入口，msgType 就在参数里 —— 无需靠 SendMessage
    // 反推。GUI 手动发视频若不走 D8 会走 q8；发图/文件也走这条。
    try {
        var q8Method = h8y.q8;
        var q8Overloads = q8Method.overloads;
        for (var iQ = 0; iQ < q8Overloads.length; iQ++) {
            (function (o) {
                o.implementation = function () {
                    var t0 = Date.now();
                    gc(t0);
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    // 定位 msgType（int）与 FileMessage：q8 常见形态是 (Ctx, long, FM, int, ...)
                    var fm = null;
                    var mt = -1;
                    for (var j = 0; j < argv.length; j++) {
                        var a = argv[j];
                        if (a && typeof a === 'object' && a.getClass) {
                            var cn = '';
                            try { cn = String(a.getClass().getName()); } catch (_) {}
                            if (cn.indexOf('FileMessage') >= 0 || cn.indexOf('Message') >= 0) fm = a;
                        }
                    }
                    // msgType 通常是第 4 个参数（idx=3）；也可能是最后一个 int 前
                    if (argv.length >= 4) {
                        var maybeMt = argv[3];
                        if (typeof maybeMt === 'number') mt = maybeMt | 0;
                    }
                    var extracted = extractFilePathFromMessage(fm);
                    var p = extracted.path;
                    var tp = extracted.thumbPath;
                    send({ event: 'upstream_seen', kind: 'q8', hook: 'h8y.q8', msgType: mt, path: p, thumbPath: tp, argc: argv.length, tsMs: t0 });
                    if (p) {
                        // 按 msgType 优先分派；否则按路径后缀
                        if ((mt === 7 || mt === 14) && isDevicePath(p)) {
                            pendingImages.push({ path: p, tsMs: t0 });
                        } else if (mt === 5 || mt === 23 || mt === 43 || mt === 62) {
                            pendingVideos.push({ videoPath: p, thumbPath: tp, tsMs: t0, from: 'q8' });
                        } else {
                            var pc = classifyMediaPath(p);
                            if (pc === 'image' && isDevicePath(p)) pendingImages.push({ path: p, tsMs: t0 });
                            else if (pc === 'video') pendingVideos.push({ videoPath: p, thumbPath: tp, tsMs: t0, from: 'q8' });
                        }
                    }
                    return o.apply(this, argv);
                };
            })(q8Overloads[iQ]);
        }
        console.log('[fta_lite] hooked h8y.q8 (generic dispatch, ' + q8Overloads.length + ' overloads)');
    } catch (eQ8) {
        send({ event: 'error', stage: 'hook_q8', message: String(eQ8) });
    }

    // ------------------- oa dispatch: h8y.s8 -------------------
    // s8(Context, Conversation, Message, zi90, ISendMessageCallback) —— GUI 分享
    // 公众号/小程序卡片走这里下发。仅诊断上报，因为 f2 已经把结构化字段入队了。
    try {
        var s8Method = h8y.s8;
        var s8Overloads = s8Method.overloads;
        for (var iS = 0; iS < s8Overloads.length; iS++) {
            (function (o) {
                o.implementation = function () {
                    var t0 = Date.now();
                    gc(t0);
                    var argv = [];
                    for (var k = 0; k < arguments.length; k++) argv.push(arguments[k]);
                    send({ event: 'upstream_seen', kind: 'oa_dispatch', hook: 'h8y.s8', argc: argv.length, tsMs: t0 });
                    return o.apply(this, argv);
                };
            })(s8Overloads[iS]);
        }
        console.log('[fta_lite] hooked h8y.s8 (oa/wxa dispatch, ' + s8Overloads.length + ' overloads)');
    } catch (eS8) {
        send({ event: 'error', stage: 'hook_s8', message: String(eS8) });
    }

    // ------------------- SendMessage: mType 分派 -------------------
    try {
        var CS = Java.use('com.tencent.wework.foundation.logic.ConversationService');
        var sm0 = CS.SendMessage.overload(
            'com.tencent.wework.foundation.model.Conversation',
            'com.tencent.wework.foundation.model.Message',
            'com.tencent.wework.foundation.callback.ISendMessageCallback'
        );
        sm0.implementation = function (conv, msg, cb) {
            var t0 = Date.now();
            gc(t0);
            var convStr = String(conv || '');
            var msgStr = String(msg || '');
            var ids = parseConvIds(convStr);
            var mType = parseMsgType(msgStr);
            var lId = parseLId(msgStr);
            // Java Message identityHashCode 兜底（lId 未回填时也能去重）
            var msgHc = '';
            try {
                if (msg && JSystem) msgHc = String(JSystem.identityHashCode(msg));
            } catch (eHc) { /* ignore */ }

            // P0：同一条消息只处理一次 —— 两层去重任一命中即早返回
            if (ids.remote === FTA_REMOTE_ID) {
                if ((lId && seenLIds[lId]) || (msgHc && seenMsgHc[msgHc])) {
                    return sm0.call(this, conv, msg, cb);
                }
                if (lId) seenLIds[lId] = t0;
                if (msgHc) seenMsgHc[msgHc] = t0;
            }

            if (ids.remote === FTA_REMOTE_ID) {
                // 观察面：全 mType 上报（F1.5 保留，方便新增类型的探索）
                var preview = msgStr.length > MSG_PREVIEW_BYTES
                    ? msgStr.substring(0, MSG_PREVIEW_BYTES)
                    : msgStr;
                send({
                    event: 'fta_asset_seen',
                    mType: mType,
                    convLocal: ids.local,
                    convRemote: ids.remote,
                    msgPreview: preview,
                    tsMs: t0,
                    hookElapsedMs: Date.now() - t0
                });

                // 文字：在传输助手输入框发出的纯文本 / 富文本。
                if (mType === 0 || mType === 1 || mType === 2) {
                    emitPlainText(ids, msg, mType, t0);
                }
                // voice
                else if (mType === 9 && pendingVoices.length > 0) {
                    var voice = pendingVoices[pendingVoices.length - 1];
                    send({
                        event: 'fta_voice_seen',
                        convLocal: ids.local, convRemote: ids.remote,
                        sourcePath: voice.path, voiceTime: voice.voiceTime,
                        tsMs: t0, hookElapsedMs: Date.now() - t0
                    });
                }
                // image: mType=7 是历史实测，当前 GUI 也会出现 mType=14；
                // 和 video/OA 一样，当上游 V1/q8 没命中时从 Message 反射兜底。
                else if (mType === 7 || mType === 14) {
                    var img = latestInWindow(pendingImages, t0, UPSTREAM_WINDOW_MS);
                    if (img) {
                        send({ event: 'fta_image_seen', convLocal: ids.local, convRemote: ids.remote, sourcePath: img.path, mType: mType, source: 'pending', tsMs: t0, hookElapsedMs: Date.now() - t0 });
                    } else {
                        var dumpImg = dumpMessageFields(msg, 3000);
                        var reflectedImg = extractImageFromDump(dumpImg);
                        if (reflectedImg.imagePath) {
                            send({ event: 'fta_image_seen', convLocal: ids.local, convRemote: ids.remote, sourcePath: reflectedImg.imagePath, mType: mType, source: 'reflect', tsMs: t0, hookElapsedMs: Date.now() - t0 });
                        } else {
                            var cacheImgPath = findRecentImageCachePath(t0);
                            if (cacheImgPath) {
                                send({ event: 'fta_image_seen', convLocal: ids.local, convRemote: ids.remote, sourcePath: cacheImgPath, mType: mType, source: 'tempimagecache', tsMs: t0, hookElapsedMs: Date.now() - t0 });
                            } else {
                                var imgFieldNames = Object.keys(dumpImg.fields);
                                var imgSubNames = {};
                                Object.keys(dumpImg.sub).forEach(function (k) {
                                    imgSubNames[k] = Object.keys(dumpImg.sub[k]);
                                });
                                // 诊断：把解码后的字段"值"也带上（已在 dumpMessageFields 里截断到 3000）。
                                // 用来看清 mType=14 图片消息把本地路径/md5/CDN 引用藏在哪个字段。
                                send({ event: 'dispatch_miss', kind: 'image', mType: mType, pendingSize: pendingImages.length, tsMs: t0, msgFields: imgFieldNames, msgSub: imgSubNames, msgDump: dumpImg });
                            }
                        }
                    }
                }
                // video: 先用 pending buffer（若上游 hook 命中）；否则反射 msg 兜底
                // mType=5 是当前企微 GUI 发视频的实际值（真机联调实测），
                // 23/43/62 保留为旧版本兼容（历史 probe 覆盖，仍在其他 build 上出现）。
                else if (mType === 5 || mType === 23 || mType === 43 || mType === 62) {
                    var vid = latestVideoInWindow(pendingVideos, t0, UPSTREAM_WINDOW_MS);
                    var vpath = vid ? vid.videoPath : '';
                    var tpath = vid ? vid.thumbPath : '';
                    var vSource = vid ? ('pending:' + (vid.from || 'unknown')) : '';
                    if (!vpath) {
                        // 反射兜底 —— 参考 notes/video_send_chain.md §9.1-A：
                        // VideoMessage 字段 url/rawUrl = videoPath；previewImgUrl = thumbPath
                        var dump = dumpMessageFields(msg, 8000);
                        var reflectedVideo = extractVideoFromDump(dump);
                        vpath = reflectedVideo.videoPath;
                        tpath = reflectedVideo.thumbPath;
                        vSource = vpath ? 'reflect' : '';
                    }
                    if (!vpath) {
                        var cacheVidPath = findRecentVideoCachePath(t0);
                        if (cacheVidPath) {
                            vpath = cacheVidPath;
                            vSource = 'filecache';
                            if (!tpath) tpath = findRecentVideoThumbPath(t0);
                        }
                    }
                    if (!vpath) {
                        var missDump = dumpMessageFields(msg, 8000);
                        var fieldNames = Object.keys(missDump.fields);
                        var subNames = {};
                        Object.keys(missDump.sub).forEach(function (k) {
                            subNames[k] = Object.keys(missDump.sub[k]);
                        });
                        send({ event: 'dispatch_miss', kind: 'video', mType: mType, pendingSize: pendingVideos.length, tsMs: t0, msgFields: fieldNames, msgSub: subNames, msgDump: missDump });
                    }
                    if (vpath) {
                        send({ event: 'fta_video_seen', convLocal: ids.local, convRemote: ids.remote, videoPath: vpath, thumbPath: tpath, source: vSource, tsMs: t0, hookElapsedMs: Date.now() - t0 });
                    }
                }
                // 小程序 contentType 78。禁止并入公众号：字段在 WeAppMessage，不在 LinkMessage。
                else if (mType === 78) {
                    var wxa = parseWxaCard(msg, true);
                    if (wxa && wxa.weappBodyB64) {
                        send({
                            event: 'fta_wxa_seen',
                            convLocal: ids.local,
                            convRemote: ids.remote,
                            mType: 78,
                            username: wxa.username,
                            pagepath: wxa.pagepath,
                            title: wxa.title,
                            appName: wxa.appName,
                            weappIconUrl: wxa.weappIconUrl,
                            desc: wxa.desc,
                            type: wxa.type,
                            weappBodyB64: wxa.weappBodyB64,
                            source: 'proto',
                            tsMs: t0,
                            hookElapsedMs: Date.now() - t0
                        });
                    } else {
                        send({ event: 'dispatch_miss', kind: 'wxa', mType: mType, tsMs: t0 });
                    }
                }
                // 公众号 contentType 13
                else if (mType === 13) {
                    var wxaOnOaType = parseWxaCard(msg, false);
                    if (wxaOnOaType && wxaOnOaType.weappBodyB64) {
                        send({
                            event: 'fta_wxa_seen',
                            convLocal: ids.local,
                            convRemote: ids.remote,
                            mType: 78,
                            username: wxaOnOaType.username,
                            pagepath: wxaOnOaType.pagepath,
                            title: wxaOnOaType.title,
                            appName: wxaOnOaType.appName,
                            weappIconUrl: wxaOnOaType.weappIconUrl,
                            desc: wxaOnOaType.desc,
                            type: wxaOnOaType.type,
                            weappBodyB64: wxaOnOaType.weappBodyB64,
                            source: 'proto',
                            tsMs: t0,
                            hookElapsedMs: Date.now() - t0
                        });
                    } else {
                    var oa = latestInWindow(pendingOas, t0, UPSTREAM_WINDOW_MS);
                    var link = oa ? oa.link : '';
                    var title = oa ? oa.title : '';
                    var desc = oa ? oa.desc : '';
                    var imgUrl = oa ? oa.imgUrl : '';
                    var hasImg = oa ? oa.hasImgData : false;
                    var oaSource = oa ? 'pending' : '';
                    var oaAppname = '';
                    // 优先按 proto 解析 content（拿到干净 title/公众号名 desc）。
                    // f2 pending 只在"我们工具重建"时才有；真实分享走不到 f2，
                    // 且即便命中 pending，proto 里的 title/desc 更完整可信。
                    var parsed = parseOaLinkMessage(msg);
                    if (parsed && (parsed.title || parsed.link)) {
                        link = parsed.link || link;
                        title = parsed.title || title;
                        desc = parsed.desc || desc;
                        imgUrl = parsed.imgUrl || imgUrl;
                        oaAppname = parsed.appname || '';
                        oaSource = 'proto';
                    }
                    if (!link && !title) {
                        // 反射兜底 —— 参考 notes/oa_send_chain.md §7.3：
                        // LinkMessage 字段 url/title/desc/previewImgUrl
                        var dumpOa = dumpMessageFields(msg, 3000);
                        var reflectedOa = extractOaFromDump(dumpOa);
                        link = reflectedOa.link;
                        title = reflectedOa.title;
                        desc = reflectedOa.desc;
                        imgUrl = reflectedOa.imgUrl;
                        oaSource = (link || title) ? 'reflect' : '';
                        if (!link && !title) {
                            var oaFieldNames = Object.keys(dumpOa.fields);
                            var oaSubNames = {};
                            Object.keys(dumpOa.sub).forEach(function (k) {
                                oaSubNames[k] = Object.keys(dumpOa.sub[k]);
                            });
                            send({ event: 'dispatch_miss', kind: 'oa', mType: mType, pendingSize: pendingOas.length, tsMs: t0, msgFields: oaFieldNames, msgSub: oaSubNames });
                        }
                    }
                    if (link || title) {
                        send({ event: 'fta_oa_seen', convLocal: ids.local, convRemote: ids.remote, link: link, title: title, desc: desc, imgUrl: imgUrl, appname: oaAppname, hasImgData: hasImg, source: oaSource, tsMs: t0, hookElapsedMs: Date.now() - t0 });
                    }
                    }
                }
                else {
                    // 兜底：未知 mType 也尝试提取视频路径，兼容客户端/上传入口差异。
                    var anyDump = dumpMessageFields(msg, 8000);
                    var anyVideo = extractVideoFromDump(anyDump);
                    if (anyVideo.videoPath) {
                        send({
                            event: 'fta_video_seen',
                            convLocal: ids.local,
                            convRemote: ids.remote,
                            videoPath: anyVideo.videoPath,
                            thumbPath: anyVideo.thumbPath || '',
                            source: 'reflect:unknown_mtype',
                            mType: mType,
                            tsMs: t0,
                            hookElapsedMs: Date.now() - t0
                        });
                    }
                }
            }
            return sm0.call(this, conv, msg, cb);
        };
        try {
            var sm1 = CS.SendMessage.overload(
                'com.tencent.wework.foundation.model.ConversationRef',
                'com.tencent.wework.foundation.model.Message',
                'com.tencent.wework.foundation.callback.ISendMessageCallback'
            );
            sm1.implementation = function (convRef, msg, cb) {
                var t0 = Date.now();
                gc(t0);
                var convStr = String(convRef || '');
                var msgStr = String(msg || '');
                var ids = parseConvIds(convStr);
                var mType = parseMsgType(msgStr);
                var lId = parseLId(msgStr);
                var dumpForIds = null;
                if (!ids.local || !ids.remote) {
                    dumpForIds = dumpMessageFields(msg, 3000);
                    if (!ids.local) ids.local = pickFromDump(dumpForIds, ['conversationId']);
                    if (!ids.remote) ids.remote = pickFromDump(dumpForIds, ['remoteId']);
                }
                var msgHc = '';
                try {
                    if (msg && JSystem) msgHc = String(JSystem.identityHashCode(msg));
                } catch (eHc1) { /* ignore */ }

                var maybeFtaImage = (mType === 7 || mType === 14) && pendingImages.length > 0;
                if (ids.remote === FTA_REMOTE_ID || maybeFtaImage) {
                    if ((lId && seenLIds[lId]) || (msgHc && seenMsgHc[msgHc])) {
                        return sm1.call(this, convRef, msg, cb);
                    }
                    if (lId) seenLIds[lId] = t0;
                    if (msgHc) seenMsgHc[msgHc] = t0;

                    var preview = msgStr.length > MSG_PREVIEW_BYTES
                        ? msgStr.substring(0, MSG_PREVIEW_BYTES)
                        : msgStr;
                    send({
                        event: 'fta_asset_seen',
                        source: 'ConversationRef',
                        mType: mType,
                        convLocal: ids.local,
                        convRemote: ids.remote || FTA_REMOTE_ID,
                        msgPreview: preview,
                        tsMs: t0,
                        hookElapsedMs: Date.now() - t0
                    });

                    if (ids.remote === FTA_REMOTE_ID && (mType === 0 || mType === 1 || mType === 2)) {
                        emitPlainText(ids, msg, mType, t0);
                    } else if (mType === 7 || mType === 14) {
                        var img = latestInWindow(pendingImages, t0, UPSTREAM_WINDOW_MS);
                        if (img) {
                            send({ event: 'fta_image_seen', convLocal: ids.local, convRemote: ids.remote || FTA_REMOTE_ID, sourcePath: img.path, mType: mType, source: 'pending:ConversationRef', tsMs: t0, hookElapsedMs: Date.now() - t0 });
                        } else {
                            var dumpImg = dumpForIds || dumpMessageFields(msg, 3000);
                            var reflectedImg = extractImageFromDump(dumpImg);
                            if (reflectedImg.imagePath) {
                                send({ event: 'fta_image_seen', convLocal: ids.local, convRemote: ids.remote || FTA_REMOTE_ID, sourcePath: reflectedImg.imagePath, mType: mType, source: 'reflect:ConversationRef', tsMs: t0, hookElapsedMs: Date.now() - t0 });
                            } else {
                                var cacheImgPath = findRecentImageCachePath(t0);
                                if (cacheImgPath) {
                                    send({ event: 'fta_image_seen', convLocal: ids.local, convRemote: ids.remote || FTA_REMOTE_ID, sourcePath: cacheImgPath, mType: mType, source: 'tempimagecache:ConversationRef', tsMs: t0, hookElapsedMs: Date.now() - t0 });
                                } else {
                                    var imgFieldNames = Object.keys(dumpImg.fields);
                                    var imgSubNames = {};
                                    Object.keys(dumpImg.sub).forEach(function (k) {
                                        imgSubNames[k] = Object.keys(dumpImg.sub[k]);
                                    });
                                    send({ event: 'dispatch_miss', kind: 'image', source: 'ConversationRef', mType: mType, pendingSize: pendingImages.length, tsMs: t0, msgFields: imgFieldNames, msgSub: imgSubNames, msgDump: dumpImg });
                                }
                            }
                        }
                    }
                }
                return sm1.call(this, convRef, msg, cb);
            };
            console.log('[fta_lite] hooked ConversationService.SendMessage ConversationRef');
            send({ event: 'hooked', hook: 'ConversationService.SendMessage(ConversationRef)', overloads: 1 });
        } catch (eSMRef) {
            send({ event: 'error', stage: 'hook_sendmessage_ref', message: String(eSMRef) });
        }
        console.log('[fta_lite] hooked ConversationService.SendMessage');
        send({ event: 'hooked', hook: 'ConversationService.SendMessage(Conversation)', overloads: 1 });
    } catch (eSM) {
        send({ event: 'error', stage: 'hook_sendmessage', message: String(eSM) });
    }

    send({ event: 'ready' });
    console.log('[fta_lite] ready');
});
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FTA capture · lite (hook 零 I/O + 异步落库)")
    parser.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    parser.add_argument("--adb-executable", default=adb_binary(), help="adb 可执行文件名或绝对路径")
    parser.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--fta-remote-id", default=DEFAULT_FTA_REMOTE_ID)
    parser.add_argument("--voice-lib-root", default=DEFAULT_VOICE_LIB_ROOT)
    parser.add_argument("--image-lib-root", default=DEFAULT_IMAGE_LIB_ROOT, help="F3.5 image adb cp 目标目录")
    parser.add_argument("--video-lib-root", default=DEFAULT_VIDEO_LIB_ROOT, help="F3.5 video adb cp 目标目录")
    parser.add_argument(
        "--upstream-window-seconds",
        type=int,
        default=15,
        help="F3.5 image/video/oa 上游 hook 参数与 SendMessage 的关联窗口秒数",
    )
    parser.add_argument(
        "--note-window-seconds",
        type=int,
        default=30,
        help="F1.4 起已废弃：备注命名改由 GUI 双击。参数保留仅为兼容外部调用者。",
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="账号 id（F2.2）；未显式指定 --index-path 时，据此派生 voice 索引路径。",
    )
    parser.add_argument(
        "--index-path",
        default="",
        help="voice 索引路径；留空时按 --account-id 派生（fallback 到 runtime/voice_lib_index.json）。",
    )
    parser.add_argument("--timeout-seconds", type=float, default=0.0, help="0 表示常驻直到 Ctrl-C")
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=15.0,
        help="心跳周期（0 关闭）；失败即触发 reconnect（F3.3）",
    )
    parser.add_argument(
        "--reconnect-retries",
        type=int,
        default=3,
        help="心跳失败后 frida_session.reconnect 尝试次数（F3.3）",
    )
    parser.add_argument(
        "--emit-jsonl",
        action="store_true",
        default=True,
        help="事件走 stdout jsonl（默认开启）",
    )
    parser.add_argument(
        "--no-emit-jsonl",
        dest="emit_jsonl",
        action="store_false",
        help="关闭 jsonl，回退到人类可读 print",
    )
    parser.add_argument(
        "--dry-copy",
        action="store_true",
        help="只 send 事件不实际 cp（对比 hook 纯开销）",
    )
    parser.add_argument(
        "--metrics-out",
        default="",
        help="退出时把 hook 耗时统计落到指定 json 文件",
    )
    parser.add_argument(
        "--diag-log-path",
        default="runtime/artifacts/fta_lite_last.jsonl",
        help=(
            "会话级诊断日志（覆盖写）；包含 upstream_seen / dispatch_miss / captured "
            "等所有事件，供离线复盘。留空则不写。"
        ),
    )
    return parser.parse_args()


def find_pid(adb: str, adb_serial: str, process_name: str) -> int | None:
    proc = subprocess.run(
        [adb, "-s", adb_serial, "shell", "ps -A -o PID,NAME"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        print(
            f"[fta_lite] find_pid failed serial={adb_serial}: {err or proc.returncode}",
            file=sys.stderr,
            flush=True,
        )
        return None
    for line in (proc.stdout or "").splitlines():
        p = line.split()
        if len(p) >= 2 and p[1] == process_name:
            return int(p[0])
    return None


def build_js(fta_remote_id: str, upstream_window_ms: int = UPSTREAM_WINDOW_MS_DEFAULT) -> str:
    return (
        JS.replace("__FTA_REMOTE_ID__", fta_remote_id)
        .replace("__MSG_PREVIEW_BYTES__", str(ASSET_MSG_PREVIEW_BYTES))
        .replace("__UPSTREAM_WINDOW_MS__", str(int(upstream_window_ms)))
    )


def _fmt_ts(now_ms: int) -> str:
    d = datetime.fromtimestamp(now_ms / 1000.0)
    return d.strftime("%Y%m%d_%H%M%S")


def load_index(index_path: Path) -> list[dict[str, Any]]:
    raw = _SECURE_STORE.load_json(index_path, default=[])
    if not isinstance(raw, list):
        raise ValueError(f"voice index 不是数组: {index_path}")
    return raw


def save_index(index_path: Path, rows: list[dict[str, Any]]) -> None:
    _SECURE_STORE.save_json(index_path, rows, encrypt=True)


def add_index_entry(rows: list[dict[str, Any]], file: str, duration: int) -> dict[str, Any]:
    entry = {
        "id": str(uuid.uuid4()),
        "file": file.strip(),
        "duration": int(duration),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        # note/label 都留空，展示层用 "未命名 #NNN" 占位（决策 F9）
        "note": "",
        "label": "",
    }
    rows.append(entry)
    return entry


class _EventSink:
    def __init__(self, *, emit_jsonl: bool, diag_log_path: Path | None = None) -> None:
        self.emit_jsonl = emit_jsonl
        self._lock = threading.Lock()
        self._diag_fp = None
        if diag_log_path is not None:
            try:
                diag_log_path.parent.mkdir(parents=True, exist_ok=True)
                # 覆盖写：每次采集会话一份新日志，避免无限增长
                self._diag_fp = diag_log_path.open("w", encoding="utf-8")
            except OSError as exc:  # noqa: BLE001
                print(f"[fta_lite] diag log open failed: {exc}", file=sys.stderr, flush=True)

    def emit(self, event_type: str, **fields: Any) -> None:
        # F2.1：所有事件带 schema 版本；顺序 sv/t/ts/... 便于人眼扫读 jsonl
        payload = {"sv": EVENT_SCHEMA_VERSION, "t": event_type, "ts": time.time(), **fields}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._lock:
            # 先落诊断文件。窗口版 exe 没有控制台时 stdout/stderr 是 None，
            # 若先写它们，进程会在记到 mType 之前就崩。
            if self._diag_fp is not None:
                try:
                    self._diag_fp.write(line)
                    self._diag_fp.flush()
                except OSError:
                    pass
            try:
                if self.emit_jsonl:
                    out = sys.stdout
                    if out is not None:
                        out.write(line)
                        out.flush()
                else:
                    err = sys.stderr
                    if err is not None:
                        print(
                            f"[fta_lite] {event_type}",
                            json.dumps(fields, ensure_ascii=False),
                            file=err,
                            flush=True,
                        )
            except (OSError, AttributeError):
                pass

    def close(self) -> None:
        if self._diag_fp is not None:
            try:
                self._diag_fp.close()
            except OSError:
                pass
            self._diag_fp = None


def _device_cp_file(
    *,
    adb: str,
    adb_serial: str,
    source_path: str,
    dst_dir: str,
    filename: str,
) -> tuple[bool, str, int]:
    """在设备内 cp（走 adbd，与企微主线程完全解耦）。返回 (ok, dst_path, size_or_-1)。

    F1.2 决策 #2：任何素材文件复制走 `adb shell cp`，wework 崩了也不影响 cp；
    F3.5：image / video 也复用（各自 dst_dir 不同）。
    """
    dst_path = f"{dst_dir.rstrip('/')}/{filename}"
    mk = subprocess.run(
        [adb, "-s", adb_serial, "shell", f"mkdir -p '{dst_dir}'"],
        capture_output=True,
        text=True,
    )
    if mk.returncode != 0:
        return False, dst_path, -1
    cp = subprocess.run(
        [adb, "-s", adb_serial, "shell", f"cp '{source_path}' '{dst_path}'"],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        return False, dst_path, -1
    st = subprocess.run(
        [adb, "-s", adb_serial, "shell", f"stat -c %s '{dst_path}'"],
        capture_output=True,
        text=True,
    )
    try:
        size = int(st.stdout.strip())
    except (ValueError, TypeError):
        size = -1
    return True, dst_path, size


def _device_source_basename(path: str) -> str:
    """从 /a/b/c.png → c.png；容错空串。"""
    p = path.strip().rstrip("/")
    if not p:
        return ""
    idx = p.rfind("/")
    return p[idx + 1:] if idx >= 0 else p


def _decode_octal_escaped_path(path_text: str) -> str:
    """把 '\\347\\273\\203...' 形式的八进制转义路径还原为真实路径。"""
    text = str(path_text or "").strip()
    if not text or "\\" not in text:
        return text
    try:
        raw = codecs.escape_decode(text)[0]
    except Exception:
        return text
    for encoding in ("utf-8", "gb18030"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if decoded.strip():
            return decoded
    return text


def _extract_video_path_from_miss_payload(payload: dict[str, Any]) -> str:
    """从 dispatch_miss(video) 的 msgDump.mInfo 文本中兜底提取视频路径。"""
    if str(payload.get("kind", "")) != "video":
        return ""
    msg_dump = payload.get("msgDump", {})
    if not isinstance(msg_dump, dict):
        return ""
    fields = msg_dump.get("fields", {})
    if not isinstance(fields, dict):
        return ""
    m_info = str(fields.get("mInfo", "") or "")
    if not m_info:
        return ""
    patterns = (
        r"(/data/user/0/com\.tencent\.wework/files/[^\s\"']+?\.(?:mp4|mov|avi|mkv|m4v))",
        r"(/storage/emulated/0/Android/data/com\.tencent\.wework/files/[^\s\"']+?\.(?:mp4|mov|avi|mkv|m4v))",
        r"(/sdcard/Android/data/com\.tencent\.wework/files/[^\s\"']+?\.(?:mp4|mov|avi|mkv|m4v))",
    )
    for pattern in patterns:
        m = re.search(pattern, m_info, flags=re.IGNORECASE)
        if not m:
            continue
        return _decode_octal_escaped_path(m.group(1))
    return ""


def _pick_recent_video_cache_path(
    *, adb: str, adb_serial: str, ref_ts_ms: int | None = None
) -> str:
    """从企微缓存目录挑选与当前发送时刻最接近的视频文件路径。"""
    roots = [
        "/storage/emulated/0/Android/data/com.tencent.wework/files/filecache",
        "/storage/emulated/0/Android/data/com.tencent.wework/files/tempvideocache",
        "/sdcard/Android/data/com.tencent.wework/files/filecache",
        "/sdcard/Android/data/com.tencent.wework/files/tempvideocache",
    ]
    find_expr = " -o ".join([f"-path '{root}/*'" for root in roots])
    find_cmd = (
        "find /storage/emulated/0/Android/data/com.tencent.wework/files "
        "/sdcard/Android/data/com.tencent.wework/files "
        f"\\( {find_expr} \\) -type f "
        "\\( -name '*.mp4' -o -name '*.mov' -o -name '*.m4v' -o -name '*.avi' -o -name '*.mkv' \\) "
        "2>/dev/null"
    )
    proc = subprocess.run(
        [adb, "-s", adb_serial, "shell", find_cmd],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    candidates = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not candidates:
        return ""
    best_path = ""
    best_score = 10**18
    ref_ts_sec = int((ref_ts_ms or int(time.time() * 1000)) / 1000)
    for path in candidates[-80:]:
        st = subprocess.run(
            [adb, "-s", adb_serial, "shell", f"stat -c '%Y %s' '{path}'"],
            capture_output=True,
            text=True,
        )
        if st.returncode != 0:
            continue
        parts = (st.stdout or "").strip().split()
        if len(parts) < 2:
            continue
        try:
            mtime = int(parts[0])
            size = int(parts[1])
        except ValueError:
            continue
        if size < 100 * 1024:
            continue
        # 优先选择发送前后 90s 内的文件，避免误捞历史缓存。
        delta = abs(mtime - ref_ts_sec)
        if delta > 90:
            continue
        score = delta * 1000 + (0 if "/filecache/" in path else 200)
        if score < best_score:
            best_score = score
            best_path = path
    return best_path


def main() -> int:  # noqa: C901
    # 走 pipe 时 stdout 默认 block-buffered，改成行缓冲，配合 Tee-Object/日志尾读
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    args = parse_args()
    adb = args.adb_executable
    diag_path = Path(args.diag_log_path).resolve() if args.diag_log_path else None
    sink = _EventSink(emit_jsonl=args.emit_jsonl, diag_log_path=diag_path)

    from app.bootstrap.runtime_bootstrap import ensure_adb_serial, ensure_frida_forward

    configured_serial = str(args.adb_serial or "").strip()

    def _bind_live_serial() -> str:
        serial = ensure_adb_serial(adb, configured_serial)
        if not serial:
            return ""
        if serial != args.adb_serial:
            print(
                f"[fta_lite] adb serial {args.adb_serial} offline, using {serial}",
                file=sys.stderr,
                flush=True,
            )
        args.adb_serial = serial
        code, out = ensure_frida_forward(adb, serial)
        if code != 0:
            print(
                f"[fta_lite] adb forward failed serial={serial}: {out or code}",
                file=sys.stderr,
                flush=True,
            )
        return serial

    if not _bind_live_serial():
        sink.emit(
            "error",
            stage="adb_connect",
            message=f"device offline: {configured_serial}",
        )
        sink.close()
        return 2

    pid = find_pid(adb, args.adb_serial, args.process_name)
    if pid is None:
        # 列出设备之后、ps 之前传输可能又掉了，再连一次。
        _bind_live_serial()
        pid = find_pid(adb, args.adb_serial, args.process_name)
    if pid is None:
        sink.emit(
            "error",
            stage="find_pid",
            message=f"process not found: {args.process_name} serial={args.adb_serial}",
        )
        sink.close()
        return 2

    # F2.2：--index-path 显式优先；否则按 --account-id 派生（与 GUI 账号隔离对齐）
    if args.index_path:
        index_path = Path(args.index_path)
    else:
        index_path = account_voice_index_path(args.account_id or None)
    rows = load_index(index_path)
    sink.emit("index_loaded", path=str(index_path), entries=len(rows))

    executor = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="fta-lite")
    io_lock = threading.Lock()

    hook_elapsed_samples: list[int] = []
    samples_lock = threading.Lock()

    def _handle_voice_seen(payload: dict[str, Any]) -> None:
        source_path = str(payload.get("sourcePath", ""))
        voice_time = int(payload.get("voiceTime") or 0)
        ts_ms = int(payload.get("tsMs") or int(time.time() * 1000))
        filename = f"{_fmt_ts(ts_ms)}_{voice_time}s.silk"

        if args.dry_copy:
            ok, dst_path, size = True, f"{args.voice_lib_root}/{filename}", -1
        else:
            ok, dst_path, size = _device_cp_file(
                adb=adb,
                adb_serial=args.adb_serial,
                source_path=source_path,
                dst_dir=args.voice_lib_root,
                filename=filename,
            )

        if not ok:
            sink.emit("capture_failed", kind="voice", reason="device_copy_failed", sourcePath=source_path)
            return

        with io_lock:
            entry = add_index_entry(rows, file=filename, duration=voice_time)
            save_index(index_path, rows)
        sink.emit(
            "captured",
            kind="voice",
            id=entry["id"],
            file=filename,
            duration=voice_time,
            sourcePath=source_path,
            dstPath=dst_path,
            size=size,
            convLocal=payload.get("convLocal", ""),
            convRemote=payload.get("convRemote", ""),
            dryCopy=args.dry_copy,
        )

    def _handle_text_seen(payload: dict[str, Any]) -> None:
        from app.broadcast.library import VoiceLibrary

        body = str(payload.get("body") or "").strip()
        if not body:
            sink.emit("capture_failed", kind="text", reason="empty_body")
            return
        tid = str(uuid.uuid4())
        with io_lock:
            lib = VoiceLibrary.load(account_id=args.account_id or None)
            lib.add_text_template(
                name=tid,
                variants=[body],
                template_id=tid,
                kind="text",
                unnamed=True,
            )
            lib.save()
        sink.emit(
            "captured",
            kind="text",
            id=tid,
            preview=body,
            convLocal=payload.get("convLocal", ""),
            convRemote=payload.get("convRemote", ""),
            dryCopy=args.dry_copy,
        )

    # ---------------- F3.5 image / video / oa handlers ----------------
    # 三类落库共同点：
    #   1. dry_copy 只发事件不真跑 adb cp
    #   2. payload["unnamed"] = True → GUI 显示 `未命名 #NNN` 双击命名（F3.2 扩展）
    #   3. captured_from 保留 conv 追溯信息，方便未来数据分析
    #   4. save_template(pending=False) 立即落已提交目录（决策 q1 立即落库）
    def _handle_image_seen(payload: dict[str, Any]) -> None:
        from app.broadcast import image_templates as itpl

        source_path = str(payload.get("sourcePath", ""))
        msg_type = int(payload.get("mType") or 7)
        ts_ms = int(payload.get("tsMs") or int(time.time() * 1000))
        # 原始文件通常是 `..._compress.png`；但 mType=14 GUI 发图的主图是
        # imagecache/imagemsg2/<md5>.0（扩展名是数字，实为 JPEG）。非常规图片后缀
        # 一律归一化成 .jpg，避免素材库里出现 `.0` 这种无法识别的文件名。
        src_base = _device_source_basename(source_path) or "img.jpg"
        raw_ext = Path(src_base).suffix.lower()
        ext = raw_ext if raw_ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} else ".jpg"
        tid = str(uuid.uuid4())
        filename = f"{_fmt_ts(ts_ms)}_{tid[:8]}{ext}"

        if args.dry_copy or not source_path:
            ok = bool(source_path)
            dst_path = f"{args.image_lib_root}/{filename}"
            size = -1
        else:
            ok, dst_path, size = _device_cp_file(
                adb=adb,
                adb_serial=args.adb_serial,
                source_path=source_path,
                dst_dir=args.image_lib_root,
                filename=filename,
            )
        if not ok:
            sink.emit("capture_failed", kind="image", reason="device_copy_failed", sourcePath=source_path)
            return

        body = {
            "template_id": tid,
            "name": "",           # 显式空 → _prepare_payload 会 fallback template_id
            "tags": [],
            "unnamed": True,       # F3.2 flag：GUI 判定未命名 → `未命名 #NNN`
            "msgType": msg_type,
            "path": dst_path,     # 已 cp 到 image_lib，与企微缓存解耦
            "sourcePath": source_path,
            "size": size,
            "captured_from": {
                "convLocal": payload.get("convLocal", ""),
                "convRemote": payload.get("convRemote", ""),
                "event": "fta_image_seen",
            },
        }
        try:
            itpl.save_template(body, template_id=tid, account_id=args.account_id, pending=False)
        except Exception as exc:  # noqa: BLE001
            sink.emit("capture_failed", kind="image", reason=f"save_template_failed:{exc}", sourcePath=source_path)
            return
        sink.emit(
            "captured",
            kind="image",
            id=tid,
            file=filename,
            sourcePath=source_path,
            dstPath=dst_path,
            size=size,
            convLocal=payload.get("convLocal", ""),
            convRemote=payload.get("convRemote", ""),
            dryCopy=args.dry_copy,
        )

    def _handle_video_seen(payload: dict[str, Any]) -> None:
        from app.broadcast import video_templates as vtpl

        video_src = _decode_octal_escaped_path(str(payload.get("videoPath", "")))
        thumb_src = _decode_octal_escaped_path(str(payload.get("thumbPath", "")))
        ts_ms = int(payload.get("tsMs") or int(time.time() * 1000))
        tid = str(uuid.uuid4())
        video_base = _device_source_basename(video_src) or "v.mp4"
        video_ext = Path(video_base).suffix.lower() or ".mp4"
        thumb_base = _device_source_basename(thumb_src)
        thumb_ext = Path(thumb_base).suffix.lower() or ".wwdata"
        video_name = f"{_fmt_ts(ts_ms)}_{tid[:8]}{video_ext}"
        thumb_name = f"{_fmt_ts(ts_ms)}_{tid[:8]}{thumb_ext}"

        if args.dry_copy or not video_src:
            ok_v = bool(video_src)
            video_dst = f"{args.video_lib_root}/{video_name}"
            video_size = -1
        else:
            # 某些版本会给出 /data/user/0/com.tencent.wework/files/... 私有路径；
            # adb shell 可能不可读，回退到外部同构路径再尝试一次。
            src_candidates = [video_src]
            private_prefix = "/data/user/0/com.tencent.wework/files/"
            if video_src.startswith(private_prefix):
                mapped = video_src.replace(
                    private_prefix,
                    "/storage/emulated/0/Android/data/com.tencent.wework/files/",
                    1,
                )
                if mapped not in src_candidates:
                    src_candidates.append(mapped)
            ok_v = False
            video_dst = f"{args.video_lib_root}/{video_name}"
            video_size = -1
            used_src = video_src
            for candidate in src_candidates:
                ok_try, dst_try, size_try = _device_cp_file(
                    adb=adb,
                    adb_serial=args.adb_serial,
                    source_path=candidate,
                    dst_dir=args.video_lib_root,
                    filename=video_name,
                )
                if ok_try:
                    ok_v, video_dst, video_size, used_src = True, dst_try, size_try, candidate
                    break
            if not ok_v:
                fallback_src = _pick_recent_video_cache_path(
                    adb=adb,
                    adb_serial=args.adb_serial,
                    ref_ts_ms=ts_ms,
                )
                if fallback_src:
                    ok_try, dst_try, size_try = _device_cp_file(
                        adb=adb,
                        adb_serial=args.adb_serial,
                        source_path=fallback_src,
                        dst_dir=args.video_lib_root,
                        filename=video_name,
                    )
                    if ok_try:
                        ok_v, video_dst, video_size, used_src = True, dst_try, size_try, fallback_src
            video_src = used_src
        if not ok_v:
            sink.emit(
                "capture_failed",
                kind="video",
                reason="device_copy_failed_video",
                sourcePath=video_src,
                videoPath=video_src,
                thumbPath=thumb_src,
            )
            return

        thumb_dst = ""
        if thumb_src and not args.dry_copy:
            ok_t, thumb_dst_try, _ = _device_cp_file(
                adb=adb, adb_serial=args.adb_serial,
                source_path=thumb_src, dst_dir=args.video_lib_root, filename=thumb_name,
            )
            if ok_t:
                thumb_dst = thumb_dst_try
        elif thumb_src and args.dry_copy:
            thumb_dst = f"{args.video_lib_root}/{thumb_name}"

        body = {
            "template_id": tid,
            "name": "",
            "tags": [],
            "unnamed": True,
            "msgType": 23,
            "path": video_dst,
            "video_path": video_dst,
            "thumb_path": thumb_dst,
            "sourceVideoPath": video_src,
            "sourceThumbPath": thumb_src,
            "size": video_size,
            "captured_from": {
                "convLocal": payload.get("convLocal", ""),
                "convRemote": payload.get("convRemote", ""),
                "event": "fta_video_seen",
            },
        }
        try:
            vtpl.save_template(body, template_id=tid, account_id=args.account_id, pending=False)
        except Exception as exc:  # noqa: BLE001
            sink.emit(
                "capture_failed",
                kind="video",
                reason=f"save_template_failed:{exc}",
                sourcePath=video_src,
                videoPath=video_src,
                thumbPath=thumb_src,
            )
            return
        sink.emit(
            "captured",
            kind="video",
            id=tid,
            file=video_name,
            sourcePath=video_src,
            dstPath=video_dst,
            thumbPath=thumb_dst,
            size=video_size,
            convLocal=payload.get("convLocal", ""),
            convRemote=payload.get("convRemote", ""),
            dryCopy=args.dry_copy,
        )

    def _handle_wxa_seen(payload: dict[str, Any]) -> None:
        from app.broadcast import wxa_templates as wtpl
        from app.broadcast.wxa_templates import has_card_fields

        tid = str(uuid.uuid4())
        raw_type = payload.get("type")
        try:
            wxa_type = int(raw_type)
        except (TypeError, ValueError):
            wxa_type = 2
        body = {
            "template_id": tid,
            "name": "",
            "tags": [],
            "unnamed": True,
            "msgType": 78,
            "username": str(payload.get("username") or ""),
            "pagepath": str(payload.get("pagepath") or ""),
            "title": str(payload.get("title") or ""),
            "appName": str(payload.get("appName") or ""),
            "weappIconUrl": str(payload.get("weappIconUrl") or ""),
            "desc": str(payload.get("desc") or ""),
            "type": wxa_type,
            "weappBodyB64": str(payload.get("weappBodyB64") or ""),
            "captured_from": {
                "convLocal": payload.get("convLocal", ""),
                "convRemote": payload.get("convRemote", ""),
                "event": "fta_wxa_seen",
            },
        }
        if not has_card_fields(body):
            sink.emit("capture_failed", kind="wxa", reason="missing_card_fields")
            return
        try:
            wtpl.save_template(body, template_id=tid, account_id=args.account_id, pending=False)
        except Exception as exc:  # noqa: BLE001
            sink.emit("capture_failed", kind="wxa", reason=f"save_template_failed:{exc}")
            return
        sink.emit(
            "captured",
            kind="wxa",
            id=tid,
            title=body["title"],
            appName=body["appName"],
            username=body["username"],
            convLocal=payload.get("convLocal", ""),
            convRemote=payload.get("convRemote", ""),
            dryCopy=args.dry_copy,
        )

    def _handle_oa_seen(payload: dict[str, Any]) -> None:
        # oa 没有本地文件，只是结构化字段（link/title/desc/imgUrl）——直接 dump JSON。
        from app.broadcast import oa_templates as otpl

        tid = str(uuid.uuid4())
        from app.broadcast.link_cards import classify_link_card

        url = str(payload.get("link", ""))
        title = str(payload.get("title", ""))
        desc = str(payload.get("desc", ""))
        appname = str(payload.get("appname", ""))
        card_kind = classify_link_card(url=url, title=title, desc=desc, appname=appname)
        body = {
            "template_id": tid,
            "name": "",
            "tags": [],
            "unnamed": True,
            "msgType": 13,
            "appMsgType": 5,
            "card_kind": card_kind,
            "url": url,
            "title": title,
            "desc": desc,
            "sourcename": appname,
            "thumburl": str(payload.get("imgUrl", "")),
            "hasImgData": bool(payload.get("hasImgData", False)),
            "captured_from": {
                "convLocal": payload.get("convLocal", ""),
                "convRemote": payload.get("convRemote", ""),
                "event": "fta_oa_seen",
            },
        }
        try:
            otpl.save_template(body, template_id=tid, account_id=args.account_id, pending=False)
        except Exception as exc:  # noqa: BLE001
            sink.emit("capture_failed", kind=card_kind, reason=f"save_template_failed:{exc}")
            return
        sink.emit(
            "captured",
            kind=card_kind,
            id=tid,
            title=body["title"],
            url=body["url"],
            convLocal=payload.get("convLocal", ""),
            convRemote=payload.get("convRemote", ""),
            dryCopy=args.dry_copy,
        )

    def _handle_asset_observed(payload: dict[str, Any]) -> None:
        # F1.5 观察模式：只把 JS 侧的 mType/msgPreview 转发到 stdout jsonl，不落库。
        # F3.5 会基于这里收集到的样本决定各类型的 hook 与入库 schema。
        sink.emit(
            "asset_observed",
            mType=payload.get("mType", -1),
            convLocal=payload.get("convLocal", ""),
            convRemote=payload.get("convRemote", ""),
            msgPreview=payload.get("msgPreview", ""),
            tsMs=payload.get("tsMs", 0),
        )

    errored = {"flag": False}

    def on_message(msg: dict[str, Any], _data: Any) -> None:
        mtype = msg.get("type")
        # F2.2 fix：frida-python 默认把 type=='log' 消息打到 stdout（污染 jsonl 通道）。
        # 这里主动接管，重定向到 stderr。
        if mtype == "log":
            level = str(msg.get("level", "info"))
            text = str(msg.get("payload", ""))
            print(f"[fta_lite][js:{level}] {text}", file=sys.stderr, flush=True)
            return
        if mtype == "error":
            errored["flag"] = True
            sink.emit("script_error", stack=str(msg.get("stack", msg)))
            return
        if mtype != "send":
            return

        payload = msg.get("payload", {}) or {}
        event = payload.get("event")

        elapsed = payload.get("hookElapsedMs")
        if isinstance(elapsed, (int, float)):
            with samples_lock:
                hook_elapsed_samples.append(int(elapsed))

        if event == "ready":
            sink.emit("ready", attached_pid=pid)
            return
        if event == "fta_asset_seen":
            executor.submit(_handle_asset_observed, payload)
            return
        if event == "upstream_seen":
            sink.emit("upstream_seen", **{k: v for k, v in payload.items() if k != "event"})
            return
        if event == "dispatch_miss":
            if str(payload.get("kind", "")) == "video":
                fallback_path = _extract_video_path_from_miss_payload(payload)
                if fallback_path:
                    sink.emit(
                        "video_dispatch_fallback",
                        mType=payload.get("mType", -1),
                        sourcePath=fallback_path,
                        convLocal=payload.get("convLocal", ""),
                        convRemote=payload.get("convRemote", ""),
                        tsMs=payload.get("tsMs", 0),
                    )
                    executor.submit(
                        _handle_video_seen,
                        {
                            "videoPath": fallback_path,
                            "thumbPath": "",
                            "tsMs": payload.get("tsMs", 0),
                            "convLocal": payload.get("convLocal", ""),
                            "convRemote": payload.get("convRemote", ""),
                            "mType": payload.get("mType", -1),
                        },
                    )
            sink.emit("dispatch_miss", **{k: v for k, v in payload.items() if k != "event"})
            return
        if event == "fta_voice_seen":
            executor.submit(_handle_voice_seen, payload)
            return
        if event == "fta_text_seen":
            executor.submit(_handle_text_seen, payload)
            return
        if event == "fta_image_seen":
            executor.submit(_handle_image_seen, payload)
            return
        if event == "fta_video_seen":
            executor.submit(_handle_video_seen, payload)
            return
        if event == "fta_oa_seen":
            executor.submit(_handle_oa_seen, payload)
            return
        if event == "fta_wxa_seen":
            executor.submit(_handle_wxa_seen, payload)
            return
        if event == "error":
            errored["flag"] = True
            sink.emit("js_error", stage=payload.get("stage", ""), message=payload.get("message", ""))
            return

    from runtime.wework_re_android.frida_session import FridaSession

    js = build_js(
        args.fta_remote_id,
        upstream_window_ms=max(1000, int(args.upstream_window_seconds * 1000)),
    )

    # F2.2 fix：frida-python 默认在 C 层把 JS console.log 直接写到 stdout（污染 jsonl 通道）。
    # 正确覆盖方式是 set_log_handler，签名 handler(level, text)。老版本 fallback 到
    # on_message 里 type=='log' 分支。
    def _on_js_log(level: str, text: str) -> None:
        print(f"[fta_lite][js:{level}] {text}", file=sys.stderr, flush=True)

    # F3.3：走 FridaSession，方便主循环内做 heartbeat + reconnect。attach 的 pid 优先
    # 用外层已解析的 pid（避免 pid_provider 再跑一次 `adb shell ps`）。
    session_holder = FridaSession(
        frida_remote=args.frida_remote,
        process_name=args.process_name,
        pid_provider=lambda _s, _n: pid,
    )
    script_holder: dict[str, Any] = {"script": None, "session": None}

    def _install_script() -> None:
        """attach + create_script + on_message + load，可反复调用（重连时用）。"""
        sess = session_holder.attach(adb_serial=args.adb_serial)
        script = sess.create_script(js)
        script.on("message", on_message)
        if hasattr(script, "set_log_handler"):
            script.set_log_handler(_on_js_log)
        script.load()
        script_holder["script"] = script
        script_holder["session"] = sess

    def _safe_detach() -> None:
        """F3.4：atexit / finally 都会调；幂等。"""
        try:
            session_holder.detach()
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_safe_detach)

    # F3.4：SIGTERM → KeyboardInterrupt，让 finally 走完 detach + metrics
    def _sigterm_handler(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (ValueError, AttributeError, OSError):  # 非主线程 / Windows 子集
        pass

    _install_script()
    sink.emit("attached", pid=pid, dry_copy=args.dry_copy)

    deadline = time.time() + args.timeout_seconds if args.timeout_seconds > 0 else None
    exit_code = 0
    hb_interval = max(0.0, float(args.heartbeat_seconds))
    hb_next = time.time() + hb_interval if hb_interval > 0 else None
    try:
        while True:
            if errored["flag"]:
                exit_code = 1
                break
            if deadline is not None and time.time() >= deadline:
                sink.emit("stopped", reason="timeout")
                break
            # F3.3：心跳
            if hb_next is not None and time.time() >= hb_next:
                if not session_holder.heartbeat():
                    sink.emit(
                        "heartbeat_lost",
                        attempt=1,
                        message=session_holder.state.last_error,
                    )
                    ok = session_holder.reconnect(
                        adb_serial=args.adb_serial,
                        retries=max(1, int(args.reconnect_retries)),
                        retry_gap_seconds=5.0,
                        on_retry=lambda i, err: sink.emit(
                            "heartbeat_lost", attempt=i + 1, message=err
                        ),
                    )
                    if not ok:
                        sink.emit("stopped", reason="reconnect_failed")
                        exit_code = 1
                        break
                    try:
                        _install_script()
                        sink.emit("reconnected", attempt=1, pid=pid)
                    except Exception as exc:  # noqa: BLE001
                        sink.emit(
                            "stopped",
                            reason="reconnect_failed",
                            message=f"reinstall_script: {exc}",
                        )
                        exit_code = 1
                        break
                hb_next = time.time() + hb_interval
            time.sleep(0.2)
    except KeyboardInterrupt:
        sink.emit("stopped", reason="keyboard_interrupt")
    finally:
        try:
            session_holder.detach()
        except Exception as exc:  # noqa: BLE001
            sink.emit("detach_error", message=str(exc))
        executor.shutdown(wait=True, cancel_futures=False)

        with samples_lock:
            samples = list(hook_elapsed_samples)
        if samples:
            samples.sort()

            def _pct(p: float) -> int:
                idx = min(len(samples) - 1, int(round(p * (len(samples) - 1))))
                return samples[idx]

            metrics = {
                "count": len(samples),
                "p50_ms": _pct(0.5),
                "p95_ms": _pct(0.95),
                "p99_ms": _pct(0.99),
                "max_ms": samples[-1],
                "min_ms": samples[0],
                "avg_ms": sum(samples) / len(samples),
            }
            sink.emit("metrics", **metrics)
            if args.metrics_out:
                Path(args.metrics_out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.metrics_out).write_text(
                    json.dumps({**metrics, "samples": samples}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        sink.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
