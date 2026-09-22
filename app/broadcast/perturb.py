"""Step 4.2: silk 尾部扰动。

实现策略：
1) 解析 Tencent/WeChat 常见 silk 容器（`#!SILK_V3` + 帧长度前缀）。
2) 在尾部窗口内选择“最短 payload 帧”作为近似静音帧。
3) 随机追加 1-3 个该帧，得到 hash 不同但听感变化极小的副本。
"""

from __future__ import annotations

import secrets
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import resolve_path

SILK_MAGIC = b"#!SILK_V3"


@dataclass(frozen=True)
class SilkFrame:
    """一个 silk 帧（带长度前缀）"""

    size: int
    payload: bytes

    def to_bytes(self) -> bytes:
        return self.size.to_bytes(2, "little") + self.payload


@dataclass(frozen=True)
class PerturbResult:
    source: Path
    output: Path
    appended_frames: int
    frame_size: int


def _find_frame_start(raw: bytes) -> int:
    pos = raw.find(SILK_MAGIC)
    if pos < 0:
        raise ValueError("非 silk_v3 文件：未找到 #!SILK_V3 头")
    start = pos + len(SILK_MAGIC)
    while start < len(raw) and raw[start] in (0x00, 0x0A, 0x0D):
        start += 1
    return start


def parse_silk_frames(raw: bytes) -> tuple[bytes, list[SilkFrame]]:
    """解析 silk 容器，返回 `header_prefix` 与可识别帧列表。"""
    frame_start = _find_frame_start(raw)
    header_prefix = raw[:frame_start]
    frames: list[SilkFrame] = []
    cursor = frame_start

    while cursor + 2 <= len(raw):
        size = int.from_bytes(raw[cursor : cursor + 2], "little")
        cursor += 2
        if size <= 0 or cursor + size > len(raw):
            break
        payload = raw[cursor : cursor + size]
        cursor += size
        frames.append(SilkFrame(size=size, payload=payload))

    if not frames:
        raise ValueError("silk 解析失败：未识别到有效帧")
    return header_prefix, frames


def _pick_tail_quiet_frame(frames: list[SilkFrame], tail_window: int = 20) -> SilkFrame:
    window = frames[-min(len(frames), max(1, tail_window)) :]
    return min(window, key=lambda f: f.size)


def perturb_silk_bytes(
    raw: bytes,
    *,
    min_append_frames: int = 1,
    max_append_frames: int = 3,
) -> tuple[bytes, int, int]:
    if min_append_frames < 1 or max_append_frames < min_append_frames:
        raise ValueError("append_frames 参数非法")

    header_prefix, frames = parse_silk_frames(raw)
    candidate = _pick_tail_quiet_frame(frames)
    append_count = min_append_frames + secrets.randbelow(max_append_frames - min_append_frames + 1)

    out = bytearray()
    out.extend(header_prefix)
    for frame in frames:
        out.extend(frame.to_bytes())
    for _ in range(append_count):
        out.extend(candidate.to_bytes())
    return bytes(out), append_count, candidate.size


def create_perturbed_copy(
    source_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    min_append_frames: int = 1,
    max_append_frames: int = 3,
) -> PerturbResult:
    source = resolve_path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"silk 不存在: {source}")

    raw = source.read_bytes()
    mutated, appended, frame_size = perturb_silk_bytes(
        raw,
        min_append_frames=min_append_frames,
        max_append_frames=max_append_frames,
    )

    if output_dir is None:
        dst_dir = source.parent
    else:
        dst_dir = resolve_path(output_dir)
        dst_dir.mkdir(parents=True, exist_ok=True)
    dst_name = f"{source.stem}_tmp_{uuid.uuid4().hex[:8]}{source.suffix or '.silk'}"
    output = dst_dir / dst_name
    output.write_bytes(mutated)
    return PerturbResult(
        source=source,
        output=output,
        appended_frames=appended,
        frame_size=frame_size,
    )


def clone_without_perturb(source_path: str | Path, output_path: str | Path) -> Path:
    """调试/回退辅助：复制文件且不做扰动。"""
    src = resolve_path(source_path)
    dst = resolve_path(output_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst

