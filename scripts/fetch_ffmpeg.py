"""
下载 ffmpeg.exe 到 vendor/ffmpeg/

来源：https://www.gyan.dev/ffmpeg/builds/  (essentials 版, 官方推荐)

用法：
    python scripts/fetch_ffmpeg.py
"""

from __future__ import annotations

import io
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "ffmpeg"


def _pretty(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def download(url: str) -> bytes:
    print(f"→ 下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        buf = io.BytesIO()
        got = 0
        last = time.perf_counter()
        chunk = 1024 * 64
        while True:
            b = resp.read(chunk)
            if not b:
                break
            buf.write(b)
            got += len(b)
            now = time.perf_counter()
            if now - last > 0.2 or got == total:
                pct = f"{100 * got / total:.1f}%" if total else "?"
                print(f"\r  {_pretty(got)} / {_pretty(total or got)}  ({pct})",
                      end="", flush=True)
                last = now
        print()
    return buf.getvalue()


def extract(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        target = next((n for n in names if n.endswith("bin/ffmpeg.exe")), None)
        if not target:
            raise RuntimeError("zip 里未找到 bin/ffmpeg.exe")
        print(f"→ 解压 {target}")
        with zf.open(target) as src, open(dest / "ffmpeg.exe", "wb") as out:
            out.write(src.read())
    print(f"✓ 已保存: {dest / 'ffmpeg.exe'}")


def main() -> int:
    exe = DEST / "ffmpeg.exe"
    if exe.exists():
        size = exe.stat().st_size
        print(f"✓ 已存在: {exe}  ({_pretty(size)})")
        return 0
    try:
        data = download(URL)
        extract(data, DEST)
        return 0
    except Exception as e:
        print(f"\n✗ 下载失败: {e}")
        print(f"\n手动下载指引：")
        print(f"  1) 打开 {URL}")
        print(f"  2) 解压后把 bin/ffmpeg.exe 放到 {DEST}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
