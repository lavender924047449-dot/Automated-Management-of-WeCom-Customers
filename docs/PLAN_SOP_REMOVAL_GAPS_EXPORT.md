# 实施计划：SOP 移除 / 间隔定制化 / 素材包同步素材库 / 导出导入

**状态：** `100%`（S1-S9 全部完成 ✅）

## 需求总览

| # | 需求 | 简述 |
|---|------|------|
| 1 | 调度器未初始化 | exe 打包缺少 apscheduler，"具体时刻"发送失败 |
| 2 | 移除 SOP + 间隔迁移 | plan_composer 去掉 SOP 和全局间隔控件，间隔设定内嵌到素材序列 |
| 3 | 逐条消息间隔定制化 | 每条消息之间可单独设定间隔（≥3s），联系人间隔保留全局 |
| 4 | 素材包同步到素材库 | 已保存素材 Tab 中展示素材包行 |
| 5 | 一键导出/导入素材库 | zip 格式，含全部索引+媒体+素材包，支持冲突策略选择 |

---

## 1. 调度器未初始化（打包缺依赖）

### 根因

`app/broadcast/scheduler.py` 第 17 行：

```python
try:
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:
    BackgroundScheduler = None
```

PyInstaller 静态分析追不到 `try/except` 内的动态 import。打包后的 exe 里不含
`apscheduler`，运行时 `BackgroundScheduler is None`，`_build_scheduler_backend`
抛 RuntimeError，被 `_resolve_scheduler` 的 `except` 吞掉返回 `None`，弹出
"调度器不可用 / 当前调度器未初始化"。

### 修复

| 文件 | 改动 |
|------|------|
| `packaging/wecom-voice-blaster-gui.spec` | `hiddenimports` 加 `"apscheduler"` + `collect_submodules("apscheduler")` |

一行改动，风险极低。

---

## 2. 移除 SOP + 间隔迁移到素材序列

### 2.1 plan_composer 中移除的 UI 和逻辑

| 移除项 | 涉及代码 |
|--------|----------|
| "另存为 SOP" 按钮 (`_btn_save_sop`) | `plan_composer.py` |
| SOP 下拉 (`_sop_pick`) | `plan_composer.py` |
| "使用 SOP" 按钮 (`_btn_use_sop`) | `plan_composer.py` |
| `_save_as_sop` / `_use_sop` / `_reload_sops` / `_current_sop` | `plan_composer.py` |
| SOP 导入 (`from gui.widgets.sop_dialog import SopDialog`) | `plan_composer.py` |
| `from app.broadcast.sop_store import ...` | `plan_composer.py` |
| 全局 msg_gap slider+spinbox 4 组控件 | `plan_composer.py` |
| 全局 contact_gap slider+spinbox 4 组控件 | `plan_composer.py` |
| 语音扰动开关 (`_perturb` QCheckBox) | `plan_composer.py` |
| SOP 相关布局行 (`sop_row`) | `plan_composer.py` |

**注意**：`sop_store.py` 和 `sop_dialog.py` 文件暂保留（不删文件），但 plan_composer 不再 import 和使用。

### 2.2 plan_composer 新增的 UI

**「参数与调度」分组改版后只保留：**

1. **联系人间隔**（全局）：1 个 QSpinBox，最小值 3，默认 60s
2. **发送时间**（调度模式）：保持不变（立即/延迟/具体时刻/循环）

**素材序列列表改版：**

在序列列表的每两个步骤之间插入一个**间隔行**（视觉区分，灰色/斜体）：
- 每个间隔行包含 1 个 QSpinBox，最小值 3s，默认 10s
- 用户可直接在序列列表里编辑每条消息之间的间隔
- 添加新步骤时自动在前一步后面插入默认间隔行
- 删除步骤时自动合并相邻间隔行
- 拖拽排序时间隔行跟随其前面的素材步骤移动

**素材包原子节点：**
- 素材包作为原子节点时，显示为 1 行（如 `素材包：XXX（5 条）`）
- 双击可展开查看内部步骤和 gap（只读预览 or 可编辑 gap）
- 展开为多个步骤时，内部 `step_gaps` 也展开成序列间隔行

### 2.3 素材包模型变更

```python
# app/broadcast/asset_bundles.py
class AssetBundle(BaseModel):
    id: str
    name: str
    items: list[BundleItem]
    tags: list[str]
    step_gaps: list[float] = []      # 新增：长度 = max(0, len(items)-1)
    contact_gap: float = 60.0        # 新增：建议联系人间隔（全局）
    created_at: str
    updated_at: str
```

`step_gaps` 校验：
- 每个值 `>= 3.0`
- 长度必须 = `len(items) - 1`（保存时自动补齐或截断）
- 默认每条间隔 10.0s

`contact_gap` 校验：
- `>= 3.0`
- 默认 60.0s

### 2.4 BroadcastPlan 模型变更

