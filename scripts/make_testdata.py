"""
生成闭环测试材料: 中文口播 WAV + 测试图 + 报价文件.

用法 (项目根):
    .\\.venv\\Scripts\\python.exe scripts\\make_testdata.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "testdata" / "materials"
FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"

VOICE_CLIPS = [
    {
        "stem": "voice_01_greeting",
        "title": "问候",
        "text": (
            "你好，我是企微批量发送测试。"
            "这是第一条语音问候，请确认你能听清每一个字。"
        ),
    },
    {
        "stem": "voice_02_promo",
        "title": "活动说明",
        "text": (
            "本周活动开始了。我刚发了图片和链接，点开就能看详情。"
            "有问题直接回我这条消息就可以。"
        ),
    },
    {
        "stem": "voice_03_followup",
        "title": "跟进",
        "text": "资料都在上面了，方便的时候回我一个收到就可以。谢谢。",
    },
]


def _tts_wav(text: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix(".sapi.wav")
    ps1 = dest.with_suffix(".tts.ps1")
    # 慧慧 zh-CN；用 UTF-8 BOM 避免 PowerShell 中文乱码
    script = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{ $s.SelectVoice('Microsoft Huihui Desktop') }} catch {{}}
$s.Rate = -1
$s.Volume = 100
$s.SetOutputToWaveFile('{raw.as_posix()}')
$s.Speak(@'
{text}
'@)
$s.Dispose()
"""
    ps1.write_text(script, encoding="utf-8-sig")
    subprocess.check_call(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps1),
        ]
    )
    ff = FFMPEG if FFMPEG.is_file() else Path("ffmpeg")
    cmd = [
        str(ff),
        "-y",
        "-i",
        str(raw),
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11,adelay=150,apad=pad_dur=0.2",
        str(dest),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raw.unlink(missing_ok=True)
    ps1.unlink(missing_ok=True)


def _make_image() -> Path:
    path = OUT / "image_promo_card.png"
    img = Image.new("RGB", (960, 540), (24, 64, 120))
    draw = ImageDraw.Draw(img)
    font_path = Path(r"C:\Windows\Fonts\msyh.ttc")
    title_font = ImageFont.truetype(str(font_path), 48) if font_path.is_file() else ImageFont.load_default()
    body_font = ImageFont.truetype(str(font_path), 28) if font_path.is_file() else ImageFont.load_default()
    draw.rectangle((40, 40, 920, 500), outline=(255, 210, 80), width=6)
    draw.text((80, 90), "企微批量发送 · 测试卡片", fill=(255, 255, 255), font=title_font)
    draw.text((80, 200), "编号: E2E-IMAGE-001", fill=(255, 210, 80), font=body_font)
    draw.text((80, 260), "用途: 图片发送闭环验收", fill=(230, 240, 255), font=body_font)
    draw.text((80, 320), "看到这张黄框蓝底图，即表示图片发送成功。", fill=(230, 240, 255), font=body_font)
    draw.text((80, 400), "请勿发给真实客户。", fill=(255, 180, 180), font=body_font)
    img.save(path)
    return path


def _make_quote_file() -> Path:
    path = OUT / "报价单_闭环测试.txt"
    path.write_text(
        "报价单 · 闭环测试（假数据，禁止发给真实客户）\n"
        "================================\n"
        "单号: QT-E2E-20260908\n"
        "客户: 文件传输助手\n"
        "项目          数量    单价\n"
        "语音测试包      1     0.00\n"
        "图片测试卡      1     0.00\n"
        "合计: 0.00\n"
        "备注: 若企微里能打开这份 TXT，文件发送即通过。\n",
        encoding="utf-8",
    )
    return path


def _write_scripts_index() -> Path:
    lines = ["闭环测试 · 语音口播对照表", "=" * 32, ""]
    for clip in VOICE_CLIPS:
        lines.append(f"文件: {clip['stem']}.wav")
        lines.append(f"标题: {clip['title']}")
        lines.append(f"口播: {clip['text']}")
        lines.append("")
    path = OUT / "voice_scripts.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {OUT}")
    for clip in VOICE_CLIPS:
        dest = OUT / f"{clip['stem']}.wav"
        print(f"  TTS {dest.name} …")
        _tts_wav(clip["text"], dest)
        print(f"    {dest.stat().st_size} bytes")
    print(f"  图片 {_make_image().name}")
    print(f"  文件 {_make_quote_file().name}")
    print(f"  口播 {_write_scripts_index().name}")
    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
