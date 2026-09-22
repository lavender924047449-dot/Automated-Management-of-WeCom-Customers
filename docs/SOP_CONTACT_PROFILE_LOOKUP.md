# SOP: 联系人昵称 / 备注 / 标签 一键抓取

> **总览文档**：[`SOP_WEWORK_CONTACT_LABELS_PLAYBOOK.md`](SOP_WEWORK_CONTACT_LABELS_PLAYBOOK.md)（含 Step 11 逆向、β 手打、按标签解析等全部固定流程）  
> 本文档仅展开 **§4 日常查联系人资料** 的细节。

> 适用于「已经完成 Step 11 客户标签取证、Frida 服务在线」的常规运维场景。
> 目标：给一个外部微信联系人（Conversation.type=0），一次 attach 拿到昵称、备注、命中的所有客户标签（id + 名字）。
## 0. 前置条件
- MuMu / 真机上 `com.tencent.wework` 已登录且已经把该联系人加为客户。
- `frida-server` 在 `127.0.0.1:27042`，adb 端口 `127.0.0.1:16384`（如不同，用 `--adb-serial` / `--frida-remote` 覆盖）。
- `.venv` 已装 `frida==16.7.19`。

## 1. 一句话命令

```powershell
.venv\Scripts\python.exe -m runtime.wework_re_android.dump_contact_profile `
    --name "海鸟与鱼" `
    --out "runtime/reports/contact_profile_<name>_<yyyymmdd>.json"
```

三种定位方式（互斥或并用，命中一个即可）：
| 参数           | 场景                                          | 稳定度 |
| -------------- | --------------------------------------------- | ------ |
| `--conv-local` | 会话内部 id（`Conversation.Local: xxx`）      | ⭐⭐⭐    |
| `--remote`     | 外部微信 remoteId（跨端稳定）                 | ⭐⭐⭐    |
| `--name`       | 精确匹配 `Conversation.getDefaultName(true)`  | ⭐⭐     |

## 2. 输出示例

```json
{
  "found": true,
  "conversation": { "defaultName": "海鸟与鱼", "remoteId": "7881300363276969", "convLocalRaw": "7685582584742596232", "type": 0 },
  "user":         { "name": "海鸟与鱼", "realRemark": "海鸟与鱼-老客", "displayName": "海鸟与鱼-老客", "corpId": "1970325134026788" },
  "labels":       [ { "id": "14073749343974545", "scene": "1", "name": "测试5a" } ]
}
```

字段来源速查：

| 字段          | 取自                                                                  |
| ------------- | --------------------------------------------------------------------- |
| 昵称          | `Conversation.getDefaultName(true)` == `User.getName()`               |
| 备注          | `User.getRealRemark()` / `getDisplayName()`（两者相同即无二次改写）   |
| 标签 id       | `ContactService.GetAllLabelIdsHasContact(1)` 全量遍历，用 remoteId 过滤 |
| 标签名        | `runtime/customer_label_names.json` 手动/半自动映射                     |

## 3. 内部流程（三次同步调用）
```
GetConversationList()                          # 定位 conv / remote / conv_local
  ↓
GetAllLabelIdsHasContact(scene=1)              # 拿所有客户标签 id
  ↓
GetLabelContactsByLabelId(id, scene=1) × N     # 逐个 label 拉 User[], remote 命中即摘 remark
```
全部走同步返回值，**不用**异步 pb 回调（`GetServiceLabels` / `GetCacheLabelFullInfoById` 之类），因此不会像上一版反复触发 `TransportError: the connection is closed`。

## 4. 标签名怎么维护
- 位置：`runtime/customer_label_names.json`
- 格式：`{"<label_id 十进制字符串>": "<中文名>"}`，`_comment` 之类下划线开头 key 会被忽略。
- 新增标签流程：
  1. 在企微里用户手动打过一次标签。
  2. 跑 `dump_contact_profile.py --name <任意含新标签的联系人>` → `labels[].name` 为空说明未映射。
  3. 用 `probe_customer_labels_rpc.py`（Step 11 α 探针）打开标签编辑页触发一次保存，看 `arg1:j=<name> arg1:n=<id>` 记进 ndjson。
  4. 把 `id -> name` 写进 `customer_label_names.json`。

> ⚠️ 不要把没经 α/UI 双确认的 id→name 猜进 JSON，会污染整个下游。

## 5. 常见问题

| 现象                              | 原因                                                | 处理                                                  |
| --------------------------------- | --------------------------------------------------- | ----------------------------------------------------- |
| `found: false`                    | conv 未创建过或该联系人是企业员工（type≠0）         | 换 `--remote`；或先在企微里点开会话让 conv 落地       |
| `labels: []` 但联系人页明明有标签 | scene 只扫了 1（外部客户），公司内标签在 scene=0/2  | 编辑脚本 `scenes = [1]` 加入 0/2                       |
| `TransportError` / 脚本卡住       | Frida server 断了 / 企微被反调触发                  | `adb shell pidof com.tencent.wework` 确认；重启 frida-server |
| 昵称是「海鸟...」等英文乱码       | Windows 终端 cp936 显示问题，磁盘 UTF-8 文件是对的  | 直接看 `--out` 落盘的 JSON                            |

## 6. 与旧手段的差别

| 维度                | 旧手段（Step 11 调试期）                                          | 现 SOP                                    |
| ------------------- | ----------------------------------------------------------------- | ----------------------------------------- |
| 脚本数              | 3 个 `_tmp_dump_hainiao_live*.py`                                 | 1 个正式模块                              |
| Frida 调用类型      | 同步 + 异步 pb 回调混合，易触发反调                               | 只用 3 个同步 getter                      |
| 平均耗时            | 20~60 秒 且经常 `TransportError`                                  | ~3 秒稳定                                 |
| 标签名解析          | 需 pb decode `GetServiceLabels` 回调                              | 查本地 JSON                               |
| 可复用性            | 一次性脚本                                                        | 命令化，`--out` 归档，可入 CI/巡检        |

## 7. 相关文件
- 主脚本：[`runtime/wework_re_android/dump_contact_profile.py`](../runtime/wework_re_android/dump_contact_profile.py)
- 标签名缓存：[`runtime/customer_label_names.json`](../runtime/customer_label_names.json)
- 参考探针（α 取证，用于补标签名）：[`runtime/wework_re_android/probe_customer_labels_rpc.py`](../runtime/wework_re_android/probe_customer_labels_rpc.py)
- β 兜底存储：[`app/broadcast/labels.py`](../app/broadcast/labels.py) + [`runtime/wework_re_android/label_store_cli.py`](../runtime/wework_re_android/label_store_cli.py)
