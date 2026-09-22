# 企微联系人 / 客户标签 — 固定流程 Playbook

> **用途**：把 Step 11 逆向阶段 + 日常运维阶段已经跑通、可复用的命令与操作步骤集中成一份 SOP，后续按本文固定流程走，不再临时写 `_tmp_*.py`。
>
> **最后更新**：2026-09-16  
> **关联计划**：[`PLAN_VOICE_LIBRARY_AND_BROADCAST.md`](PLAN_VOICE_LIBRARY_AND_BROADCAST.md) Step 11 / 手动清单 K1–K4  
> **详细笔记**：[`runtime/wework_re_android/notes/customer_labels.md`](../runtime/wework_re_android/notes/customer_labels.md)

---

## 0. 什么时候用哪条流程

| 你的目标 | 用哪一节 | 一句话 |
| -------- | -------- | ------ |
| 查某个人的**昵称 / 备注 / 已有标签** | [§4](#4-日常运维查联系人资料昵称--备注--标签) | `dump_contact_profile` 一次 attach ~3s |
| 按标签找要群发的 **conv_local 列表** | [§5](#5-业务广播按标签解析-conv_localαβ-混合) | `conv_resolver --label` 或 β CLI |
| 企微里删不了标签、只能手打映射 | [§5.2](#52-β-本地标签存储手打) | `label_store_cli set/add` |
| 逆向取证：标签 ID / 保存链 / UI 类名 | [§3](#3-step-11-逆向取证固定流程) | `probe_customer_labels_rpc` |
| 验证 Conversation 里有没有标签字段 | [§3.1](#31-112-conversation-快照探针已证伪) | `probe_customer_labels`（已知无效） |
| 补全新标签的 **id → 中文名** | [§6](#6-标签名缓存维护) | RPC 探针 + `customer_label_names.json` |
| 回归验收 Step 11 | [§8](#8-验收清单-k1k4) | 对照 K1–K4 |

---

## 1. 通用前置条件

每次跑 Frida 脚本前，确认以下项（**固定默认值**，除非环境不同）：

| 项 | 默认值 | 检查命令 |
| -- | ------ | -------- |
| Python | 项目 `.venv` | `.venv\Scripts\python.exe --version` |
| frida 包 | `16.7.19` | `.venv\Scripts\pip show frida` |
| adb 设备 | `127.0.0.1:16384`（MuMu） | `adb -s 127.0.0.1:16384 devices` |
| frida-server | `127.0.0.1:27042` | attach 不报错即可 |
| 企微进程 | `com.tencent.wework` 已登录 | `adb -s 127.0.0.1:16384 shell pidof com.tencent.wework` |

**固定解释器前缀**（下文命令均以此开头）：

```powershell
$PY = ".\.venv\Scripts\python.exe"
```

**原则**（Step 11 已验证）：

- 优先 **attach-only**，不要 spawn、不要 force-stop 企微（防掉登录）。
- 优先 **同步 getter**（`GetConversationList` / `GetAllLabelIdsHasContact` / `GetLabelContactsByLabelId`），**不要**混用异步 pb 回调（易 `TransportError: the connection is closed`）。
- 终端中文乱码时，以 `--out` 落盘的 UTF-8 JSON 为准。

---

## 2. 架构速记（α / β 两路）

```
                    ┌─────────────────────────────────────┐
  企微原生标签       │  α 路径                              │
  (ContactService)  │  GetAllLabelIdsHasContact           │
                    │  → GetLabelContactsByLabelId        │
                    │  → User.getRealRemark / remoteId    │
                    └──────────────┬──────────────────────┘
                                   │  Frida 失败或无命中
                                   ▼
                    ┌─────────────────────────────────────┐
  本地 JSON 手打     │  β 路径                              │
  customer_labels   │  LocalLabelStore                    │
                    │  conv_local → [标签名…]              │
                    └──────────────┬──────────────────────┘
                                   │
                                   ▼
              conv_resolver.resolve_by_label(tag)
              → 去重合并 → [conv_local, …]
```

**已知阻塞（移交 Step 12）**：α 侧的 `mTagFriendsHasSet`（CRM 好友 ID，`140737…`）与 `conv_local` / `remoteId` **零交集**，因此 `resolve_by_label` 的 α 直出 conv_local 数量对齐尚未闭环（手动清单 **K3**）。

---

## 3. Step 11 逆向取证固定流程

> 本节记录「前几轮对话里已经固定下来」的逆向步骤；日常查联系人资料**不必**再走 §3，直接用 §4。

### 3.1 11.2 Conversation 快照探针（已证伪）

**结论**：标签**不会**稳定出现在 `Conversation` / `ConversationInfo` 反射字段里；连续采样 `label_changes=0`。

**固定命令**：

```powershell
& $PY -m runtime.wework_re_android.probe_customer_labels `
    --timeout-seconds 180 `
    --interval-seconds 2
```

**固定人工操作**：脚本运行期间，在企微里给某个外部客户手动打/改标签。

**固定产物**：`runtime/wework_re_android/artifacts/customer_labels.ndjson`

**固定判读**：看 `labelsSnapshot` 是否有真实标签文本变化；若无 → 转 §3.2。

---

### 3.2 11.3 / 11.5 RPC lazy-fetch 探针（α 取证主链）

**结论**：标签主链在 **标签页 UI + RPC**；已稳定抓到标签文本、标签 ID、保存调用链。

**固定命令**：

```powershell
& $PY -m runtime.wework_re_android.probe_customer_labels_rpc `
    --timeout-seconds 180 `
    --watch-keyword ""
```

**固定人工操作**（脚本提示后执行）：

1. 打开**外部客户**详情页  
2. 进入「标签」/「客户标签」编辑  
3. 勾选或取消标签（MuMu 里可能无法删除，勾选已有标签即可）  
4. 点**保存**

**固定产物**：`runtime/wework_re_android/artifacts/customer_labels_rpc.ndjson`

**固定判读**（在 ndjson 里搜 `event=rpc_hit`）：

| 观测项 | 典型证据 |
| ------ | -------- |
| UI 入口 | `CustomerTagMarkSettingActivity` |
| 保存链 | `UserLabelService.nativeAddOrModifyLabel`、`AddOrModifyLabel` |
| 标签对象 `wce` | `j=<标签文本>`，`n=<标签 ID>` |
| 分组对象 `nod` | `f=个人标签`，`e=14073751983981222` |
| 目标集合 | `mTagFriendsHasSet=[14073749343974097, …]` |

**已确认标签 ID 对照表**（2026-09-16 取证）：

| 标签名 | label_id |
| ------ | -------- |
| 测试4 | `14073751800998929` |
| 测试5a | `14073749343974545` |
| 个人标签（分组） | `14073751983981222` |

---

### 3.3 11.6 验收固定流程

**α 取证验收** — 确认 ndjson 含上表字段 → 报告见  
`runtime/reports/step11_6_acceptance_report_20260916.json`

**β 业务验收** — 固定跑单测：

```powershell
& $PY -m pytest tests/test_local_label_store.py `
              tests/test_conv_resolver_android.py `
              tests/test_label_store_cli.py -q
```

**β CLI 烟测**（临时文件，不污染正式映射）：

```powershell
$tmp = Join-Path $env:TEMP "tv_labels_smoke.json"
& $PY -m runtime.wework_re_android.label_store_cli --path $tmp set --conv 7685582584742596232 --labels "新客,测试5a"
& $PY -m runtime.wework_re_android.label_store_cli --path $tmp resolve --tag 新客
& $PY -m runtime.wework_re_android.label_store_cli --path $tmp resolve --tag 测试5a
```

期望：`7685582584742596232` 出现在两次 resolve 输出中。

---

## 4. 日常运维：查联系人资料（昵称 / 备注 / 标签）

> **本轮（2026-09-16）新增的固定流程**，替代临时脚本 `_tmp_dump_hainiao_live*.py`。

### 4.1 固定命令

```powershell
& $PY -m runtime.wework_re_android.dump_contact_profile `
    --name "海鸟与鱼" `
    --out "runtime/reports/contact_profile_海鸟与鱼_20260916.json"
```

三种定位方式（至少提供一个）：

| 参数 | 适用 | 稳定度 |
| ---- | ---- | ------ |
| `--conv-local` | 已知会话内部 id | ⭐⭐⭐ |
| `--remote` | 已知外部微信 remoteId | ⭐⭐⭐ |
| `--name` | 精确匹配 `getDefaultName(true)` | ⭐⭐ |

### 4.2 内部调用链（同步，单次 attach）

```
GetConversationList()                     → conv_local / remoteId / defaultName
GetAllLabelIdsHasContact(scene=1)         → 全部客户标签 id
GetLabelContactsByLabelId(id, 1) × N      → remoteId 命中 → User 快照
```

### 4.3 字段来源

| 输出字段 | Java 来源 |
| -------- | --------- |
| 昵称 | `Conversation.getDefaultName(true)` ≈ `User.getName()` |
| 备注 | `User.getRealRemark()` / `getDisplayName()` |
| 标签 id | `GetLabelContactsByLabelId` 命中时的 label id |
| 标签名 | `runtime/customer_label_names.json` 映射 |

### 4.4 已验证样例（海鸟与鱼）

| 字段 | 值 |
| ---- | -- |
| 昵称 | 海鸟与鱼 |
| 备注 | 海鸟与鱼-老客 |
| 标签 | 测试5a（id=`14073749343974545`） |
| conv_local | `7685582584742596232` |
| remote | `7881300363276969` |
| 耗时 | ~3s，无 TransportError |

报告：`runtime/reports/contact_profile_hainiao_20260916.json`

### 4.5 常见问题

| 现象 | 处理 |
| ---- | ---- |
| `found: false` | 先在企微里点开会话让 conv 落地；或换 `--remote` |
| `labels[].name` 为空 | 标签 id 已抓到，按 [§6](#6-标签名缓存维护) 补 JSON |
| `labels: []` 但 UI 有标签 | 外部客户标签 scene=1；公司内标签需扩展 `scenes` |
| 终端中文乱码 | 看 `--out` JSON 文件 |

---

## 5. 业务广播：按标签解析 conv_local（α+β 混合）

### 5.1 α+β 统一入口

```powershell
# 按标签名反查所有 conv_local（α 命中 + β 本地映射，去重合并）
& $PY -m runtime.wework_re_android.conv_resolver --label 新客

# 按联系人昵称反查 conv_local
& $PY -m runtime.wework_re_android.conv_resolver --name 海鸟与鱼

# 强制刷新会话列表后再解析
& $PY -m runtime.wework_re_android.conv_resolver --label 新客 --refresh
```

**行为**（Step 11.6 已固定）：

1. 先走 Frida → `list_conversations_with_names.py` 输出里的 `labels=[…]`  
2. Frida 失败或 α 无命中 → 读 `runtime/customer_labels.json`（β）  
3. α 与 β 同时有值 → **去重合并**  
4. 两者皆空 → `LookupError`

### 5.2 β 本地标签存储（手打）

正式映射文件：`runtime/customer_labels.json`

```powershell
# 列出全部 conv_local → 标签
& $PY -m runtime.wework_re_android.label_store_cli list

# 覆盖设置某会话的标签
& $PY -m runtime.wework_re_android.label_store_cli set `
    --conv 7685582584742596232 --labels "新客,测试5a"

# 追加单个标签
& $PY -m runtime.wework_re_android.label_store_cli add `
    --conv 7685582584742596232 --label 重点

# 删除单个标签 / 整条映射
& $PY -m runtime.wework_re_android.label_store_cli rm --conv 7685582584742596232 --label 重点
& $PY -m runtime.wework_re_android.label_store_cli drop --conv 7685582584742596232

# 仅 β 路径反查
& $PY -m runtime.wework_re_android.label_store_cli resolve --tag 新客
```

**典型场景**：MuMu 里企微无法删除标签 → 运营在企微 UI 手动打标签 → 用 §4 查到 conv_local 和标签 id → 用 `label_store_cli set` 写入 β 映射 → 广播侧 `resolve_by_label` 可用。

### 5.3 枚举全部会话（带 labels 字段）

```powershell
& $PY -m runtime.wework_re_android.list_conversations_with_names --keyword 海鸟
& $PY -m runtime.wework_re_android.list_conversations_with_names --refresh
```

输出 `[HIT] local=… remote=… name='…' members=[…] labels=[…]`，供 `conv_resolver` 解析。

---

## 6. 标签名缓存维护

**文件**：`runtime/customer_label_names.json`  
**格式**：`{"<label_id>": "<中文名>"}`（`_` 开头 key 会被忽略）

**固定补录流程**：

1. §4 `dump_contact_profile` → 若 `labels[].name` 为空但 `id` 有值  
2. §3.2 `probe_customer_labels_rpc` → 在 ndjson 找 `wce` 的 `j=<名>` `n=<id>`  
3. 把确认过的 `id → name` 写入 `customer_label_names.json`  
4. 再跑 §4 验证 `name` 已填充  

> ⚠️ 禁止猜测写入未经验证的 id→name，会污染下游广播筛选。

---

## 7. 推荐日常 SOP（运营 / 开发通用）

### 场景 A：确认某个客户现在叫什么、什么标签

```
§1 环境检查 → §4 dump_contact_profile --name "<昵称>" --out runtime/reports/...
```

### 场景 B：新建广播计划，按「新客」标签选人

```
§5.1 conv_resolver --label 新客
  ├─ 有结果 → 直接用 conv_local 列表
  └─ 无结果 / Frida 挂 → §5.2 label_store_cli 手打映射 → 再 resolve
```

### 场景 C：企微里新增了一个标签，系统还不认识

```
§3.2 RPC 探针保存一次 → §6 写 customer_label_names.json
→ §5.2 给已知 conv_local 写入 β 映射（可选）
```

### 场景 D：Step 11 回归 / 换机验收

```
§3.2 RPC 探针 → §3.3 单测 + 烟测 → §8 K1–K4 对照
```

---

## 8. 验收清单 K1–K4

| 编号 | 内容 | 状态 | 对应流程 |
| ---- | ---- | ---- | -------- |
| K1 | 标签页操作可命中探针事件 | ✅ | §3.2 |
| K2 | RPC 抓到标签文本 + 保存调用 | ✅ | §3.2 |
| K3 | `resolve_by_label` α 直出 conv_local 数量对齐 | ⏸ Step 12 | §5.1（β 可用） |
| K4 | β 本地存储可手打反查 | ✅ | §5.2 + §3.3 |

---

## 9. 文件与脚本索引

| 类型 | 路径 |
| ---- | ---- |
| **日常查资料** | `runtime/wework_re_android/dump_contact_profile.py` |
| **按标签/名解析** | `runtime/wework_re_android/conv_resolver.py` |
| **枚举会话** | `runtime/wework_re_android/list_conversations_with_names.py` |
| **β 标签 CLI** | `runtime/wework_re_android/label_store_cli.py` |
| **β 存储库** | `app/broadcast/labels.py` |
| **α RPC 探针** | `runtime/wework_re_android/probe_customer_labels_rpc.py` |
| **Conversation 探针（已证伪）** | `runtime/wework_re_android/probe_customer_labels.py` |
| **标签名缓存** | `runtime/customer_label_names.json` |
| **β conv→标签映射** | `runtime/customer_labels.json` |
| **逆向笔记** | `runtime/wework_re_android/notes/customer_labels.md` |
| **11.6 验收报告** | `runtime/reports/step11_6_acceptance_report_20260916.json` |
| **联系人资料样例** | `runtime/reports/contact_profile_hainiao_20260916.json` |
| **α RPC 产物** | `runtime/wework_re_android/artifacts/customer_labels_rpc.ndjson` |

---

## 10. 变更记录

| 日期 | 变更 |
| ---- | ---- |
| 2026-09-16 | Step 11.6 验收闭环；`conv_resolver` α+β 合并；β CLI 与单测 |
| 2026-09-16 | 新增 `dump_contact_profile` 固定日常查资料流程；验证「海鸟与鱼」 |
| 2026-09-16 | 本文档首次建立，汇总 Step 11 逆向 + 日常运维全部固定流程 |
