# wecom-voice-blaster

**企业微信素材转发工具（PC 企微 FTA 主链路）**。

> 当前主链路：PC 企业微信文件传输助手（FTA）接收素材 → 自动编码 → 长按转发到目标对象。
> Android 模拟器与 VB-CABLE 方案已进入兼容归档，仅供历史版本回退。

---

## 项目概览

一句话：把素材先发到 PC 企业微信的文件传输助手，程序自动产出素材编码并建立锚点，再按待发送清单一键转发给目标对象。

### 为什么这么做

- 支持图片、视频、文件、语音、小程序、视频号、位置、名片等素材的原生转发形态。
- 编码格式统一为 `<类型前缀>-<12位哈希>`，同素材跨账号保持一致。
- 运行链路轻量，不再依赖 Android 模拟器与虚拟麦克风。

### 主体架构

```
┌──────────────────────────┐
│  GUI (PySide6, Stage 5)  │  ← Plan 浏览 / 编辑 / 执行监控 / 素材库
└─────────────┬────────────┘
              │
┌─────────────▼────────────┐
│  PlanRunner (Stage 4.5.9)│  ← 分发 + 调度 + 严格/宽松 + resume + report
└─────────────┬────────────┘
              │
┌─────────────▼────────────┐
│  SenderRegistry          │
│    · Direct 类 sender    │  ← text/image/video/file/voice/contact_card/sticker
│    · Forward 类 sender   │  ← miniprogram/channel_video/location（走 FTA 转发）
└─────────────┬────────────┘
              │
┌─────────────▼────────────┐
│  WeComNavigator          │  ← 基于 uiautomator2 的 UI 操作库
│  AndroidFileStore        │  ← push 媒体到设备
│  AudioPreprocessor       │  ← ffmpeg loudnorm → VB-CABLE 播放
└──────────────────────────┘
```

---

## 目录结构

```
Test-Voice/
├── main.py                   # CLI 入口 (子命令: check/send/batch/plan/plan-run/gui/normalize/show-config)
├── config.yaml               # 主配置
├── pyproject.toml
├── requirements.txt
├── contacts.example.csv      # batch 子命令示例
├── app/
│   ├── audio/                # 预处理 / silk 解码 / VB-CABLE 播放
│   ├── automation/           # WeComNavigator (企微 UI 编排)
│   ├── device/               # AdbSession / AndroidFileStore
│   ├── messaging/            # BatchPlan / 各 Sender / PlanRunner / AssetLibrary
│   ├── orchestrator/         # 老的 CSV 批处理 (Stage 4)
│   ├── gui/                  # PySide6 图形界面 (Stage 5.x)
│   └── util/                 # loguru 配置等
├── samples/                  # 各类型的示例 plan.json
├── scripts/                  # 一次性辅助脚本 (fetch_ffmpeg 等)
├── tests/                    # pytest 用例 (271 条, headless)
├── vendor/                   # ffmpeg / silk 二进制 (不入库)
├── spikes/                   # 历史 spike 脚本 (仅归档, 详见 spikes/README.md)
├── logs/                     # loguru 落盘 (gitignored)
└── runtime/                  # prepared 音频 / 报告 (gitignored)
```

---

## 快速开始

### 1. 环境

- Windows 10 / 11
- Python **3.10+**
- MuMu Pro 12（默认 ADB 端口 `16384`）
- VB-CABLE 虚拟音频线（Windows 默认输入设备切到 CABLE Output）
- FFmpeg（放到 `vendor/ffmpeg/ffmpeg.exe`，或系统 PATH 里有）

> **PowerShell 中文乱码**：若 `python main.py --help` 或 pytest 输出的中文显示为
> `步�?` 之类，请在会话里执行一次
> `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`
> （Python 端已经在 UTF-8 输出，问题在 PowerShell 读管道的默认编码）。

