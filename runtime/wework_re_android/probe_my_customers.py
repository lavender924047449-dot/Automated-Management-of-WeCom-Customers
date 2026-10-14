"""60 秒 hook 窗口：「通讯录 → 我的客户」客户 + 标签一次采集。

操作方式：
  1. 运行本脚本，等待 "WINDOW_OPEN" 输出
  2. 在企微手机端打开「通讯录 → 我的客户」
  3. 从头到尾慢速滚动客户列表，让每个客户行都渲染过一遍
  4. 60 秒到期自动结束，输出 NDJSON 到 artifacts/

hook 目标：CommonExternalContactDisplayView.setText
  - left = 客户显示名
  - right = 标签小字（多个标签以空格/顿号分隔，取决于企微版本）
  - 同一个 left 只记录一次（滚动重绘去重）

同时完成 Step 1（客户列表采集）和 Step 2（标签映射）。
不走客户群页面，不调用未验证的同步写接口。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from runtime.wework_re_android._frida_env import adb_binary, import_frida

DEFAULT_ADB_SERIAL = "127.0.0.1:16384"
DEFAULT_FRIDA_REMOTE = "127.0.0.1:27042"
DEFAULT_PROCESS_NAME = "com.tencent.wework"
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_OUT = "runtime/wework_re_android/artifacts/my_customers_window.ndjson"

JS = r"""
'use strict';
Java.perform(function () {
    var seen = {};
    var rowCount = 0;

    function emit(event, payload) {
        var out = { event: event, tsMs: Date.now() };
        if (payload) {
            Object.keys(payload).forEach(function (k) { out[k] = payload[k]; });
        }
        send(out);
    }

    function textOf(v) {
        if (v === null || v === undefined) return '';
        try { return String(v).trim(); } catch (_) { return ''; }
    }

    // ---- hook ContactSummaryView ----
    // "我的客户"列表行 = ContactSummaryView
    //   setTitle(String)       → 客户显示名
    //   setSubTitle1(CS)       → 标签小字
    var CSV = Java.use('com.tencent.wework.contact.views.ContactSummaryView');

    // 用字段 i（String）暂存最近一次 setTitle 的值，
    // 因为 setTitle 和 setSubTitle1 可能分两次调用，
    // 需要把同一个 View 实例上的 title + subTitle 配对。
    var origSetTitle = CSV.setTitle.overload('java.lang.String');
    origSetTitle.implementation = function (title) {
        var t = textOf(title);
        if (t) {
            // 记到 Java 字段 i 上（ContactSummaryView 自己就有这个 String 字段）
            try { this.i.value = t; } catch (_) {}
        }
        return origSetTitle.call(this, title);
    };

    // setSubTitle1(CharSequence) — 标签
    var origSub1 = CSV.setSubTitle1.overload('java.lang.CharSequence');
    origSub1.implementation = function (sub) {
        var subText = textOf(sub);
        // 读取同一 View 上最近的 title
        var titleText = '';
        try { titleText = textOf(this.i.value); } catch (_) {}
        if (!titleText) {
            return origSub1.call(this, sub);
        }
        if (seen[titleText]) {
            if (subText && seen[titleText] !== subText) {
                seen[titleText] = subText;
                emit('row_update', { left: titleText, right: subText });
            }
            return origSub1.call(this, sub);
        }
        rowCount += 1;
        seen[titleText] = subText || '__empty__';
        emit('row', { left: titleText, right: subText, n: rowCount });
        return origSub1.call(this, sub);
    };

    // setSubTitle1(int, CharSequence) — 带 visibility 的重载
    try {
        var origSub1v = CSV.setSubTitle1.overload('int', 'java.lang.CharSequence');
        origSub1v.implementation = function (vis, sub) {
            var subText = textOf(sub);
            var titleText = '';
            try { titleText = textOf(this.i.value); } catch (_) {}
            if (titleText && !seen[titleText]) {
                rowCount += 1;
                seen[titleText] = subText || '__empty__';
                emit('row', { left: titleText, right: subText, n: rowCount });
            }
            return origSub1v.call(this, vis, sub);
        };
    } catch (eOvl) {
        emit('log', { msg: 'setSubTitle1(int,CS) overload skipped: ' + eOvl });
    }

    emit('window_open', { seconds: __WINDOW_SECONDS__ });
});
"""


def find_pid(adb_serial: str, process_name: str) -> int | None:
    raw = subprocess.check_output(
        [adb_binary(), "-s", adb_serial, "shell", "pidof", process_name],
        stderr=subprocess.STDOUT,
    ).decode("utf-8", "replace")
    text = raw.strip().split()
    if not text:
        return None
    try:
        return int(text[0])
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="60s hook 窗口：通讯录→我的客户（客户+标签）")
    parser.add_argument("--adb-serial", default=DEFAULT_ADB_SERIAL)
    parser.add_argument("--frida-remote", default=DEFAULT_FRIDA_REMOTE)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    pid = find_pid(args.adb_serial, args.process_name)
    if pid is None:
        print(f"[probe] process not found: {args.process_name}")
        return 1

    frida = import_frida()
    device = frida.get_device_manager().add_remote_device(args.frida_remote)
    session = device.attach(pid)
    js = JS.replace("__WINDOW_SECONDS__", str(int(args.window_seconds)))
    script = session.create_script(js)
    logs: list[dict[str, Any]] = []
    customer_map: dict[str, str] = {}  # left(客户名) → right(标签)

    def on_message(msg: dict[str, Any], _data: Any) -> None:
        if msg.get("type") == "error":
            row = {"event": "script_error", "stack": msg.get("stack", str(msg))}
            logs.append(row)
            print(json.dumps(row, ensure_ascii=False)[:800])
            return
        if msg.get("type") != "send":
            return
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return
        logs.append(payload)
        event = payload.get("event")
        if event == "row":
            left = payload.get("left", "")
            right = payload.get("right", "")
            customer_map[left] = right
            n = payload.get("n", "?")
            tag_info = f"  标签=[{right}]" if right else ""
            print(f"[{n}] {left}{tag_info}")
            return
        if event == "row_update":
            left = payload.get("left", "")
            right = payload.get("right", "")
            customer_map[left] = right
            print(f"[更新] {left}  标签=[{right}]")
            return
        print(json.dumps(payload, ensure_ascii=False)[:500])

    script.on("message", on_message)
    script.load()
    ws = int(args.window_seconds)
    print(f"[probe] WINDOW_OPEN pid={pid} seconds={ws}", flush=True)
    print(f"[probe] 请在企微打开「通讯录 → 我的客户」，慢速滚动浏览所有客户", flush=True)
    print(f"[probe] {ws} 秒后自动结束...", flush=True)
    time.sleep(max(1.0, float(args.window_seconds)))
    session.detach()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in logs),
        encoding="utf-8",
    )

    # 输出汇总
    print(f"\n{'='*50}")
    print(f"[probe] WINDOW_CLOSED")
    print(f"[probe] 总事件数: {len(logs)}")
    print(f"[probe] 去重后客户数: {len(customer_map)}")
    tagged = sum(1 for v in customer_map.values() if v and v != "__empty__")
    print(f"[probe] 有标签的客户: {tagged}")
    print(f"[probe] 无标签的客户: {len(customer_map) - tagged}")
    print(f"[probe] 输出文件: {out}")

    # 生成客户-标签汇总 JSON
    summary_path = out.with_name("my_customers_summary.json")
    summary = []
    for name, tags in sorted(customer_map.items()):
        entry: dict[str, Any] = {"name": name}
        if tags and tags != "__empty__":
            entry["tags_raw"] = tags
            # 尝试按常见分隔符拆分
            for sep in ("、", " ", "，", ","):
                if sep in tags:
                    entry["tags"] = [t.strip() for t in tags.split(sep) if t.strip()]
                    break
            else:
                entry["tags"] = [tags.strip()] if tags.strip() else []
        else:
            entry["tags_raw"] = ""
            entry["tags"] = []
        summary.append(entry)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[probe] 客户-标签汇总: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
