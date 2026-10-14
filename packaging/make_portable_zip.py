"""把 dist-portable 打成中文 Windows 资源管理器能正确解压的 zip。

资源管理器不认 zip 的 UTF-8 文件名标记，中文名要按系统编码 GBK 写入。
``tar -a`` 会把「程序」「启动.exe」「使用说明.txt」写成乱码。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "dist-portable"
ARCHIVE_NAME = "wecom-voice-portable.zip"
_SKIP_TOP = {"logs", "runtime"}
_SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", "artifacts"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


def _encode_filename_gbk(self: zipfile.ZipInfo) -> tuple[bytes, int]:
    try:
        return self.filename.encode("ascii"), self.flag_bits
    except UnicodeEncodeError:
        return self.filename.encode("gbk"), self.flag_bits & ~0x800


def _install_gbk_filenames() -> None:
    zipfile.ZipInfo._encodeFilenameFlags = _encode_filename_gbk  # type: ignore[method-assign]


def _include(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in _SKIP_TOP:
        return False
    if parts[0] == "data" and len(parts) > 1:
        return False
    if any(part in _SKIP_DIR_NAMES for part in parts):
        return False
    return relative.suffix.lower() not in _SKIP_SUFFIXES


def iter_source_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if _include(relative):
            files.append(path)
    return files


def build_portable_zip(source: Path, dest_zip: Path) -> int:
    if not (source / "python" / "pythonw.exe").is_file():
        raise SystemExit(f"缺少 {source / 'python' / 'pythonw.exe'}")
    if not (source / "程序").is_dir():
        raise SystemExit(f"缺少 {source / '程序'}")
    if not (source / "启动.exe").is_file():
        raise SystemExit(f"缺少 {source / '启动.exe'}")

    _install_gbk_filenames()
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()

    files = iter_source_files(source)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("dist-portable/data/", "")
        for path in files:
            relative = path.relative_to(source).as_posix()
            archive.write(path, f"dist-portable/{relative}")
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 dist-portable，中文文件名使用 GBK")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / ARCHIVE_NAME)
    args = parser.parse_args(argv)
    count = build_portable_zip(args.source.resolve(), args.output.resolve())
    print(f"已生成 {args.output}")
    print(f"文件数 {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
