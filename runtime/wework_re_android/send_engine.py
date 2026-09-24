"""
Step 3/9: 统一发送引擎 (voice / text / image / oa / video)。

CLI:
  python -m runtime.wework_re_android.send_engine voice --conv-local ... --silk-path ... --voice-time 3
  python -m runtime.wework_re_android.send_engine text  --conv-local ... --body "你好"
  python -m runtime.wework_re_android.send_engine image --conv-local ... --image-path /sdcard/...
  python -m runtime.wework_re_android.send_engine oa    --conv-local ... --template <id-or-json-path>
  python -m runtime.wework_re_android.send_engine video --conv-local ... --video-path /sdcard/a.mp4 [--thumb-path /sdcard/a.jpg] [--duration 8]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Literal

from app.broadcast.account import resolve_account_id
from app.broadcast.oa_templates import load_template
from runtime.wework_re_android.frida_session import FridaSession


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
    action: Literal["voice", "text", "image", "oa", "video"],
    conv_local: str,
    silk_path: str = "",
    voice_time: int = 0,
    body: str = "",
    image_path: str = "",
    video_path: str = "",
    thumb_path: str = "",
    duration: int = 0,
    oa_template_json: str = "",
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
        var Base64 = Java.use('android.util.Base64');
        var BitmapFactory = Java.use('android.graphics.BitmapFactory');
        var BitmapOptions = Java.use('android.graphics.BitmapFactory$Options');
        var BitmapCompressFormat = Java.use('android.graphics.Bitmap$CompressFormat');
        var MediaMetadataRetriever = Java.use('android.media.MediaMetadataRetriever');

        var convId = new Int64('__CONV_LOCAL__');
        var action = '__ACTION__';

        function getForegroundActivity() {
            var at = ActivityThread.currentActivityThread();
            var atCls = at.getClass();
            var mActivitiesField = atCls.getDeclaredField('mActivities');
            mActivitiesField.setAccessible(true);
            var acts = mActivitiesField.get(at);
            var actsMap = Java.cast(acts, MapCls);
            var values = actsMap.values().toArray();
            for (var i = 0; i < values.length; i++) {
                var rec = values[i];
                var recCls = rec.getClass();
                var pausedField = recCls.getDeclaredField('paused');
                pausedField.setAccessible(true);
                if (!pausedField.getBoolean(rec)) {
                    var actField = recCls.getDeclaredField('activity');
                    actField.setAccessible(true);
                    return actField.get(rec);
                }
            }
            return null;
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
            var emptyBytes = Java.array('byte', []);
            var linkMsg = h8y.f2(linkUrl, title, desc, imageUrl, emptyBytes);
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

        Java.scheduleOnMainThread(function () {
            try {
                var ok = false;
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
                } else {
                    console.log('[send_engine] unknown action=' + action);
                    return;
                }
                console.log('[send_engine] action=' + action + ' return=' + ok);
            } catch (eSend) {
                console.log('[send_engine] action=' + action + ' error=' + eSend);
            }
        });
    } catch (e) {
        console.log('[send_engine] init error=' + e);
    }
});
"""
        .replace("__ACTION__", action)
        .replace("__CONV_LOCAL__", conv_local)
        .replace("__SILK_PATH__", _escape_js_str(silk_path))
        .replace("__VOICE_TIME__", str(int(voice_time)))
        .replace("__BODY__", _escape_js_str(body))
        .replace("__IMAGE_PATH__", _escape_js_str(image_path))
        .replace("__VIDEO_PATH__", _escape_js_str(video_path))
        .replace("__THUMB_PATH__", _escape_js_str(thumb_path))
        .replace("__DURATION__", str(int(duration)))
        .replace("__OA_TEMPLATE_JSON__", _escape_js_str(oa_template_json))
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

    p_image = sub.add_parser("image", help="send image by local path on Android")
    p_image.add_argument("--conv-local", required=True)
    p_image.add_argument("--image-path", required=True)

    p_oa = sub.add_parser("oa", help="send official-account appmsg by template")
    p_oa.add_argument("--conv-local", required=True)
    p_oa.add_argument("--template", required=True, help="template id or template json path")

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
        activity_guard=args.activity_guard,
    )
    return run_script(
        js,
        adb_serial=args.adb_serial,
        frida_remote=args.frida_remote,
        process_name=args.process_name,
        wait_seconds=args.wait_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