### 2. 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python scripts\fetch_ffmpeg.py    # 可选: 自动下载并放到 vendor/ffmpeg/
```

### 3. 自检

```powershell
python main.py check              # ffmpeg + 配置 + 日志目录
python main.py show-config        # 打印实际生效的配置
```

---

## CLI 用法

所有子命令都接受 `--config <yaml>`（默认 `./config.yaml`）。

| 子命令 | 用途 | 常用示例 |
|--------|------|----------|
| `check` | ffmpeg / 配置 / 日志目录自检 | `python main.py check` |
| `show-config` | 打印生效的配置（含默认值） | `python main.py show-config` |
| `normalize` | 用 ffmpeg 把音频转成企微友好的 WAV | `python main.py normalize test.wav` |
| `send` | 单次发送一条消息（走 VoiceSender） | `python main.py send "测试群" test.wav` |
| `batch` | 老 CSV 批发（Stage 4，保留兼容） | `python main.py batch contacts.example.csv --dry-run` |
| `plan` | 加载 BatchPlan JSON 并**干跑**（stub, 不连设备） | `python main.py plan samples\plan_text_smoke.json` |
| `plan-run` | **真正执行** BatchPlan（Stage 4.5.9 完整引擎） | `python main.py plan-run samples\plan_text_smoke.json --dry-run` |
| `scan-cache` | 扫 PC 企微本地缓存并自动落入 AssetLibrary | `python main.py scan-cache --list` / `python main.py scan-cache --timeout 60` |
| `gui` | 启动 PySide6 图形界面 | `python main.py gui samples\plan_text_smoke.json` |

### `plan-run` 常用开关

| flag | 作用 |
|------|------|
| `--dry-run` | 不连设备、不真发；用于验证 plan 合法性 |
| `--limit-contacts N` | 每个 Task 只跑前 N 个联系人 (0 = 全部) |
| `--no-jitter` | 类型化间隔固定取 `min`, 不随机抖动 |
| `--keep-remote` | 不清理 push 到设备的媒体文件（调试用） |
| `--refresh-forward-assets` | Forward 阶段前先把所有 tag 转发给 FTA 自己保鲜 |
| `--loose` | 宽松模式：单步失败继续（默认严格，首失败即停） |
| `--resume` | 从上次进度恢复（跳过已 SENT 的 step） |
| `--ignore-schedule` | 跳过工作时段/配额门控（`--dry-run` 默认已跳过） |
| `--report-dir DIR` | 报告目录（默认 `runtime/reports`） |

### BatchPlan JSON 是什么样

见 `samples/` 下的示例，最小示例：

```json
{
  "meta": { "name": "hello", "note": "smoke" },
  "tasks": [
    {
      "label": "打招呼",
      "contacts": ["文件传输助手"],
      "messages": [
        { "type": "text", "text": "你好呀" },
        { "type": "image", "path": "runtime/hi.png" }
      ]
    }
  ]
}
```

- `type` 可选：`text` / `image` / `video` / `file` / `voice` / `contact_card` /
  `sticker` / `miniprogram` / `channel_video` / `location`
- 转发类（后 4 种）走 tag，从 **素材库** (`runtime/asset_library.json`) 里解析定位。

---

## GUI 说明（Stage 5.1–5.6）

唯一 GUI 入口：`python -m gui.main`。

```powershell
python -m gui.main
# 或直接指定 plan
python -m gui.main --voice-index-path runtime\voice_lib_index.json
```

五个主面板（左侧列表切换）：

| 面板 | Stage | 能做什么 |
|------|-------|----------|
| **Plan 浏览** | 5.1 | 树形展示 plan 结构、消息类型、抽签等待时长 |
| **Plan 编辑** | 5.2 | JSON 直编 + 可视化表单（任务/消息增删改移）+ 素材 tag 插入 |
| **执行监控** | 5.3 | 一键跑 `plan-run`（后台线程），进度条 + 实时日志 + 报告目录快捷键 |
| **素材库** | 5.4 | 新增 / 编辑 / 删除 AssetEntry（本地素材 + FTA 转发素材），导出可见 tag |
| **设置** | 5.6 | 编辑 `config.yaml` 常用运行时开关（设备 / 节流 / 时序 / 音频 / 日志） |

菜单：`文件 → 打开 Plan` (Ctrl+O)、`文件 → 重新加载` (F5)、`帮助 → 关于`。

---

## 打包分发（Stage 5.5）

给"不会命令行"的运营同学交付时，可打成独立目录 exe（含所有依赖 + 资源）：

```powershell
pip install pyinstaller>=6.3.0

# 同时打 CLI 与 GUI 两份产物
python packaging\build.py

# 只打其中一份
python packaging\build.py --gui-only
python packaging\build.py --cli-only

