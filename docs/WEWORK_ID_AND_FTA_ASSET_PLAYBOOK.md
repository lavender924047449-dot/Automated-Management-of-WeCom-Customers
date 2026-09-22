# 企微 ID 与 FTA 素材调取实战手册

## 1. 目的与范围

本文用于沉淀三类能力：

- 调取企微会话 ID（个人 / 群聊）
- 从 FTA（文件传输助手）调取可复用素材
- 按统一命令发送不同类型消息：文字（含小程序链接）、语音、图片、视频、公众号卡片

> 当前版本口径：小程序走“链接替代”（text/link），不是原生 wxa card。

---

## 2. 运行前准备

在项目根目录 `d:\Only internship outputs\Test-Voice` 执行，建议先设置 UTF-8，避免中文解码问题：

```powershell
$env:PYTHONUTF8='1'
```

基础环境：

- 企微进程已登录并存活（`com.tencent.wework`）
- `adb` 可连（默认 `127.0.0.1:16384`）
- `frida-server` 可连（默认 `127.0.0.1:27042`）
- 使用项目虚拟环境 Python：`.\\.venv\\Scripts\\python.exe`

---

## 3. 调取个人/群聊 ID（conv_local）

### 3.1 推荐方法：完整会话清单（最稳）

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.dump_conversation_list
```

输出示例（关键在 `Local`）：

- `Conversation : Local: 7685582584742596232 remote: 7881300363276969 type:0`（个人）
- `Conversation : Local: 7685582584742596198 remote: 10839797135036051 type:1`（群聊）

判定规则：

- `type:0` 常见为单聊
- `type:1` 常见为群聊

---

### 3.2 关键词方法：按名字检索

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.list_conversations_with_names --keyword "海鸟与鱼" --refresh --wait-seconds 12
```

说明：

- `--refresh` 会先拉取会话再检索
- 如果 `local=?`（某些对象字段读取失败），以 3.1 的 `raw` 行里的 `Local: <数字>` 为准

---

## 4. 从 FTA 调取素材的方法

## 4.1 语音素材（自动入库）

运行抓取器：

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.fta_capture --fta-remote-id 10004 --timeout-seconds 180
```

然后手动在企微里向 FTA 发一条语音（可在 30 秒内再发一条文字作为 note）。

产物：

- 音频文件：`/sdcard/Test-Voice/voice_lib/*.silk`
- 索引：`runtime/voice_lib_index.json`

---

## 4.2 图片素材（链路探针 + 文件定位）

运行探针：

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.probe_fta_image_pipeline_v3 --timeout-seconds 180
```

手动向 FTA 发图后，查看：

- 事件证据：`runtime/wework_re_android/artifacts/image_pipeline_v3.ndjson`
- 常见目录：
  - `.../files/tempimagecache/`
  - `.../files/uploadTempMidbimage/`
  - `.../files/uploadTempThumbimage/`

快速看最近文件：

```powershell
adb -s 127.0.0.1:16384 shell ls -lt /storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache
```

---

## 4.3 视频素材（链路探针 + 本体/缩略图）

运行探针：

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.probe_fta_video_pipeline --timeout-seconds 180
```

手动向 FTA 发一条短视频（<=10s），查看：

- 事件证据：`runtime/wework_re_android/artifacts/video_pipeline.ndjson`
- 常见路径：
  - 视频本体：`.../files/filecache/<conv>/.../*.mp4`
  - 视频 thumb：`.../files/tempimagecache/<conv>/video_thumb/*_thumb.wwdata`

---

## 4.4 公众号卡片素材（模板化）

运行探针：

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.probe_fta_oa_pipeline --timeout-seconds 180
```

手动执行：公众号文章 -> 分享给朋友 -> 企微 FTA。

产物：

- 事件证据：`runtime/wework_re_android/artifacts/oa_pipeline.ndjson`
- 模板文件：`app/broadcast/oa_templates/<template_id>.json`

发送时直接引用 `template_id`。

---

## 4.5 小程序素材（当前业务口径）

当前交付口径是**小程序链接替代**，不走 wxa card：

- 在微信/企微内拿到可转发小程序链接
- 作为普通文本消息发送（见 5.2）

---

## 5. 各类型素材发送方法（send_engine）

统一脚本：

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine <subcommand> ...
```

## 5.1 语音

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine voice --conv-local <CONV_LOCAL> --silk-path /sdcard/Test-Voice/voice_lib/<file>.silk --voice-time 3
```

---

## 5.2 文字（含小程序链接）

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine text --conv-local <CONV_LOCAL> --body "你好"
```

小程序链接替代示例：

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine text --conv-local <CONV_LOCAL> --body "小程序链接：https://work.weixin.qq.com/"
```

---

## 5.3 图片

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine image --conv-local <CONV_LOCAL> --image-path /storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/<...>.png
```

说明：脚本会自动做必要预落盘，减少权限/路径导致的发送失败。

---

## 5.4 视频

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine --wait-seconds 10 video --conv-local <CONV_LOCAL> --video-path /storage/emulated/0/Android/data/com.tencent.wework/files/filecache/<...>.mp4
```

可选参数：

- `--thumb-path <path>`：不传则自动抽首帧
- `--duration <sec>`：不传则自动探测

当前视频主发链为 `h8y.D8(...)`，为稳定路径。

---

## 5.5 公众号卡片

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine oa --conv-local <CONV_LOCAL> --template <TEMPLATE_ID_OR_JSON_PATH>
```

示例（历史可用模板）：

```powershell
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine oa --conv-local <CONV_LOCAL> --template 11a3bca9-f3a1-441d-a5d2-0cabf8d8b018
```

---

## 6. 一次性顺序发送模板（单目标）

将以下 `<CONV_LOCAL>` 替换为目标会话 ID：

```powershell
$env:PYTHONUTF8='1'
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine text --conv-local <CONV_LOCAL> --body "文字"
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine image --conv-local <CONV_LOCAL> --image-path /storage/emulated/0/Android/data/com.tencent.wework/files/tempimagecache/<...>.png
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine --wait-seconds 10 video --conv-local <CONV_LOCAL> --video-path /storage/emulated/0/Android/data/com.tencent.wework/files/filecache/<...>.mp4
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine text --conv-local <CONV_LOCAL> --body "小程序链接：https://work.weixin.qq.com/"
.\\.venv\\Scripts\\python.exe -m runtime.wework_re_android.send_engine oa --conv-local <CONV_LOCAL> --template 11a3bca9-f3a1-441d-a5d2-0cabf8d8b018
```

---

## 7. 常见问题排查

- `红色感叹号`：优先检查是否走到视频主链 `D8`，并确认视频/thumb 文件存在且可读。
- `找不到 conv_local`：先用 `dump_conversation_list`，再按 `raw` 字段取 `Local`。
- `中文乱码或检索异常`：先执行 `$env:PYTHONUTF8='1'`。
- `return=true 但对端未见`：以对端真实可见为准，必要时做单步重发+探针复核。

---

## 8. 关键脚本索引

- 会话 ID：
  - `runtime/wework_re_android/dump_conversation_list.py`
  - `runtime/wework_re_android/list_conversations_with_names.py`
- FTA 素材：
  - `runtime/wework_re_android/fta_capture.py`
  - `runtime/wework_re_android/probe_fta_image_pipeline_v3.py`
  - `runtime/wework_re_android/probe_fta_video_pipeline.py`
  - `runtime/wework_re_android/probe_fta_oa_pipeline.py`
- 发送入口：
  - `runtime/wework_re_android/send_engine.py`
