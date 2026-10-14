"""
Step 3/9: 统一发送引擎 (voice / text / image / oa / video / wxa / at / notice)。

CLI:
  python -m runtime.wework_re_android.send_engine voice --conv-local ... --silk-path ... --voice-time 3
  python -m runtime.wework_re_android.send_engine text  --conv-local ... --body "你好"
  python -m runtime.wework_re_android.send_engine at    --conv-local ... --body "你好"
  python -m runtime.wework_re_android.send_engine notice --conv-local ... --body "公告正文"
  python -m runtime.wework_re_android.send_engine image --conv-local ... --image-path /sdcard/...
  python -m runtime.wework_re_android.send_engine oa    --conv-local ... --template <id-or-json-path>
  python -m runtime.wework_re_android.send_engine wxa   --conv-local ... --template <id-or-json-path>
  python -m runtime.wework_re_android.send_engine video --conv-local ... --video-path /sdcard/a.mp4 [--thumb-path /sdcard/a.jpg] [--duration 8]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from app.bootstrap.win_subprocess import install_global_no_window_patch
from app.broadcast.account import resolve_account_id
from app.broadcast.oa_templates import load_template
from runtime.wework_re_android.frida_session import FridaSession

# 每条消息都会新开一个发送进程，里面多次调用 adb.exe。
# 父界面的无窗口补丁不会带进子进程，这里一导入就装上，避免执行时黑框频闪。
install_global_no_window_patch()


DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_ACTIVITY_GUARD = ""
DEFAULT_WAIT_SECONDS = 4.0
DEFAULT_ADB_BINARY_ENV = "TESTVOICE_ADB_BINARY"
DEFAULT_WEWORK_TEMPIMAGE_DIR = "/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/testvoice"
DEFAULT_WEWORK_TEMPVIDEO_DIR = "/storage/emulated/0/Android/data/com.tencent.wework/files/tempvideocache/testvoice"
# 企微真人录音的 silk 落地根目录，下面按账号分子目录（如 1688855042791155）。
# 主动发送必须把 silk 放进当前账号的这个目录，E8 才能走通 CDN 上传；否则
# 从 /sdcard 等外部路径发只会插入本地气泡、上传失败 → 对方看到红色感叹号。
DEFAULT_WEWORK_VOICEMSG_DIR = "/storage/emulated/0/Android/data/com.tencent.wework/files/voicemsg"


def _escape_js_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def build_js(
    *,
    action: Literal["voice", "text", "image", "oa", "video", "wxa", "at", "notice"],
    conv_local: str,
    silk_path: str = "",
    voice_time: int = 0,
    body: str = "",
    image_path: str = "",
    video_path: str = "",
    thumb_path: str = "",
    duration: int = 0,
    oa_template_json: str = "",
    wxa_template_json: str = "",
    activity_guard: str = "",
) -> str:
    return (
        r"""
