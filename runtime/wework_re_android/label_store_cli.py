"""Step 11 β: 本地客户标签映射 CLI。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.broadcast.labels import LocalLabelStore


def _parse_labels(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="本地客户标签映射管理")
    p.add_argument(
        "--path",
        default="",
        help="labels 文件路径（默认 data/<account_id>/contacts_cache/customer_labels.json）",
    )
    p.add_argument("--account-id", default="", help="账号命名空间（默认读取环境变量中的当前账号）")

    sub = p.add_subparsers(dest="cmd", required=True)

    s_list = sub.add_parser("list", help="列出全部映射")
    s_list.add_argument("--json", action="store_true", help="以 JSON 输出")

    s_set = sub.add_parser("set", help="覆盖设置 conv_local 的标签列表")
    s_set.add_argument("--conv", required=True, help="会话 conv_local")
    s_set.add_argument("--labels", required=True, help="逗号分隔标签，如 新客,测试5a")

    s_add = sub.add_parser("add", help="为 conv_local 增加一个标签")
    s_add.add_argument("--conv", required=True, help="会话 conv_local")
    s_add.add_argument("--label", required=True, help="单个标签名")

    s_rm = sub.add_parser("rm", help="删除 conv_local 下的一个标签")
    s_rm.add_argument("--conv", required=True, help="会话 conv_local")
    s_rm.add_argument("--label", required=True, help="单个标签名")

    s_drop = sub.add_parser("drop", help="删除整个 conv_local 映射")
    s_drop.add_argument("--conv", required=True, help="会话 conv_local")

    s_resolve = sub.add_parser("resolve", help="按标签反查 conv_local")
    s_resolve.add_argument("--tag", required=True, help="标签名")

    return p


def cmd_list(store: LocalLabelStore, as_json: bool) -> int:
    rows = store.rows()
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(empty)")
        return 0
    for conv, labels in sorted(rows.items(), key=lambda x: x[0]):
        print(f"{conv}\t{','.join(labels)}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    path_arg = Path(args.path) if args.path else None
    account_id = str(getattr(args, "account_id", "")).strip() or None
    store = LocalLabelStore.load(path_arg, account_id=account_id)

    if args.cmd == "list":
        return cmd_list(store, args.json)

    if args.cmd == "set":
        labels = store.set_labels(args.conv, _parse_labels(args.labels))
        store.save()
        print(f"ok set {args.conv}: {labels}")
        return 0

    if args.cmd == "add":
        labels = store.add_label(args.conv, args.label)
        store.save()
        print(f"ok add {args.conv}: {labels}")
        return 0

    if args.cmd == "rm":
        labels = store.remove_label(args.conv, args.label)
        store.save()
        print(f"ok rm {args.conv}: {labels}")
        return 0

    if args.cmd == "drop":
        deleted = store.remove_conv(args.conv)
        store.save()
        print(f"ok drop {args.conv}: {deleted}")
        return 0

    if args.cmd == "resolve":
        for conv in store.resolve(args.tag):
            print(conv)
        return 0

    raise SystemExit(f"unknown cmd: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())

