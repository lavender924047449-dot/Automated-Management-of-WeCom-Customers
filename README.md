# 企微客户消息工具

用安卓模拟器里的企业微信发消息。产品入口是 `python -m gui.main`。

电脑版企业微信「文件传输助手长按转发」那一代已经不在本仓库。可运行代码在 [PC_hook_repo 的 pc_cli_generation](https://github.com/lavender924047449-dot/PC_hook_repo/tree/main/pc_cli_generation)。

## 主链

```
gui/main.py
    图形界面：联系人、素材库、编辑和发送、执行结果
        │
app/broadcast
    计划、调度、素材库、发送编排
        │
runtime/wework_re_android
    send_engine 发送，fta_capture_lite 采集，Frida 会话
        │
模拟器里的企业微信
    adb（默认 127.0.0.1:16384）+ frida-server
```

界面不直接操作 Frida。发送和采集由 `app/broadcast` 拉起 `runtime/wework_re_android` 里的模块。

## 目录

```
gui/                         # python -m gui.main
app/broadcast/               # 计划、账号、素材、调度、发送
app/bootstrap/               # adb / frida-server / 打包态子进程
app/security/                # DPAPI 加密存储
runtime/wework_re_android/   # 模拟器侧发送与采集脚本
packaging/                   # PyInstaller
```

## 启动

- Windows 10 / 11
- Python 3.10+
- MuMu 模拟器，企业微信已登录，adb 端口默认 `16384`

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m gui.main
```

打包：

```powershell
python packaging\build.py
```

产物是 `dist\企微客户自动化管理工具.exe`，旁边需要 `vendor\` 和 `使用说明.txt`。

## 测试

```powershell
python -m pytest
```

## Frida 适配脚本

- 本地清单：`runtime/wework_re_android/adapter_manifest.json`
- 本地 hooks：`runtime/wework_re_android/send_engine_hooks.js`
- 启动时会尝试拉取远端适配，可用 `--no-auto-adapter` 关闭

## 许可

内部使用。请遵守企业微信的使用条款，以及目标客户的知情同意要求。
