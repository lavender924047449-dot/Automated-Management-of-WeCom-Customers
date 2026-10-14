"""生成目录分发的增量更新包。

用法（仓库根目录）::

    python packaging/make_program_update.py

产物是 ``dist/program-update.zip``。上传到 GitHub Release 时，附件名必须仍是
``program-update.zip``。压缩包里只有「程序」和 ``pyproject.toml``。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.updater.apply_update import UpdateError, validate_update_archive  # noqa: E402
from app.updater.pack_update import PackError, build_program_update_zip, default_update_zip_path  # noqa: E402


def main() -> int:
    dest = default_update_zip_path(ROOT)
    try:
        version = build_program_update_zip(ROOT, dest)
        validate_update_archive(dest, expected_version=version)
    except (PackError, UpdateError, OSError) as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 1
    print(f"已生成 {dest}")
    print(f"版本 {version}")
    print("上传到 GitHub Release 时，附件名必须是 program-update.zip。")
    print("这个包只覆盖用户安装目录里的「程序」和 pyproject.toml。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
