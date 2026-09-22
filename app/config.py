"""
配置系统 — 基于 pydantic 强类型模型 + YAML 加载。

用法：
    from app.config import load_config
    cfg = load_config()                        # 默认 config.yaml
    cfg = load_config(Path("custom.yaml"))
    print(cfg.audio.sample_rate)
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


# ---------------- 项目根目录 ---------------- #

def is_frozen() -> bool:
    """PyInstaller / 类似打包器冻结后为 True。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """
    可写的应用根目录。

    - 开发态: 仓库根 (app/ 的上一级)
    - 打包态: exe 所在目录 (不是 _internal / _MEIPASS)
      这样 config.yaml、logs、runtime、samples 都落在用户能看见的文件夹里。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_root() -> Path:
    """
    只读的打包资源根。

    PyInstaller 6 onedir 默认把 datas 放到 exe 旁的 `_internal/`
    (即 sys._MEIPASS)。开发态与 project_root() 相同。
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return project_root()


def resolve_path(p: str | Path) -> Path:
    """
    把相对路径解析到应用根；绝对路径原样返回。

    打包后查找顺序：exe 目录 → _MEIPASS。
    这样 vendor/ffmpeg 可以留在 _internal，而 logs/runtime 写到 exe 旁。
    若两边都不存在，返回 exe 目录下的目标路径（便于首次创建文件）。
    """
    p = Path(p)
    if p.is_absolute():
        return p
    writable = project_root() / p
    if writable.exists():
        return writable
    bundled = bundled_root() / p
    if bundled.exists():
        return bundled
    return writable


_RUNTIME_DIRS = (
    "logs",
    "runtime",
    "runtime/prepared",
    "runtime/reports",
    "runtime/silk_cache",
)
_COPY_IF_MISSING = ("config.yaml", "contacts.example.csv")


def ensure_runtime_layout() -> Path:
    """
    首次启动时把可写目录和默认配置落到 exe 旁。

    打包态会从 _MEIPASS 复制缺失的 config.yaml / samples/。
    开发态只保证 logs/runtime 目录存在，不覆盖仓库文件。
    """
    root = project_root()
    bundled = bundled_root()
    for rel in _RUNTIME_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)

    if root.resolve() != bundled.resolve():
        for name in _COPY_IF_MISSING:
            dst = root / name
            src = bundled / name
            if not dst.exists() and src.exists():
                shutil.copy2(src, dst)
        samples_src = bundled / "samples"
        samples_dst = root / "samples"
        if samples_src.is_dir() and not samples_dst.exists():
            shutil.copytree(samples_src, samples_dst)
        testdata_src = bundled / "testdata"
        testdata_dst = root / "testdata"
        if testdata_src.is_dir() and not testdata_dst.exists():
            shutil.copytree(testdata_src, testdata_dst)

    try:
        os.chdir(root)
    except OSError:
        pass
    return root


# ---------------- 子配置模型 ---------------- #

class AudioConfig(BaseModel):
    sample_rate: int = 48000
    channels: int = 1
    bit_depth: int = 16
    loudnorm_i: float = -16.0
    loudnorm_tp: float = -1.5
    loudnorm_lra: float = 11.0
    head_padding_ms: int = 150
    tail_padding_ms: int = 200

    @field_validator("channels")
    @classmethod
    def _mono_or_stereo(cls, v: int) -> int:
        if v not in (1, 2):
            raise ValueError("channels 只支持 1 (mono) 或 2 (stereo)")
        return v


class FFmpegConfig(BaseModel):
    bundled_path: str = "vendor/ffmpeg/ffmpeg.exe"

    def resolved_bundled(self) -> Path:
        return resolve_path(self.bundled_path)


class DeviceConfig(BaseModel):
    adb_port: int = 16384
    connect_timeout: float = 5.0
    wecom_package: str = "com.tencent.wework"


class LocatorsConfig(BaseModel):
    mic_toggle: str
    hold_button: str
    input_edit: str
    home_msg_tab_text: str = "消息"
    #: Stage 4.5.2 新增: 文本输入模式下的"发送"按钮 resource-id (可选).
    #: 留空时 Navigator 会用多组文本候选自动查找并在缺失时 dump UI.
    send_button: str | None = None
    #: Stage 4.5.3 新增: 附件面板 "+" 按钮 (可选)
    plus_button: str | None = None
    #: Stage 4.5.8 新增: 输入区 emoji 表情按钮 (可选; 缺失时靠 description 候选)
    emoji_button: str | None = None


class TimingConfig(BaseModel):
    pre_roll_ms: int = 150
    post_roll_ms: int = 300
    after_send_wait_s: float = 1.5


class ScheduleConfig(BaseModel):
    min_interval_s: float = 8
    max_interval_s: float = 25
    hourly_limit: int = 50
    daily_limit: int = 250
    working_hours: tuple[int, int] = (9, 20)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    dir: str = "logs"
    keep_days: int = 14

    def resolved_dir(self) -> Path:
        return resolve_path(self.dir)


class PCWeComConfig(BaseModel):
    wxwork_root: str = str(Path.home() / "Documents" / "WXWork")
    account_map_file: str = "runtime/account_map.json"
    contact_index_file: str = "runtime/contact_index.json"
    send_queue_file: str = "runtime/send_queue.json"
    send_history_file: str = "runtime/send_history.json"
    conv_map_file: str = "runtime/conv_map.json"
    # Native hijack：有 Frida + 企微进程时自动启用；失败则回退 UIA 搜人
    native_hijack: bool = True
    decoy_target: str = ""
    decoy_conv_id: str = ""
    vtable_offset: str = "0xabbb210"  # 企微 5.0.10.6015；升版需重逆向

    @field_validator("vtable_offset", mode="before")
    @classmethod
    def _vtable_as_str(cls, v: object) -> str:
        if isinstance(v, int):
            return hex(v)
        return str(v)


# ---------------- 顶层配置 ---------------- #

class AppConfig(BaseModel):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    ffmpeg: FFmpegConfig = Field(default_factory=FFmpegConfig)
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    locators: LocatorsConfig
    timing: TimingConfig = Field(default_factory=TimingConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    pc_wecom: PCWeComConfig = Field(default_factory=PCWeComConfig)


# ---------------- 加载器 ---------------- #

DEFAULT_CONFIG_FILE = "config.yaml"


def load_config(path: Path | str | None = None) -> AppConfig:
    """加载并校验配置。找不到文件抛 FileNotFoundError。"""
    if path is None:
        path = project_root() / DEFAULT_CONFIG_FILE
        if not Path(path).exists():
            bundled = bundled_root() / DEFAULT_CONFIG_FILE
            if bundled.exists():
                path = bundled
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """全局单例，方便随处 import 使用"""
    return load_config()