```python
# app/broadcast/plan.py
class BroadcastPlan(BaseModel):
    contacts: list[PlanContact]
    sequence: list[SequenceStep]
    step_gaps: list[float] = []          # 替代 msg_gap_seconds
    contact_gap: float = 60.0            # 替代 contact_gap_seconds
    # perturb 字段删除
    # msg_gap_seconds 字段删除
    # contact_gap_seconds 字段删除
```

`step_gaps` 语义：`step_gaps[i]` = 发完 `sequence[i]` 后等多久再发 `sequence[i+1]`。
长度 = `len(sequence) - 1`。每个值 `max(3.0, val)`。

`contact_gap` 语义：发完一个联系人的所有消息后等多久再发下一个人。`max(3.0, val)`。

**向后兼容**：加 `model_validator(mode="before")` 把旧格式 `msg_gap_seconds` / `contact_gap_seconds` 转成新字段。`perturb` 旧数据里有的话忽略。

### 2.5 Runner 变更

```python
# app/broadcast/runner.py
```

| 改前 | 改后 |
|------|------|
| `_sleep_gap(self.plan.msg_gap_seconds)` | `_sleep_step_gap(step_index)` 读 `plan.step_gaps[i]` |
| `_sleep_gap(self.plan.contact_gap_seconds)` | `_sleep_contact_gap()` 读 `plan.contact_gap` |
| `random.uniform(lo, hi)` | 固定值 `max(3.0, gap)` |
| `perturb` 相关逻辑 | 删除（send_path 直接用 silk_path） |
| `_dispatch_bundle` 无间隔 | 读 bundle 的 `step_gaps`，在 items 之间 sleep |

---

## 3. 素材包编辑里的间隔 UI

### LibraryBundleTab 变更

**中间「当前素材包内容」列表**：
- 在素材条目之间显示间隔行（同 plan_composer 的设计）
- 每个间隔行包含 QSpinBox（最小 3s，默认 10s）
- 保存素材包时从间隔行收集 `step_gaps`

**BundleDetailDialog 变更**：
- 同样在条目之间显示间隔行
- 可编辑间隔值
- `result_bundle()` 返回时带上 `step_gaps`

---

## 4. 素材包同步到已保存素材

### LibrarySavedTab 变更

`reload_from_store` 新增加载素材包行：

```python
bundle_rows = self._load_bundle_rows(self._account_id)
merged = [*voice_rows, *text_rows, *oa_rows, *wxa_rows, *image_rows, *video_rows, *bundle_rows]
```

**bundle 行属性**：

| 字段 | 值 |
|------|----|
| `asset_type` | `"bundle"` |
| `item_id` | `bundle.id` |
| `name` | `bundle.name` |
| `source` | `"bundle"` |
| `usage_count` | 实际使用次数（从报告中统计） |
| `archived` | `False`（bundle 无归档概念） |

**操作**：
- 双击名字列 → 重命名（`save_bundle` 写回）
- 双击其他列 → 打开 `BundleDetailDialog`
- 右键删除 → `delete_bundle` + 通知 `LibraryBundleTab` 刷新
- 类型列显示「素材包」

### usage_count 统计

从 `ReportStore` 的历史报告中统计 `step_type == "bundle"` 且 `status == "sent"` 的次数，
按 `bundle_id` 聚合。加载时一次性扫描，缓存在内存。

---

## 5. 一键导出 / 导入素材库

### 导出

**UI**：「已保存的素材」Tab 工具栏新增「导出素材库」按钮。

**流程**：
1. 弹出文件保存对话框，用户自定义文件名，默认 `素材库导出_{日期时间}.zip`
2. 收集当前账号数据目录下的：
   - `voice_index.json` + 所有 silk 文件
   - `text_templates.json`
   - `broadcast/asset_bundles.json`
   - `oa/*.json`
   - `image/*.json` + 对应图片文件
   - `video/*.json` + 对应视频文件 + 缩略图
   - `wxa/*.json`
3. 打包为 zip，内部保持相对路径结构

### 导入

**UI**：「已保存的素材」Tab 工具栏新增「导入素材库」按钮。

**流程**：
1. 弹出文件选择对话框，选 `.zip`
2. 解压到临时目录，扫描内容
3. 逐文件检查是否与当前账号目录冲突（同文件名已存在）
4. 如有冲突，弹对话框：
   - 「覆盖已有」（仅当前冲突项）
   - 「跳过重复」（仅当前冲突项）
   - 「全部覆盖」
   - 「全部跳过」
5. 按用户选择写入
6. 完成后 `reload_from_store`

### 数据层

```python
# app/broadcast/library_export.py（新文件）

def export_library(account_id: str, dest_zip: Path) -> Path: ...
def import_library(account_id: str, src_zip: Path, conflict_cb: Callable) -> ImportResult: ...
```

---

## 执行顺序