'use strict';
Java.perform(function () {
    try {
        var h8y = Java.use('h8y');
        var ActivityThread = Java.use('android.app.ActivityThread');
        var FileCls = Java.use('java.io.File');
        var FileInputStream = Java.use('java.io.FileInputStream');
        var FileOutputStream = Java.use('java.io.FileOutputStream');
        var MapCls = Java.use('java.util.Map');
        var JString = Java.use('java.lang.String');
        var engineNoticeCb = null;
        var engineAtCb = null;

        function callbackSingleton(className, factory) {
            try {
                return factory().$new();
            } catch (eReg) {
                try { return Java.use(className).$new(); } catch (eUse) { return null; }
            }
        }
        var Base64 = Java.use('android.util.Base64');
        var BitmapFactory = Java.use('android.graphics.BitmapFactory');
        var BitmapOptions = Java.use('android.graphics.BitmapFactory$Options');
        var BitmapCompressFormat = Java.use('android.graphics.Bitmap$CompressFormat');
        var MediaMetadataRetriever = Java.use('android.media.MediaMetadataRetriever');

        var convRaw = '__CONV_LOCAL__';
        var convId = null;
        if (convRaw.indexOf('friend:') !== 0) {
            convId = new Int64(convRaw);
        }
        var action = '__ACTION__';

        function getForegroundActivity() {
            var at = ActivityThread.currentActivityThread();
            var atCls = at.getClass();
            var mActivitiesField = atCls.getDeclaredField('mActivities');
            mActivitiesField.setAccessible(true);
            var acts = mActivitiesField.get(at);
            var actsMap = Java.cast(acts, MapCls);
            var values = actsMap.values().toArray();
            var fallback = null;
            for (var i = 0; i < values.length; i++) {
                var rec = values[i];
                var recCls = rec.getClass();
                var pausedField = recCls.getDeclaredField('paused');
                pausedField.setAccessible(true);
                var paused = pausedField.getBoolean(rec);
                var stopped = false;
                try {
                    var stoppedField = recCls.getDeclaredField('stopped');
                    stoppedField.setAccessible(true);
                    stopped = stoppedField.getBoolean(rec);
                } catch (_) {}
                var actField = recCls.getDeclaredField('activity');
                actField.setAccessible(true);
                var act = actField.get(rec);
                if (!act) continue;
                if (!fallback || String(act.getClass().getName()).indexOf('WwMainActivity') >= 0) {
                    fallback = act;
                }
                if (!paused && !stopped) return act;
            }
            return fallback;
        }

        function validateFile(path, label) {
            var f = FileCls.$new(path);
            var exists = f.exists();
            var len = exists ? f.length() : -1;
            console.log('[send_engine] ' + label + ' exists=' + exists + ' len=' + len + ' path=' + path);
            return exists && len > 0;
        }

        function copyFile(srcPath, dstPath) {
            var fis = null;
            var fos = null;
            try {
                fis = FileInputStream.$new(srcPath);
                fos = FileOutputStream.$new(dstPath);
                var buf = Java.array('byte', Array(8192).fill(0));
                while (true) {
                    var n = fis.read(buf);
                    if (n <= 0) break;
                    fos.write(buf, 0, n);
                }
                fos.flush();
                return true;
            } catch (eCopy) {
                console.log('[send_engine] copyFile failed: ' + eCopy);
                return false;
            } finally {
                try { if (fis) fis.close(); } catch (_) {}
                try { if (fos) fos.close(); } catch (_) {}
            }
        }

        function stageImageForWeWork(srcPath) {
            var root = '/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/test_voice';
            var dir = FileCls.$new(root);
            if (!dir.exists()) {
                dir.mkdirs();
            }
            var ts = String((new Date()).getTime());
            var dstPath = root + '/' + ts + '_compress.png';
            try {
                var bmp = BitmapFactory.decodeFile(srcPath);
                if (bmp) {
                    var fos = FileOutputStream.$new(dstPath);
                    bmp.compress(BitmapCompressFormat.JPEG.value, 90, fos);
                    fos.flush();
                    fos.close();
                    bmp.recycle();
                    console.log('[send_engine] staged by bitmap compress: ' + dstPath);
                    return dstPath;
                }
            } catch (eBmp) {
                console.log('[send_engine] bitmap stage failed: ' + eBmp);
            }
            if (copyFile(srcPath, dstPath)) {
                console.log('[send_engine] staged by raw copy: ' + dstPath);
                return dstPath;
            }
            return srcPath;
        }

        function ensureParentDir(path) {
            try {
                var idx = path.lastIndexOf('/');
                if (idx <= 0) return;
                var dir = FileCls.$new(path.substring(0, idx));
                if (!dir.exists()) dir.mkdirs();
            } catch (_) {}
        }

        function buildThumbFromVideo(videoPath) {
            var ts = String((new Date()).getTime());
            var outPath = '/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/test_voice/' + ts + '_video_thumb.jpg';
            try {
                ensureParentDir(outPath);
                var mmr = MediaMetadataRetriever.$new();
                mmr.setDataSource(videoPath);
                var bmp = mmr.getFrameAtTime();
                if (!bmp) {
                    mmr.release();
                    console.log('[send_engine] buildThumbFromVideo no frame');
                    return '';
                }
                var fos = FileOutputStream.$new(outPath);
                bmp.compress(BitmapCompressFormat.JPEG.value, 85, fos);
                fos.flush();
                fos.close();
                bmp.recycle();
                mmr.release();
                if (validateFile(outPath, 'video_thumb_auto')) {
                    console.log('[send_engine] auto-generated video thumb: ' + outPath);
                    return outPath;
                }
            } catch (eThumb) {
                console.log('[send_engine] buildThumbFromVideo failed: ' + eThumb);
            }
            return '';
        }

        function parseConvLocal(convStr) {
            var m = /Local:\s*(\d+)/.exec(convStr || '');
            return m ? m[1] : '';
        }

        function findConversationByLocal(convLocal) {
            var CS = Java.use('com.tencent.wework.foundation.logic.ConversationService');
            var service = CS.getService();
            var list = service.GetConversationList();
            if (!list) return null;
            for (var i = 0; i < list.length; i++) {
                var c = list[i];
                if (!c) continue;
                var cur = parseConvLocal(String(c));
                if (String(cur) === String(convLocal)) {
                    return c;
                }
            }
            return null;
        }

        function atNoticeSetInt(obj, name, n) {
            var f = obj.getClass().getDeclaredField(name);
            f.setAccessible(true);
            f.setInt(obj, n);
        }

        function atNoticeSetObj(obj, name, value) {
            obj[name].value = value;
        }

        function atNoticeTextPart(body) {
            var Text = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessage$TextMessage');
            var Part = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$Message');
            var Nano = Java.use('com.google.protobuf.nano.MessageNano');
            var text = Text.$new();
            atNoticeSetObj(text, 'content', JString.$new(body).getBytes('UTF-8'));
            var part = Part.$new();
            atNoticeSetInt(part, 'contentType', 0);
            atNoticeSetObj(part, 'data', Nano.toByteArray(text));
            return part;
        }

        function atNoticeAtAllPart() {
            var At = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessage$AtMessage');
            var Part = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$Message');
            var Nano = Java.use('com.google.protobuf.nano.MessageNano');
            var at = At.$new();
            atNoticeSetObj(at, 'name', JString.$new('\u6240\u6709\u4eba ').getBytes('UTF-8'));
            var part = Part.$new();
            atNoticeSetInt(part, 'contentType', 5);
            atNoticeSetObj(part, 'data', Nano.toByteArray(at));
            return part;
        }

        function atNoticeRich(parts) {
            var Rich = Java.use('com.tencent.wework.foundation.model.pb.WwRichmessageBase$RichMessage');
            var rich = Rich.$new();
            atNoticeSetObj(
                rich,
                'messages',
                Java.array('com.tencent.wework.foundation.model.pb.WwRichmessageBase$Message', parts)
            );
            return rich;
        }

        function sendAtOrNotice(which, convLocal, body) {
            var conv = findConversationByLocal(convLocal);
            if (!conv) {
                console.log('[send_engine] action=' + which + ' error=conversation not found');
                console.log('[send_engine] action=' + which + ' return=false');
                return;
            }
            var typeMatch = /type:\s*(\d+)/.exec(String(conv));
            var convType = typeMatch ? parseInt(typeMatch[1], 10) : -1;
            if (convType !== 1) {
                console.log('[send_engine] action=' + which + ' error=not a group type=' + convType);
                console.log('[send_engine] action=' + which + ' return=false');
                return;
            }
            var textBody = String(body || '');
            if (!textBody.trim()) {
                console.log('[send_engine] action=' + which + ' error=empty body');
                console.log('[send_engine] action=' + which + ' return=false');
                return;
            }
            var CS = Java.use('com.tencent.wework.foundation.logic.ConversationService');
            var svc = CS.getService();
            if (which === 'notice') {
                if (!engineNoticeCb) {
                    engineNoticeCb = callbackSingleton('com.testvoice.EngineNoticeCb', function () {
                        return Java.registerClass({
                            name: 'com.testvoice.EngineNoticeCb',
                            implements: [Java.use('com.tencent.wework.foundation.callback.IConversationOperateCallback2')],
                            methods: {
                                onResult: function (code, text, convBack) {
                                    var ok = parseInt(String(code), 10) === 0;
                                    console.log('[send_engine] action=notice code=' + code + ' text=' + text);
                                    console.log('[send_engine] action=notice return=' + ok);
                                }
                            }
                        });
                    });
                }
                if (!engineNoticeCb) {
                    console.log('[send_engine] action=notice error=callback unavailable');
                    console.log('[send_engine] action=notice return=false');
                    return;
                }
                svc.SetGroupNotification(conv, atNoticeRich([atNoticeTextPart(textBody)]), true, engineNoticeCb);
                console.log('[send_engine] action=notice dispatched');
                return;
            }
            if (!engineAtCb) {
                engineAtCb = callbackSingleton('com.testvoice.EngineAtCb', function () {
                    return Java.registerClass({
                        name: 'com.testvoice.EngineAtCb',
                        implements: [Java.use('com.tencent.wework.foundation.callback.ISendMessageCallback')],
                        methods: {
                            onProgress: function () {},
                            onResult: function (code, convBack, message) {
                                var ok = parseInt(String(code), 10) === 0;
                                console.log('[send_engine] action=at code=' + code);
                                console.log('[send_engine] action=at return=' + ok);
                            }
                        }
                    });
                });
            }
            if (!engineAtCb) {
                console.log('[send_engine] action=at error=callback unavailable');
                console.log('[send_engine] action=at return=false');
                return;
            }
            var sendCb = engineAtCb;
            var Nano = Java.use('com.google.protobuf.nano.MessageNano');
            var WwMsg = Java.use('com.tencent.wework.foundation.model.pb.WwMessage$Message');
            var Message = Java.use('com.tencent.wework.foundation.model.Message');
            var info = WwMsg.$new();
            atNoticeSetInt(info, 'contentType', 0);
            atNoticeSetObj(info, 'content', Nano.toByteArray(atNoticeRich([atNoticeAtAllPart(), atNoticeTextPart(textBody)])));
            var msg = Message.NewMessage();
            msg.setInfo(info);
            svc.SendMessage.overload(
                'com.tencent.wework.foundation.model.Conversation',
                'com.tencent.wework.foundation.model.Message',
                'com.tencent.wework.foundation.callback.ISendMessageCallback'
            ).call(svc, conv, msg, sendCb);
            console.log('[send_engine] action=at dispatched');
        }

        function firstNonEmpty(arr, fallback) {
            for (var i = 0; i < arr.length; i++) {
                var v = arr[i];
                if (v !== null && v !== undefined) {
                    var s = String(v).trim();
                    if (s.length > 0) return s;
                }
            }
            return fallback;
        }

        function decodeUtf8Field(row) {
            if (!row || typeof row !== 'object') return '';
            var text = firstNonEmpty([row.utf8, row.value], '');
            if (text) return text;
            var b64 = firstNonEmpty([row.b64], '');
            if (!b64) return '';
            try {
                var bytes = Base64.decode(JString.$new(b64), 0);
                return String(JString.$new(bytes, 'UTF-8'));
            } catch (_) {
                return '';
            }
        }

        function buildLinkMessageFromTemplate(rawJson) {
            var tpl = {};
            try {
                tpl = JSON.parse(rawJson);
            } catch (eParse) {
                console.log('[send_engine] invalid oa template json: ' + eParse);
                return null;
            }
            var snap = tpl.linkMessageSnapshot || {};
            var title = firstNonEmpty([tpl.title, decodeUtf8Field(snap.title)], '');
            var desc = firstNonEmpty([tpl.desc, tpl.digest, decodeUtf8Field(snap.description)], '');
            var linkUrl = firstNonEmpty([tpl.url, decodeUtf8Field(snap.linkUrl)], '');
            var imageUrl = firstNonEmpty([tpl.thumburl, decodeUtf8Field(snap.imageUrl)], '');
            if (!linkUrl) {
                console.log('[send_engine] oa template missing linkUrl');
                return null;
            }

            // Step 3: 下载封面图字节，传入 h8y.f2 第 5 参
            var imgBytes = Java.array('byte', []);
            if (imageUrl && (imageUrl.indexOf('http://') === 0 || imageUrl.indexOf('https://') === 0)) {
                try {
                    var URL = Java.use('java.net.URL');
                    var ByteArrayOutputStream = Java.use('java.io.ByteArrayOutputStream');
                    var conn = URL.$new(imageUrl).openConnection();
                    conn.setConnectTimeout(5000);
                    conn.setReadTimeout(5000);
                    conn.connect();
                    var is = conn.getInputStream();
                    var baos = ByteArrayOutputStream.$new();
                    var buf = Java.array('byte', Array(4096).fill(0));
                    while (true) {
                        var n = is.read(buf);
                        if (n <= 0) break;
                        baos.write(buf, 0, n);
                    }
                    is.close();
                    imgBytes = baos.toByteArray();
                    console.log('[send_engine] oa thumb downloaded, size=' + imgBytes.length);
                } catch (eDl) {
                    console.log('[send_engine] oa thumb download failed: ' + eDl);
                    imgBytes = Java.array('byte', []);
                }
            }

            var linkMsg = h8y.f2(linkUrl, title, desc, imageUrl, imgBytes);
            if (!linkMsg) {
                console.log('[send_engine] h8y.f2 returned null');
                return null;
            }
            var msgType = parseInt(String(tpl.msgType || 13), 10) || 13;
            var message = h8y.j2(msgType, linkMsg);
            if (!message) {
                console.log('[send_engine] h8y.j2 returned null');
                return null;
            }
            return { message: message, msgType: msgType };
        }

        function javaBytesFromB64(b64) {
            return Base64.decode(JString.$new(String(b64)), 0);
        }

        function varintList(n) {
            var out = [];
            var v = n;
            if (!(v >= 0)) v = 0;
            while (v > 127) {
                out.push((v & 127) | 128);
                v = Math.floor(v / 128);
            }
            out.push(v & 127);
            return out;
        }

        function signedByte(n) {
            var v = n & 255;
            return v > 127 ? v - 256 : v;
        }

        function listToJavaBytes(list) {
            var out = Java.array('byte', Array(list.length).fill(0));
            for (var i = 0; i < list.length; i++) out[i] = signedByte(list[i]);
            return out;
        }

        function concatJavaBytes(parts) {
            var System = Java.use('java.lang.System');
            var total = 0;
            for (var i = 0; i < parts.length; i++) total += parts[i].length;
            var out = Java.array('byte', Array(total).fill(0));
            var off = 0;
            for (var i = 0; i < parts.length; i++) {
                System.arraycopy(parts[i], 0, out, off, parts[i].length);
                off += parts[i].length;
            }
            return out;
        }

        function protoKey(fieldNum, wireType) {
            return varintList((fieldNum << 3) | wireType);
        }

        function wrapLenField(fieldNum, payload) {
            var head = protoKey(fieldNum, 2).concat(varintList(payload.length));
            return concatJavaBytes([listToJavaBytes(head), payload]);
        }

        function setDeclared(obj, name, jsValue) {
            if (!obj || jsValue === null || jsValue === undefined || jsValue === '') return false;
            try {
                var f = obj.getClass().getDeclaredField(name);
                f.setAccessible(true);
                var t = String(f.getType().getName());
                if (t === 'java.lang.String') {
                    f.set(obj, JString.$new(String(jsValue)));
                } else if (t === '[B') {
                    f.set(obj, JString.$new(String(jsValue)).getBytes('UTF-8'));
                } else if (t === 'int') {
                    f.setInt(obj, parseInt(jsValue, 10));
                } else if (t === 'java.lang.Integer') {
                    f.set(obj, Java.use('java.lang.Integer').valueOf(parseInt(jsValue, 10)));
                } else {
                    return false;
                }
                return true;
            } catch (_) {
                return false;
            }
        }

        function buildWxaMessage(rawJson) {
            var tpl = {};
            try {
                tpl = JSON.parse(rawJson);
            } catch (eParse) {
                console.log('[send_engine] invalid wxa template json: ' + eParse);
                return null;
            }
            var WeApp = Java.use('com.tencent.wework.foundation.model.pb.WeAppMessageProto$WeAppMessage');
            var app = null;
            if (tpl.weappBodyB64) {
                try {
                    var body = javaBytesFromB64(String(tpl.weappBodyB64));
                    app = WeApp.parseFrom.overload('[B').call(WeApp, body);
                    console.log('[send_engine] wxa parseFrom body=' + body.length);
                } catch (eParseFrom) {
                    console.log('[send_engine] wxa parseFrom failed: ' + eParseFrom);
                    app = null;
                }
            }
            if (!app) {
                try {
                    app = WeApp.$new();
                } catch (eNew) {
                    console.log('[send_engine] wxa WeAppMessage.$new failed: ' + eNew);
                    return null;
                }
            }
            var names = ['username', 'pagepath', 'title', 'appName', 'weappIconUrl', 'desc'];
            for (var ni = 0; ni < names.length; ni++) {
                if (tpl[names[ni]]) setDeclared(app, names[ni], tpl[names[ni]]);
            }
            if (tpl.type !== undefined && tpl.type !== null && tpl.type !== '') {
                setDeclared(app, 'type', tpl.type);
            }
            var MN = Java.use('com.google.protobuf.nano.MessageNano');
            var toBytes = MN.toByteArray.overload('com.google.protobuf.nano.MessageNano');
            var appBytes = toBytes.call(MN, app);
            console.log('[send_engine] wxa appBytes=' + appBytes.length);
            if (!appBytes || appBytes.length < 8) {
                console.log('[send_engine] wxa app bytes too short');
                return null;
            }
            var wrapped = wrapLenField(107, appBytes);
            var contentTypeKey = listToJavaBytes(protoKey(7, 0).concat(varintList(78)));
            var outer = concatJavaBytes([contentTypeKey, wrapLenField(10, wrapped)]);
            var Info = Java.use('com.tencent.wework.foundation.model.pb.WwMessage$Message');
            var info = null;
            try {
                info = Info.parseFrom.overload('[B').call(Info, outer);
            } catch (eInfo) {
                console.log('[send_engine] wxa message parseFrom failed: ' + eInfo);
                return null;
            }
            var back = toBytes.call(MN, info);
            var ctype = '?';
            try {
                ctype = String(info.contentType.value);
            } catch (_) {
                try { ctype = String(info.contentType); } catch (_2) {}
            }
            console.log('[send_engine] wxa wire=' + outer.length + ' roundtrip=' + back.length + ' contentType=' + ctype);
            if (back.length < Math.floor(outer.length / 2)) {
                try {
                    info = Info.$new();
                    setDeclared(info, 'contentType', 78);
                    var contentField = info.getClass().getDeclaredField('content');
                    contentField.setAccessible(true);
                    contentField.set(info, wrapped);
                    back = toBytes.call(MN, info);
                    console.log('[send_engine] wxa content-field roundtrip=' + back.length);
                } catch (eContent) {
                    console.log('[send_engine] wxa content-field fallback failed: ' + eContent);
                }
            }
            var Msg = Java.use('com.tencent.wework.foundation.model.Message');
            var message = null;
            var newErr = '';
            try {
                message = Msg.NewMessage.overload('com.tencent.wework.foundation.model.pb.WwMessage$Message').call(Msg, info);
            } catch (eNmInfo) {
                newErr = String(eNmInfo);
            }
            if (!message) {
                try {
                    message = Msg.NewMessage();
                } catch (eNm) {
                    newErr = newErr + ' | ' + eNm;
                }
            }
            if (!message) {
                try {
                    message = Msg.NewMessage.overload().call(Msg);
                } catch (eNm2) {
                    newErr = newErr + ' | ' + eNm2;
                }
            }
            if (!message) {
                console.log('[send_engine] wxa NewMessage failed: ' + newErr);
                return null;
            }
            try {
                if (message.getInfo) {
                    var cur = message.getInfo();
                    var same = cur && String(cur) === String(info);
                    if (!same) {
                        message.setInfo.overload('com.tencent.wework.foundation.model.pb.WwMessage$Message').call(message, info);
                    }
                } else {
                    message.setInfo.overload('com.tencent.wework.foundation.model.pb.WwMessage$Message').call(message, info);
                }
            } catch (eSetInfo) {
                console.log('[send_engine] wxa setInfo failed: ' + eSetInfo);
                return null;
            }
            return message;
        }

        function setFieldIfPresent(obj, fieldName, value) {
            if (!obj || value === null || value === undefined) return false;
            try {
                var cls = obj.getClass();
                var f = cls.getDeclaredField(fieldName);
                f.setAccessible(true);
                f.set(obj, value);
                return true;
            } catch (_) {
                return false;
            }
        }

        function extractVideoMeta(path) {
            var out = { width: 0, height: 0, durationSec: 0 };
            try {
                var mmr = MediaMetadataRetriever.$new();
                mmr.setDataSource(path);
                var w = parseInt(String(mmr.extractMetadata(18) || '0'), 10) || 0;
                var h = parseInt(String(mmr.extractMetadata(19) || '0'), 10) || 0;
                var dms = parseInt(String(mmr.extractMetadata(9) || '0'), 10) || 0;
                out.width = w > 0 ? w : 0;
                out.height = h > 0 ? h : 0;
                out.durationSec = dms > 0 ? Math.max(1, Math.round(dms / 1000.0)) : 0;
                mmr.release();
            } catch (eMeta) {
                console.log('[send_engine] extractVideoMeta failed: ' + eMeta);
            }
            return out;
        }

        var activity = null;
        try {
            activity = getForegroundActivity();
        } catch (eAct) {
            console.log('[send_engine] getForegroundActivity failed: ' + eAct);
        }
        var actName = activity ? activity.getClass().getName() : 'null';
        console.log('[send_engine] activity=' + actName);
        if (!activity) {
            console.log('[send_engine] abort: no foreground activity');
            return;
        }
        var guard = '__ACTIVITY_GUARD__';
        if (guard && guard.length > 0 && actName.indexOf(guard) < 0) {
            console.log('[send_engine] abort: activity does not match guard=' + guard);
            return;
        }

        function findCachedUser(remoteId) {
            var contacts = Java.use('com.tencent.wework.foundation.logic.ContactService').getService();
            var arr = contacts.GetCachedContactList();
            var n = arr ? arr.length : 0;
            for (var i = 0; i < n; i++) {
                var u = arr[i];
                if (!u) continue;
                try {
                    if (String(u.getRemoteId()) === String(remoteId)) return u;
                } catch (eUser) {}
            }
            return null;
        }

        function publishConversation(conv, api) {
            var svc = Java.use('com.tencent.wework.foundation.logic.ConversationService').getService();
            var info = conv.getInfo();
            var hidden = info.getClass().getDeclaredField('hidden');
            hidden.setAccessible(true);
            hidden.set(info, Java.use('java.lang.Boolean').valueOf(false));
            conv.setInfo(info);
            svc.SaveConversation(conv);
            if (api) {
                var convArr = Java.array('com.tencent.wework.foundation.model.Conversation', [conv]);
                api.updateConversationCache.overload(
                    '[Lcom.tencent.wework.foundation.model.Conversation;',
                    'boolean'
                ).call(api, convArr, true);
            }
            var idField = conv.getInfo().getClass().getDeclaredField('id');
            idField.setAccessible(true);
            return new Int64(String(idField.get(conv.getInfo())));
        }

        function createSingleConversation(user, api) {
            var holder = {id: null, err: ''};
            var CB = Java.registerClass({
                name: 'com.cursor.EnsureConv' + Date.now(),
                implements: [Java.use('com.tencent.wework.foundation.callback.ICommonConversationOperateCallback')],
                methods: {
                    onResult: function (code, conv, errMsg) {
                        if (!conv || code !== 0) {
                            holder.err = String(code) + ' ' + (errMsg ? String(errMsg) : '');
                            return;
                        }
                        try {
                            holder.id = publishConversation(conv, api);
                        } catch (ePub) {
                            holder.err = String(ePub);
                        }
                    }
                }
            });
            var svc = Java.use('com.tencent.wework.foundation.logic.ConversationService').getService();
            var users = Java.array('com.tencent.wework.foundation.model.User', [user]);
            svc.CreateConversation.overload(
                'java.lang.String',
                '[Lcom.tencent.wework.foundation.model.User;',
                'com.tencent.wework.foundation.model.Conversation',
                'com.tencent.wework.foundation.callback.ICommonConversationOperateCallback'
            ).call(svc, '', users, null, CB.$new());
            if (!holder.id) console.log('[send_engine] create conversation failed: ' + holder.err);
            return holder.id;
        }

        function resolveSendableConv(api, currentId) {
            if (api && currentId) {
                try {
                    var existing = api.getConversationItem.overload('long').call(api, currentId);
                    if (existing) return currentId;
                } catch (eItem) {
                    console.log('[send_engine] getConversationItem failed: ' + eItem);
                }
            }
            var remote = '';
            if (convRaw.indexOf('friend:') === 0) remote = convRaw.substring('friend:'.length);
            else if (convRaw.indexOf('788') === 0) remote = convRaw;
            if (!remote) {
                console.log('[send_engine] session item missing for conv=' + convRaw);
                return currentId;
            }
            var user = findCachedUser(remote);
            if (!user) {
                console.log('[send_engine] cached contact missing remote=' + remote);
                return null;
            }
            var created = createSingleConversation(user, api);
            if (created) console.log('[send_engine] conversation ready local=' + created);
            return created;
        }

        var convApi = null;
        Java.choose('com.tencent.wework.msg.temp.ConversationApiImpl', {
            onMatch: function (inst) { if (!convApi) convApi = inst; },
            onComplete: function () {
        Java.scheduleOnMainThread(function () {
            try {
                var ok = false;
                convId = resolveSendableConv(convApi, convId);
                if (!convId) {
                    console.log('[send_engine] abort: conversation not ready');
                    return;
                }
                if (action === 'voice') {
                    var silkPath = '__SILK_PATH__';
                    if (!validateFile(silkPath, 'silk')) return;
                    var voiceTime = __VOICE_TIME__;
                    var mm = h8y.X4();
                    // 历史验收路径（send_voice_poc_h8y）：E8 之前必须先调用 U1 构建/登记
                    // 语音 FileMessage。U1 会对 silk 做发送前预处理（尺寸/时长/缓存登记），
                    // 缺这一步 E8 仍会插入本地气泡但服务端上传失败 → 对方收到红色感叹号。
                    try {
                        var voiceFm = h8y.U1.overload(
                            'java.lang.String', 'int', 'int', 'int', 'boolean'
                        ).call(h8y, silkPath, 0, 0, voiceTime, false);
                        console.log('[send_engine] voice U1 built=' + (voiceFm ? 'yes' : 'no'));
                    } catch (eU1) {
                        console.log('[send_engine] voice U1 failed: ' + eU1);
                    }
                    var e8 = h8y.E8.overload('android.app.Activity', 'long', 'java.lang.String', 'int');
                    ok = e8.call(mm, activity, convId, silkPath, voiceTime);
                } else if (action === 'text') {
                    var body = JString.$new('__BODY__');
                    var b8 = h8y.B8.overload('android.content.Context', 'long', 'java.lang.CharSequence', 'boolean');
                    ok = b8.call(h8y, activity, convId, body, false);
                } else if (action === 'image') {
                    var imagePath = '__IMAGE_PATH__';
                    if (!validateFile(imagePath, 'image')) return;
                    if (imagePath.indexOf('/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/') !== 0) {
                        imagePath = stageImageForWeWork(imagePath);
                        if (!validateFile(imagePath, 'image_staged')) return;
                    }
                    var mm2 = h8y.X4();
                    var opts = BitmapOptions.$new();
                    opts.inJustDecodeBounds.value = true;
                    BitmapFactory.decodeFile(imagePath, opts);
                    var width = opts.outWidth.value;
                    var height = opts.outHeight.value;
                    if (width <= 0 || height <= 0) {
                        try {
                            var v4 = h8y.V4.overload('java.lang.String');
                            var v4opts = v4.call(h8y, imagePath);
                            if (v4opts) {
                                width = v4opts.outWidth.value;
                                height = v4opts.outHeight.value;
                                console.log('[send_engine] image bounds from V4 width=' + width + ' height=' + height);
                            }
                        } catch (eV4) {
                            console.log('[send_engine] image V4 bounds failed: ' + eV4);
                        }
                    }
                    if (width < 0) width = 0;
                    if (height < 0) height = 0;
                    console.log('[send_engine] image bounds width=' + width + ' height=' + height);

                    var v1 = h8y.V1.overload(
                        'java.lang.String',
                        'int',
                        'int',
                        'int',
                        'boolean',
                        'kotlin.jvm.functions.Function2'
                    );
                    var fm = v1.call(h8y, imagePath, width, height, 0, false, null);
                    if (!fm) {
                        console.log('[send_engine] image V1 returned null, fallback to f8');
                        var f8fb = h8y.f8.overload('android.content.Context', 'long', 'java.lang.String', 'zi90');
                        ok = f8fb.call(mm2, activity, convId, imagePath, null);
                    } else {
                        try {
                            var v8 = h8y.V8.overload('com.tencent.wework.foundation.model.pb.WwRichmessage$FileMessage');
                            var v8ret = v8.call(h8y, fm);
                            console.log('[send_engine] image V8 ret=' + v8ret);
                        } catch (eV8) {
                            console.log('[send_engine] image V8 skipped: ' + eV8);
                        }
                        var q8 = h8y.q8.overload(
                            'android.content.Context',
                            'long',
                            'com.tencent.wework.foundation.model.pb.WwRichmessage$FileMessage',
                            'int',
                            'zi90',
                            'com.tencent.wework.foundation.callback.ISendMessageCallback'
                        );
                        ok = q8.call(mm2, activity, convId, fm, 7, null, null);
                    }
                } else if (action === 'oa') {
                    var built = buildLinkMessageFromTemplate('__OA_TEMPLATE_JSON__');
                    if (!built || !built.message) {
                        console.log('[send_engine] action=oa error=template build failed');
                        return;
                    }
                    var convObj = findConversationByLocal(String(convId));
                    if (!convObj) {
                        console.log('[send_engine] action=oa error=conversation not found');
                        return;
                    }
                    var sendOa = h8y.s8.overload(
                        'android.content.Context',
                        'com.tencent.wework.foundation.model.Conversation',
                        'com.tencent.wework.foundation.model.Message',
                        'zi90',
                        'com.tencent.wework.foundation.callback.ISendMessageCallback'
                    );
                    sendOa.call(h8y, activity, convObj, built.message, null, null);
                    ok = true;
                } else if (action === 'wxa') {
                    var wxaActivity = activity;
                    var wxaConvId = String(convId);
                    Java.scheduleOnMainThread(function () {
                        try {
                            console.log('[send_engine] wxa begin');
                            var wxaMsg = buildWxaMessage('__WXA_TEMPLATE_JSON__');
                            if (!wxaMsg) {
                                console.log('[send_engine] action=wxa error=template build failed');
                                return;
                            }
                            var convObjW = findConversationByLocal(wxaConvId);
                            if (!convObjW) {
                                console.log('[send_engine] action=wxa error=conversation not found');
                                return;
                            }
                            var sendWxa = h8y.s8.overload(
                                'android.content.Context',
                                'com.tencent.wework.foundation.model.Conversation',
                                'com.tencent.wework.foundation.model.Message',
                                'zi90',
                                'com.tencent.wework.foundation.callback.ISendMessageCallback'
                            );
                            sendWxa.call(h8y, wxaActivity, convObjW, wxaMsg, null, null);
                            console.log('[send_engine] action=wxa return=true');
                        } catch (eWxa) {
                            console.log('[send_engine] action=wxa error=' + eWxa);
                        }
                    });
                    return;
                } else if (action === 'video') {
                    var videoPath = '__VIDEO_PATH__';
                    var thumbPath = '__THUMB_PATH__';
                    if (!validateFile(videoPath, 'video')) return;
                    if (thumbPath && thumbPath.length > 0 && !validateFile(thumbPath, 'video_thumb')) {
                        thumbPath = '';
                    }
                    if (!thumbPath || thumbPath.length === 0) {
                        thumbPath = buildThumbFromVideo(videoPath);
                    }
                    if (!thumbPath || thumbPath.length === 0) {
                        console.log('[send_engine] action=video error=no thumb path');
                        return;
                    }
                    var fileObj = FileCls.$new(videoPath);
                    var fileSize = fileObj.length();
                    var meta = extractVideoMeta(videoPath);
                    var widthV = meta.width;
                    var heightV = meta.height;
                    var durationSec = __DURATION__;
                    if (!(durationSec > 0)) {
                        durationSec = meta.durationSec;
                    }
                    if (!(durationSec > 0)) {
                        durationSec = 1;
                    }
                    console.log('[send_engine] video meta width=' + widthV + ' height=' + heightV + ' duration=' + durationSec + ' size=' + fileSize);

                    // 运行时确认视频主入口是 D8(Activity,long,videoPath,?,thumbPath,boolean)。
                    try {
                        var mmVideo = h8y.X4();
                        var d8 = h8y.D8.overload(
                            'android.app.Activity',
                            'long',
                            'java.lang.String',
                            'java.lang.String',
                            'java.lang.String',
                            'boolean'
                        );
                        ok = d8.call(mmVideo, activity, convId, videoPath, '', thumbPath, false);
                        console.log('[send_engine] video D8 return=' + ok);
                    } catch (eD8) {
                        console.log('[send_engine] video D8 unavailable, fallback q8: ' + eD8);
                    }

                    if (ok) {
                        console.log('[send_engine] action=video path=D8');
                    }

                    var fmVideo = null;
                    if (ok) {
                        fmVideo = null;
                    } else {
                    try {
                        var u1Video = h8y.U1.overload(
                            'java.lang.String',
                            'int',
                            'int',
                            'int',
                            'boolean'
                        );
                        fmVideo = u1Video.call(h8y, videoPath, widthV, heightV, durationSec, false);
                        console.log('[send_engine] video U1 returned=' + (fmVideo ? 'object' : 'null'));
                    } catch (eU1Video) {
                        console.log('[send_engine] video U1 failed: ' + eU1Video);
                    }
                    if (!fmVideo) {
                    try {
                        var v1Video = h8y.V1.overload(
                            'java.lang.String',
                            'int',
                            'int',
                            'int',
                            'boolean',
                            'kotlin.jvm.functions.Function2'
                        );
                        fmVideo = v1Video.call(h8y, videoPath, widthV, heightV, durationSec, false, null);
                    } catch (eV1Video) {
                        console.log('[send_engine] video V1 failed: ' + eV1Video);
                    }
                    }
                    }
                    if (!fmVideo) {
                        if (!ok) {
                            console.log('[send_engine] video V1 returned null');
                            return;
                        }
                    } else {
                        setFieldIfPresent(fmVideo, 'filePath', JString.$new(videoPath));
                        setFieldIfPresent(fmVideo, 'localPath', JString.$new(videoPath));
                        setFieldIfPresent(fmVideo, 'videoPath', JString.$new(videoPath));
                        if (thumbPath && thumbPath.length > 0) {
                            setFieldIfPresent(fmVideo, 'thumbPath', JString.$new(thumbPath));
                            setFieldIfPresent(fmVideo, 'thumbFilePath', JString.$new(thumbPath));
                        }
                        setFieldIfPresent(fmVideo, 'duration', durationSec);
                        setFieldIfPresent(fmVideo, 'voiceTime', durationSec);
                        setFieldIfPresent(fmVideo, 'width', widthV);
                        setFieldIfPresent(fmVideo, 'height', heightV);
                        setFieldIfPresent(fmVideo, 'size', fileSize);
                        setFieldIfPresent(fmVideo, 'fileSize', fileSize);

                        try {
                            var v8Video = h8y.V8.overload('com.tencent.wework.foundation.model.pb.WwRichmessage$FileMessage');
                            var v8retVideo = v8Video.call(h8y, fmVideo);
                            console.log('[send_engine] video V8 ret=' + v8retVideo);
                        } catch (eV8Video) {
                            console.log('[send_engine] video V8 skipped: ' + eV8Video);
                        }
                        var q8Video = h8y.q8.overload(
                            'android.content.Context',
                            'long',
                            'com.tencent.wework.foundation.model.pb.WwRichmessage$FileMessage',
                            'int',
                            'zi90',
                            'com.tencent.wework.foundation.callback.ISendMessageCallback'
                        );
                        ok = q8Video.call(h8y.X4(), activity, convId, fmVideo, 23, null, null);
                        console.log('[send_engine] action=video path=q8_fallback');
                    }
                } else if (action === 'at' || action === 'notice') {
                    sendAtOrNotice(action, String(convId), '__BODY__');
                    return;
                } else {
                    console.log('[send_engine] unknown action=' + action);
                    return;
                }
                console.log('[send_engine] action=' + action + ' return=' + ok);
            } catch (eSend) {
                console.log('[send_engine] action=' + action + ' error=' + eSend);
            }
        });
            }
        });
    } catch (e) {
        console.log('[send_engine] init error=' + e);
    }
});
"""
        .replace("__ACTION__", action)
        .replace("__CONV_LOCAL__", _escape_js_str(conv_local))
        .replace("__SILK_PATH__", _escape_js_str(silk_path))
        .replace("__VOICE_TIME__", str(int(voice_time)))
        .replace("__BODY__", _escape_js_str(body))
        .replace("__IMAGE_PATH__", _escape_js_str(image_path))
        .replace("__VIDEO_PATH__", _escape_js_str(video_path))
        .replace("__THUMB_PATH__", _escape_js_str(thumb_path))
        .replace("__DURATION__", str(int(duration)))
        .replace("__OA_TEMPLATE_JSON__", _escape_js_str(oa_template_json))
        .replace("__WXA_TEMPLATE_JSON__", _escape_js_str(wxa_template_json))
        .replace("__ACTIVITY_GUARD__", _escape_js_str(activity_guard))
    )


def find_pid(adb_serial: str, process_name: str) -> int | None:
    raw = subprocess.check_output(
        [_adb_executable(), "-s", adb_serial, "shell", "ps -A -o PID,NAME"]
    ).decode("ascii", "replace")
    for line in raw.splitlines():
        p = line.split()
        if len(p) >= 2 and p[1] == process_name:
            return int(p[0])
    return None


def on_message(msg: dict, _data):
    if msg.get("type") == "send":
        print(msg.get("payload"))
    elif msg.get("type") == "error":
        print("[JS ERROR]", msg.get("stack", msg))
    else:
        print("[MSG]", msg)


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _adb_executable() -> str:
    value = os.environ.get(DEFAULT_ADB_BINARY_ENV, "").strip()
    return value or "adb"


def _stage_file_via_adb(*, adb_serial: str, src_path: str, dst_dir: str, marker: str) -> str:
    stamp = int(time.time() * 1000)
    base = os.path.basename(src_path) or "payload.bin"
    dst = f"{dst_dir}/{stamp}_{base}"
    src_q = _sh_quote(src_path)
    dst_q = _sh_quote(dst)
    script = (
        f"mkdir -p {_sh_quote(dst_dir)} && "
        f"cp {src_q} {dst_q} && "
        f"chmod 660 {dst_q} && "
        f'echo "__{marker}__:{dst}"'
    )
    try:
        raw = subprocess.check_output(
            [_adb_executable(), "-s", adb_serial, "shell", script]
        ).decode("utf-8", "replace")
    except subprocess.CalledProcessError as e:
        print(f"[send_engine] adb stage file failed, fallback original: {e}")
        return src_path
    for line in raw.splitlines():
        if line.startswith(f"__{marker}__:"):
            staged = line.split(":", 1)[1].strip()
            if staged:
                print(f"[send_engine] staged file via adb: {staged}")
                return staged
    return src_path


def stage_image_via_adb(adb_serial: str, image_path: str) -> str:
    if image_path.startswith("/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/"):
        return image_path
    return _stage_file_via_adb(
        adb_serial=adb_serial,
        src_path=image_path,
        dst_dir=DEFAULT_WEWORK_TEMPIMAGE_DIR,
        marker="STAGED_IMAGE",
    )


def stage_video_via_adb(adb_serial: str, video_path: str) -> str:
    if video_path.startswith("/storage/emulated/0/Android/data/com.tencent.wework/files/tempvideocache/"):
        return video_path
    return _stage_file_via_adb(
        adb_serial=adb_serial,
        src_path=video_path,
        dst_dir=DEFAULT_WEWORK_TEMPVIDEO_DIR,
        marker="STAGED_VIDEO",
    )


def stage_voice_via_adb(adb_serial: str, silk_path: str) -> str:
    """把 silk 复制进当前账号的企微 voicemsg 目录，返回设备内新路径。

    历史验收路径（docs/ANDROID_RE_HANDOFF.md §6）证明：主动发送的 silk 必须
    位于 ``voicemsg/<account_folder>/`` 内，E8 才会完成 CDN 上传并被对端收到。
    从 ``/sdcard`` 等外部路径直接发只会插入本地气泡、上传失败（红色感叹号）。

    账号子目录用「最近修改」的那个（当前登录账号录音会持续写入它）。找不到
    子目录或复制失败时回退原路径，交由上层 validateFile 兜底。
    """
    if silk_path.startswith(DEFAULT_WEWORK_VOICEMSG_DIR):
        return silk_path
    stamp = int(time.time() * 1000)
    base = DEFAULT_WEWORK_VOICEMSG_DIR
    # 单条 adb shell：选最新子目录 → cp → chmod → 回显目标路径。
    script = (
        f'base={_sh_quote(base)}; '
        'folder=""; '
        'for d in "$base"/*/; do '
        '  if [ -d "$d" ] && { [ -z "$folder" ] || [ "$d" -nt "$folder" ]; }; then folder="$d"; fi; '
        'done; '
        'if [ -z "$folder" ]; then echo "__NO_VOICE_FOLDER__"; exit 0; fi; '
        f'dst="${{folder%/}}/testvoice_{stamp}.silk"; '
        f'cp {_sh_quote(silk_path)} "$dst" && chmod 660 "$dst" && echo "__STAGED_VOICE__:$dst"'
    )
    try:
        raw = subprocess.check_output(
            [_adb_executable(), "-s", adb_serial, "shell", script]
        ).decode("utf-8", "replace")
    except subprocess.CalledProcessError as e:
        print(f"[send_engine] adb stage voice failed, fallback original: {e}")
        return silk_path
    for line in raw.splitlines():
        if line.startswith("__STAGED_VOICE__:"):
            staged = line.split(":", 1)[1].strip()
            if staged:
                print(f"[send_engine] staged voice via adb: {staged}")
                return staged
        if "__NO_VOICE_FOLDER__" in line:
            print("[send_engine] no voicemsg account folder found, fallback original")
    return silk_path


def stage_thumb_via_adb(adb_serial: str, thumb_path: str) -> str:
    if thumb_path.startswith("/storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/"):
        return thumb_path
    return _stage_file_via_adb(
        adb_serial=adb_serial,
        src_path=thumb_path,
        dst_dir=DEFAULT_WEWORK_TEMPIMAGE_DIR,
        marker="STAGED_THUMB",
    )


def run_script(js: str, *, adb_serial: str, frida_remote: str, process_name: str, wait_seconds: float) -> int:
    session = FridaSession(
        frida_remote=frida_remote,
        process_name=process_name,
        pid_provider=find_pid,
    )
    try:
        attached = session.attach(adb_serial=adb_serial)
    except Exception as exc:
        print(f"[send_engine] process attach failed: {exc}")
        return 1
    try:
        if not session.heartbeat():
            ok = session.reconnect(adb_serial=adb_serial, retries=3)
            if not ok:
                print(f"[send_engine] heartbeat/reconnect failed: {session.state.last_error}")
                return 2
            attached = session.session
            if attached is None:
                print("[send_engine] reconnect returned empty session")
                return 2
        script = attached.create_script(js)
        script.on("message", on_message)
        script.load()
        time.sleep(wait_seconds)
        return 0
    finally:
        session.detach()


def _load_wxa_template(template_ref: str, *, account_id: str) -> dict[str, Any]:
    ref = template_ref.strip()
    if not ref:
        raise ValueError("template 不能为空")
    path = Path(ref)
    if path.suffix.lower() == ".json" and path.is_file():
        raw = path.read_text(encoding="utf-8").lstrip()
        if raw.startswith("{"):
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError(f"wxa template 结构非法: {path}")
            return payload
    from app.broadcast.wxa_templates import load_template as load_wxa

    return load_wxa(ref, account_id=(account_id or None))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WeCom h8y send engine")
    parser.add_argument("--account-id", default="", help="账号命名空间（默认读取当前登录账号）")
    parser.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    parser.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--activity-guard", default=DEFAULT_ACTIVITY_GUARD)
    parser.add_argument("--wait-seconds", type=float, default=DEFAULT_WAIT_SECONDS)

    sub = parser.add_subparsers(dest="action", required=True)

    p_voice = sub.add_parser("voice", help="send voice by silk path")
    p_voice.add_argument("--conv-local", required=True)
    p_voice.add_argument("--silk-path", required=True)
    p_voice.add_argument("--voice-time", type=int, required=True)

    p_text = sub.add_parser("text", help="send text")
    p_text.add_argument("--conv-local", required=True)
    p_text.add_argument("--body", required=True)

    p_at = sub.add_parser("at", help="send @all plus text in a group")
    p_at.add_argument("--conv-local", required=True)
    p_at.add_argument("--body", required=True)

    p_notice = sub.add_parser("notice", help="publish a group notice")
    p_notice.add_argument("--conv-local", required=True)
    p_notice.add_argument("--body", required=True)

    p_image = sub.add_parser("image", help="send image by local path on Android")
    p_image.add_argument("--conv-local", required=True)
    p_image.add_argument("--image-path", required=True)

    p_oa = sub.add_parser("oa", help="send official-account appmsg by template")
    p_oa.add_argument("--conv-local", required=True)
    p_oa.add_argument("--template", required=True, help="template id or template json path")

    p_wxa = sub.add_parser("wxa", help="send mini-program card (contentType 78)")
    p_wxa.add_argument("--conv-local", required=True)
    p_wxa.add_argument("--template", required=True, help="wxa template id or template json path")

    p_video = sub.add_parser("video", help="send short video by local path on Android")
    p_video.add_argument("--conv-local", required=True)
    p_video.add_argument("--video-path", required=True)
    p_video.add_argument("--thumb-path", default="")
    p_video.add_argument("--duration", type=int, default=0, help="seconds; <=0 means auto detect")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    account_id = resolve_account_id(getattr(args, "account_id", ""))
    print(f"[send_engine] account_id={account_id}")
    action = args.action
    image_path = getattr(args, "image_path", "")
    video_path = getattr(args, "video_path", "")
    thumb_path = getattr(args, "thumb_path", "")
    oa_template_json = ""
    wxa_template_json = ""
    silk_path = getattr(args, "silk_path", "")
    if action == "voice":
        silk_path = stage_voice_via_adb(args.adb_serial, silk_path)
    if action == "image":
        image_path = stage_image_via_adb(args.adb_serial, image_path)
    if action == "video":
        video_path = stage_video_via_adb(args.adb_serial, video_path)
        if thumb_path:
            thumb_path = stage_thumb_via_adb(args.adb_serial, thumb_path)
    if action == "oa":
        template = load_template(getattr(args, "template"), account_id=(args.account_id or None))
        oa_template_json = json.dumps(template, ensure_ascii=False)
    if action == "wxa":
        template = _load_wxa_template(getattr(args, "template"), account_id=(args.account_id or ""))
        wxa_template_json = json.dumps(template, ensure_ascii=False)
    js = build_js(
        action=action,
        conv_local=args.conv_local,
        silk_path=silk_path,
        voice_time=getattr(args, "voice_time", 0),
        body=getattr(args, "body", ""),
        image_path=image_path,
        video_path=video_path,
        thumb_path=thumb_path,
        duration=getattr(args, "duration", 0),
        oa_template_json=oa_template_json,
        wxa_template_json=wxa_template_json,
        activity_guard=args.activity_guard,
    )
    wait_seconds = args.wait_seconds
    conv_local = str(args.conv_local)
    if action in {"at", "notice"}:
        body = str(getattr(args, "body", "") or "")
        if not body.strip():
            print(f"[send_engine] action={action} error=empty body")
            print(f"[send_engine] action={action} return=false")
            return 2
        wait_seconds = max(wait_seconds, 8.0)
    if conv_local.startswith("friend:") or conv_local.startswith("788"):
        wait_seconds = max(wait_seconds, 8.0)
    return run_script(
        js,
        adb_serial=args.adb_serial,
        frida_remote=args.frida_remote,
        process_name=args.process_name,
        wait_seconds=wait_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