# 先清 build/ dist/ 再打
python packaging\build.py --clean
```

产物：

```
dist\wecom-voice-blaster\wecom-voice-blaster.exe            (CLI, 有控制台)
dist\wecom-voice-blaster-gui\wecom-voice-blaster-gui.exe    (GUI, 无控制台)
```

打包会自动带上 `config.yaml` / `samples/` / `vendor/`（含 ffmpeg 与 silk 解码器）
/ `contacts.example.csv`。整个 `dist\wecom-voice-blaster-gui\` 目录发给客户即可双击运行。

### 也可以 pip 安装为命令

```powershell
pip install -e .
wecom-voice-blaster --help          # 等价于 python main.py ...
wecom-voice-blaster-gui              # 直接开 GUI (无控制台)
```

---

## 开发指引

### 运行测试

```powershell
python -m pytest                  # 全量 271 条, ~70 秒
python -m pytest -k asset_library # 只跑素材库相关
python -m pytest --tb=long -x     # 首失败即停
```

GUI 测试基于 `pytest-qt`，headless 环境也能跑。

### 代码规范

`pyproject.toml` 里配置了 ruff (E/F/W/I/UP/B, `line-length=100`, 忽略 E501)。
建议提交前跑：

```powershell
ruff check .
ruff format .
```

### 日志

- 落盘目录：`logs/app_YYYY-MM-DD.log`，`keep_days: 14`（config.yaml 可改）
- 级别：默认 INFO；改成 DEBUG 会打印 uiautomator2 的每一步 UI 操作

### 常见配置调整（`config.yaml`）

- `device.adb_port` — MuMu Pro 是 16384，其它模拟器需要改
- `locators.*` — 企微 APP 版本升级后控件 id 可能变，改这里就够
- `schedule.working_hours / hourly_limit / daily_limit` — 反风控节流
- `audio.loudnorm_i` — 语音气泡响度（LUFS）

---

## 阶段完成情况

| Stage | 内容 | 状态 |
|-------|------|------|
| 1     | 配置层 + 音频预处理 + ffmpeg 装配 | ✅ |
| 2–3   | Android UI 自动化 + 单发 | ✅ |
| 4     | CSV 批发 + 调度 | ✅ |
| 4.5.1 | BatchPlan 模型 + stub 干跑 | ✅ |
| 4.5.2–4.5.8 | 全 10 种 sender 落地 | ✅ |
| 4.5.5.x | AssetLibrary + FTA 转发路径 + 缓存扫描 | ✅ |
| 4.5.9 | PlanRunner（严格/宽松/resume/report） | ✅ |
| 5.1   | GUI · Plan 浏览 | ✅ |
| 5.2   | GUI · Plan 编辑 | ✅ |
| 5.3   | GUI · 执行监控 | ✅ |
| 5.4   | GUI · 素材库管理 | ✅ |
| 5.5   | 执行监控增强 (日志清空/导出/等宽/最新报告) + pyinstaller 打包脚本 + `pip install -e .` entry points | ✅ |
| 5.6   | GUI · Settings 面板（编辑 `config.yaml` 常用开关，locators 仍走 YAML） | ✅ |

---

## 已知遗留 / 待完善

- **`main.py batch`** 是 Stage 4 老 CSV 路径，已挂 `logger.warning` 弃用提示；主流程请用 `plan-run`。
- **Settings 不覆盖** `locators.*` / `ffmpeg.bundled_path` 等低改动高风险字段，仍需手改 YAML。
- **`spikes/SPIKE1_MANUAL_GUIDE.md`**：历史文档，源文件有编码遗留问题，非关键路径仅作参考。

## Frida 适配脚本更新通道

- 本地默认清单：`runtime/wework_re_android/adapter_manifest.json`
- 本地 hooks 脚本：`runtime/wework_re_android/send_engine_hooks.js`
- 启动时会尝试拉取远端适配（可用 `--no-auto-adapter` 关闭）
- 远端目录约定：
  - `https://github.com/<org>/Test-Voice/raw/main/dist/adapter/<wework_version>/manifest.json`
  - `https://github.com/<org>/Test-Voice/raw/main/dist/adapter/<wework_version>/send_engine_hooks.js`

---

## 许可与责任

内部使用工具，不对外分发。请遵守企业微信的使用条款与目标客户的知情/同意要求。