| 步骤 | 内容 | 依赖 | 风险 |
|------|------|------|------|
| S1 | 修 apscheduler 打包缺失 | 无 | 极低 |
| S2 | AssetBundle 模型加 step_gaps / contact_gap | 无 | 低（Pydantic 默认值兼容旧数据） |
| S3 | BroadcastPlan 模型改 step_gaps / contact_gap + 向后兼容 | 无 | 中（需改测试） |
| S4 | Runner 改为逐条间隔 + bundle 内间隔 + 删 perturb | S3 | 中 |
| S5 | LibraryBundleTab 间隔 UI（中间列表 + detail dialog） | S2 | 中 |
| S6 | plan_composer 改版（移除 SOP/全局间隔/perturb，新增逐条间隔 UI） | S3 | 高（UI 重构量大） |
| S7 | LibrarySavedTab 展示素材包行 | S2 | 低 |
| S8 | 导出/导入素材库 | 无 | 中 |
| S9 | 测试 + 重新打包验证 | S1-S8 | — |

**依据**：S1 独立且一行改动，先做。S2/S3 是数据模型变更，所有 UI 和 runner 都依赖它。
S4 紧跟 S3（runner 是核心发送路径，改早验证早）。S5/S6 是 UI，在模型稳定后做。
S7/S8 相对独立，可并行或最后做。

---

## 文件改动清单

| 文件 | 改动类型 | 步骤 |
|------|----------|------|
| `packaging/wecom-voice-blaster-gui.spec` | 加 hiddenimport | S1 |
| `app/broadcast/asset_bundles.py` | 加 step_gaps / contact_gap 字段 | S2 |
| `app/broadcast/plan.py` | 改 step_gaps / contact_gap / 删 perturb / 向后兼容 | S3 |
| `app/broadcast/runner.py` | 逐条间隔 / bundle 内间隔 / 删 perturb | S4 |
| `gui/tabs/library_bundle_tab.py` | 间隔 UI + BundleDetailDialog 间隔 | S5 |
| `gui/tabs/plan_composer.py` | 大改：移除 SOP/全局间隔/perturb，新增逐条间隔 UI | S6 |
| `gui/tabs/library_saved_tab.py` | 加载 bundle 行 + 操作 | S7 |
| `gui/tabs/library_tab.py` | 串联 bundle 行信号 | S7 |
| `app/broadcast/library_export.py` | 新文件：导出/导入逻辑 | S8 |
| `gui/tabs/library_saved_tab.py` | 导出/导入按钮 | S8 |
| `app/broadcast/dto.py` | 可能需要给 AssetRowDTO 加 bundle 支持 | S7 |
| `tests/test_broadcast_runner.py` | 适配新模型 | S4 |
| `tests/test_plan_schema.py` | 适配新模型 | S3 |
| `tests/test_sop_store.py` | 可选：保留测试但标记 skip（SOP 暂不删文件） | S6 |

---

## 手动测试清单

### 1. 调度器

- [ ] 打包 exe 后，选「具体时刻」发送 → 不再弹"调度器未初始化"
- [ ] 选「N 时间后」/ 「循环」同样正常提交

### 2. 间隔定制化

- [ ] 素材包编辑：条目之间显示间隔设置，默认 10s，最小 3s
- [ ] plan_composer：序列中每两条之间显示间隔设置，最小 3s
- [ ] 双击素材包原子节点 → 可查看/编辑内部 gap
- [ ] 展开素材包时 gap 也展开到序列中
- [ ] 联系人间隔全局 SpinBox，最小 3s

### 3. Runner

- [ ] 实际发送时每条消息之间等待时间符合设定
- [ ] 联系人之间等待时间符合设定
- [ ] 素材包原子节点内部条目之间按 step_gaps 等待
- [ ] 间隔 < 3s 时 runner 兜底为 3s

### 4. 素材包同步到素材库

- [ ] 已保存素材 Tab 里出现素材包行，类型列显示「素材包」
- [ ] 双击名字列可重命名
- [ ] 双击其他列打开包详情
- [ ] 右键删除 → 包从素材库和制作素材包 Tab 同步消失
- [ ] 使用次数如实显示

### 5. 导出/导入

- [ ] 点「导出素材库」→ 生成 zip，内含全部索引 + 媒体文件
- [ ] 在另一台电脑/另一个账号点「导入素材库」→ 选 zip → 素材出现
- [ ] 导入时遇到同名文件 → 弹对话框选覆盖/跳过
- [ ] 导入后 step_gaps / contact_gap 正确恢复

### 6. 回归

- [ ] SOP 按钮和下拉已从 plan_composer 消失
- [ ] 语音扰动开关已消失
- [ ] 旧格式 plan JSON（含 msg_gap_seconds / contact_gap_seconds / perturb）仍能加载
- [ ] 文字/图片/视频/语音/公众号发送与改之前一致
