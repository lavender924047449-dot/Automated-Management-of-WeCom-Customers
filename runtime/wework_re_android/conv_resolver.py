"""Step 4.4: 抽取可复用的联系人名 -> conv_local 解析器。"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from app.broadcast.labels import LocalLabelStore
from app.broadcast.paths import normalize_account_id
from app.bootstrap.win_subprocess import run_hidden

HIT_RE = re.compile(
    r"^\[HIT\]\s+local=(?P<local>\S+)\s+remote=(?P<remote>\S+)\s+name=(?P<name>.+?)\s+members=(?P<members>.+?)(?:\s+labels=(?P<labels>.+))?$"
)
ROW_RE = re.compile(
    r"^\[(?P<tag>[^\]]+)\]\[\s*(?P<idx>\d+)\]\s+local=(?P<local>\S+)\s+remote=(?P<remote>\S+)\s+name=(?P<name>.+?)\s+members=(?P<members>.+?)\s+labels=(?P<labels>.+)$"
)


@dataclass(frozen=True)
class ConversationHit:
    """一条联系人命中结果。

    字段语义（改造后）：
      - `name`         : 显示名（GUI 优先展示这个）
          * 单聊：`ourRemark`（我们对客户的备注）优先，否则 `peerName`
          * 群聊：群自定义名字优先，否则 `peerName`（成员拼接名）
      - `remote`       : 对方 remote id（跨会话稳定）
      - `conv_local`   : 会话本地 id（从 raw 里 `Local:` 抽出）
      - `members`      : 客户方原始名字/群成员名（历史用途，保留兼容）
      - `labels`       : 中文标签名（已由 Python 侧把 label id 转成名称）
      - `conv_type`    : 会话类型：0=外部单聊、1=外部群、3=系统服务号（FTA 等）、其他
      - `peer_name`    : 客户方原始名字（客户微信名/群成员拼接名）
      - `our_remark`   : 我们打的备注（getRealRemark）
      - `label_ids`    : 企微远端返回的标签 id；None 表示旧格式/未知来源
    """

    conv_local: str
    remote: str
    name: str
    members: tuple[str, ...]
    labels: tuple[str, ...] = ()
    conv_type: int = -1
    peer_name: str = ""
    our_remark: str = ""
    label_ids: tuple[str, ...] | None = None
    corp_name: str = ""
    # scene=1（我的客户）标签命中。中文名解析失败时仍然为 True，供内外分类使用。
    has_external_label: bool = False


def _safe_literal(value: str, default: str | list[str]) -> str | list[str]:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return default
    if isinstance(default, str) and isinstance(parsed, str):
        return parsed
    if isinstance(default, list) and isinstance(parsed, list):
        return [str(x) for x in parsed]
    return default


_LABEL_NAMES_PATH = Path(__file__).resolve().parents[2] / "runtime" / "customer_label_names.json"


def _load_label_names_map() -> dict[str, str]:
    """SOP §6：读 runtime/customer_label_names.json 把 label id 映射到中文名。"""
    try:
        raw = json.loads(_LABEL_NAMES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip()
        val = str(v).strip()
        if not key or key.startswith("_") or not val:
            continue
        out[key] = val
    return out


def _pick_group_display_name(group_candidates: dict, peer_name: str) -> str:
    """群备注名优先级：roomname_remark > getter 备注/名字 > peerName。"""
    if not isinstance(group_candidates, dict):
        return peer_name
    for key in (
        "protoRoomRemark",
        "getRemark",
        "getGroupRemark",
        "getName",
        "getGroupName",
        "getTitle",
        "getSubject",
        "protoRoomName",
    ):
        v = str(group_candidates.get(key, "")).strip()
        if v:
            return v
    for v in group_candidates.values():
        s = str(v).strip()
        if s:
            return s
    return peer_name


def _pick_group_peer_name(group_candidates: dict, peer_name: str) -> str:
    """群 id 列：群本身名字优先，避免展示成员拼接名。"""
    if not isinstance(group_candidates, dict):
        return peer_name
    for key in ("protoRoomName", "protoDefaultName"):
        v = str(group_candidates.get(key, "")).strip()
        if v:
            return v
    return peer_name


def _hit_from_json(obj: dict, *, account_label_map: dict[str, str] | None = None) -> ConversationHit:
    # 新格式 payload 用 labelIds（string 数组），老格式 fixture 用 labels（已解析字符串）
    peer_name = str(obj.get("peerName", obj.get("name", ""))).strip()
    our_remark = str(obj.get("ourRemark", "")).strip()
    corp_name = str(obj.get("corpName", "")).strip()
    label_ids: tuple[str, ...] | None
    if "labelIds" in obj:
        label_ids = tuple(str(x) for x in (obj.get("labelIds") or []))
        live_label_names = [str(x).strip() for x in (obj.get("labelNames") or []) if str(x).strip()]
        # Step 2 三级兜底：实时名 → 账号缓存 → 全局 JSON
        acct_map = account_label_map or {}
        global_map = _load_label_names_map()
        resolved: list[str] = []
        for name in live_label_names:
            if name not in resolved:
                resolved.append(name)
        for lid in label_ids:
            # 已经从 live names 拿到了就跳过
            if any(n == (acct_map.get(lid) or global_map.get(lid, "")).strip() for n in resolved):
                # 防止重复：如果实时名已包含这个 id 对应的名字，跳过
                pass
            name = acct_map.get(lid, "").strip() or global_map.get(lid, "").strip()
            if name and name not in resolved:
                resolved.append(name)
                continue
            # 没有中文映射：如果本身就是纯数字 label id，别把这串数字展示成"标签名"，
            # 直接丢掉，避免用备注后缀等非标签来源造成错误展示。
            if lid.isdigit():
                continue
            if lid not in resolved:
                resolved.append(lid)
        labels = tuple(resolved)
    else:
        label_ids = None
        labels = tuple(str(x) for x in (obj.get("labels") or []))
    has_external_label = bool(obj.get("hasExternalLabel")) or bool(label_ids)
    conv_type = int(obj.get("type", -1)) if obj.get("type") is not None else -1
    # 决定"显示名"：单聊优先我们的备注；群优先群自定义名/群备注；否则 peerName 兜底
    if conv_type == 1:
        group_candidates = obj.get("groupCandidates") or {}
        display = _pick_group_display_name(group_candidates, peer_name)
        peer_name = _pick_group_peer_name(group_candidates, peer_name)
    elif conv_type == 0:
        display = our_remark or peer_name
    else:
        display = peer_name

    members_field = obj.get("members")
    if isinstance(members_field, list):
        members_tuple = tuple(str(x) for x in members_field)
    else:
        members_tuple = ()

    return ConversationHit(
        conv_local=str(obj.get("local", "")),
        remote=str(obj.get("remote", "")),
        name=display,
        members=members_tuple,
        labels=labels,
        conv_type=conv_type,
        peer_name=peer_name,
        our_remark=our_remark,
        label_ids=label_ids if "labelIds" in obj else None,
        corp_name=corp_name,
        has_external_label=has_external_label,
    )


def _hit_from_json_legacy(obj: dict) -> ConversationHit:
    """老 payload 兼容（测试 fixture 用 name/members/labels 三字段）。"""
    return ConversationHit(
        conv_local=str(obj.get("local", "")),
        remote=str(obj.get("remote", "")),
        name=str(obj.get("name", "")),
        members=tuple(str(x) for x in (obj.get("members") or [])),
        labels=tuple(str(x) for x in (obj.get("labels") or [])),
    )


def _iter_json_events(output: str):
    """产出脚本 stdout 里的每一行 JSON 对象；非 JSON 行忽略。"""

    for raw in output.splitlines():
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_hits(output: str) -> list[ConversationHit]:
    """从 list_conversations_with_names.py 的 stdout 解析命中记录（JSONL 优先，正则兜底）。"""

    hits: list[ConversationHit] = []
    for obj in _iter_json_events(output):
        if obj.get("t") == "hit":
            hits.append(_hit_from_json(obj))
    if hits:
        return hits
    # 兼容旧文本格式（测试 fixture 里仍用文本行）
    for raw in output.splitlines():
        line = raw.strip()
        m = HIT_RE.match(line)
        if not m:
            continue
        name = str(_safe_literal(m.group("name"), ""))
        members = tuple(_safe_literal(m.group("members"), []))
        labels_raw = m.groupdict().get("labels") or "[]"
        labels = tuple(_safe_literal(labels_raw, []))
        hits.append(
            ConversationHit(
                conv_local=m.group("local"),
                remote=m.group("remote"),
                name=name,
                members=members,
                labels=labels,
            )
        )
    return hits


def parse_rows(output: str, *, account_label_map: dict[str, str] | None = None) -> list[ConversationHit]:
    """从 list_conversations_with_names.py 的 stdout 解析完整 row 记录（JSONL 优先，正则兜底）。"""

    rows: list[ConversationHit] = []
    for obj in _iter_json_events(output):
        if obj.get("t") == "row":
            rows.append(_hit_from_json(obj, account_label_map=account_label_map))
    if rows:
        return rows
    # 兼容旧文本格式
    for raw in output.splitlines():
        line = raw.strip()
        m = ROW_RE.match(line)
        if not m:
            continue
        name = str(_safe_literal(m.group("name"), ""))
        members = tuple(_safe_literal(m.group("members"), []))
        labels = tuple(_safe_literal(m.group("labels"), []))
        rows.append(
            ConversationHit(
                conv_local=m.group("local"),
                remote=m.group("remote"),
                name=name,
                members=members,
                labels=labels,
            )
        )
    return rows


def parse_friends(output: str) -> list[dict]:
    """从 list_conversations_with_names.py 的 stdout 解析 t:'friend' 好友快照。

    返回 dict 列表，每条含 remote/remark/displayName/name/corpName/labelIds/labelNames。
    这些好友来自 GetCachedContactList()，含"已添加但未开过对话的"联系人。
    """
    friends: list[dict] = []
    for obj in _iter_json_events(output):
        if obj.get("t") == "friend":
            friends.append(obj)
    return friends


def parse_label_map(output: str) -> dict[str, str]:
    """从 stdout 解析 t:'label_map' 事件，返回 {label_id: 中文名}。

    用于 Step 2 写入账号级缓存。可能为空（异步回调超时、无标签等）。
    """
    for obj in _iter_json_events(output):
        if obj.get("t") == "label_map":
            raw = obj.get("map") or {}
            if isinstance(raw, dict):
                return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(v).strip()}
    return {}


def _score_hit(keyword: str, hit: ConversationHit) -> int:
    """打分规则：精确命中优先，其次成员命中，再次子串命中。"""

    if hit.name == keyword:
        return 300
    if keyword in hit.name:
        return 200
    if any(member == keyword for member in hit.members):
        return 150
    if any(keyword in member for member in hit.members):
        return 120
    if any(label == keyword for label in hit.labels):
        return 110
    if any(keyword in label for label in hit.labels):
        return 100
    return 0


def _label_score(tag: str, hit: ConversationHit) -> int:
    if any(label == tag for label in hit.labels):
        return 200
    if any(tag in label for label in hit.labels):
        return 120
    return 0


class ConversationResolver:
    """调用 Frida 枚举脚本并解析 conv_local。"""

    def __init__(
        self,
        *,
        account_id: str | None = None,
        python_executable: str | None = None,
        module: str = "runtime.wework_re_android.list_conversations_with_names",
        wait_seconds: float = 8.0,
        run_fn: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
        cache_ttl_seconds: float = 30.0,
        time_fn: Callable[[], float] = time.time,
        local_label_path: str | Path | None = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.module = module
        self.wait_seconds = wait_seconds
        self.run_fn = run_fn
        self.cache_ttl_seconds = cache_ttl_seconds
        self.time_fn = time_fn
        self.local_label_path = local_label_path
        self.account_id = normalize_account_id(account_id)
        self._cache: dict[str, tuple[float, list[ConversationHit]]] = {}

    def resolve(self, name: str, *, refresh: bool = False) -> str:
        key = name.strip()
        if not key:
            raise ValueError("name 不能为空")
        if key.isdigit():
            return key

        hits = self._query(key, refresh=refresh)
        if not hits:
            raise LookupError(f"未找到联系人: {key}")

        best_score = max(_score_hit(key, row) for row in hits)
        winners = [row for row in hits if _score_hit(key, row) == best_score and best_score > 0]
        if len(winners) == 1:
            return winners[0].conv_local
        if winners:
            raise LookupError(self._ambiguous_error(key, winners))
        raise LookupError(f"未找到联系人: {key}")

    def resolve_by_label(self, tag: str, *, refresh: bool = False) -> list[str]:
        key = tag.strip()
        if not key:
            raise ValueError("tag 不能为空")

        alpha = self._resolve_alpha_label(key, refresh=refresh)
        beta = self._resolve_beta_label(key)
        uniq: list[str] = []
        seen: set[str] = set()
        for conv_local in [*alpha, *beta]:
            if conv_local in seen:
                continue
            seen.add(conv_local)
            uniq.append(conv_local)
        if not uniq:
            raise LookupError(f"未找到标签: {key}")
        return uniq

    def _resolve_alpha_label(self, tag: str, *, refresh: bool) -> list[str]:
        try:
            hits = self._query(tag, refresh=refresh)
        except (RuntimeError, OSError):
            return []
        scored = [row for row in hits if _label_score(tag, row) > 0]
        uniq: list[str] = []
        seen: set[str] = set()
        for row in scored:
            if row.conv_local in seen:
                continue
            seen.add(row.conv_local)
            uniq.append(row.conv_local)
        return uniq

    def _resolve_beta_label(self, tag: str) -> list[str]:
        try:
            return LocalLabelStore.load(self.local_label_path, account_id=self.account_id).resolve(tag)
        except LookupError:
            return []

    def _query(self, keyword: str, *, refresh: bool) -> list[ConversationHit]:
        now = self.time_fn()
        cached = self._cache.get(keyword)
        if cached and (now - cached[0] <= self.cache_ttl_seconds):
            return cached[1]

        # frozen 下 self.python_executable = exe，`-m` 会拉起新 GUI；改走 __runmod__。
        from app.bootstrap.frozen_dispatch import build_python_module_cmd

        extra = ["--keyword", keyword, "--wait-seconds", str(self.wait_seconds)]
        if refresh:
            extra.append("--refresh")
        if bool(getattr(sys, "frozen", False)):
            cmd = build_python_module_cmd(self.module, *extra)
        else:
            cmd = [self.python_executable, "-m", self.module, *extra]

        proc = self.run_fn(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip() or "unknown error"
            raise RuntimeError(f"会话查询失败: {err}")

        merged = f"{proc.stdout}\n{proc.stderr}"
        hits = parse_hits(merged)
        self._cache[keyword] = (now, hits)
        return hits

    @staticmethod
    def _ambiguous_error(keyword: str, rows: Iterable[ConversationHit]) -> str:
        pairs = ", ".join(f"{row.name}:{row.conv_local}" for row in rows)
        return f"联系人匹配不唯一: {keyword} -> {pairs}"


def resolve_name(name: str, **kwargs: object) -> str:
    """便捷函数：一次性解析联系人。"""

    resolver = ConversationResolver(**kwargs)
    return resolver.resolve(name)


def resolve_by_label(tag: str, **kwargs: object) -> list[str]:
    """便捷函数：一次性按标签解析会话。"""

    resolver = ConversationResolver(**kwargs)
    return resolver.resolve_by_label(tag)


def list_conversations(
    *,
    refresh: bool = False,
    wait_seconds: float = 8.0,
    python_executable: str | None = None,
    module: str = "runtime.wework_re_android.list_conversations_with_names",
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
) -> list[ConversationHit]:
    """列出当前账号下的会话（供 GUI 联系人页使用）。"""

    # frozen 下 sys.executable = exe，`-m` 会拉起新 GUI；改走 __runmod__。
    from app.bootstrap.frozen_dispatch import build_python_module_cmd

    extra = ["--wait-seconds", str(wait_seconds)]
    if refresh:
        extra.append("--refresh")
    if python_executable and not bool(getattr(sys, "frozen", False)):
        cmd = [python_executable, "-m", module, *extra]
    else:
        cmd = build_python_module_cmd(module, *extra)

    # Windows 下必须显式 utf-8，否则 subprocess 用系统 cp936 解码子进程 UTF-8 输出会 mojibake
    # 让 JSON 解析崩掉（中文里的 \n 转义在 GBK 视角下可能变非法转义）。
    proc = run_fn(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip() or "unknown error"
        raise RuntimeError(f"会话查询失败: {err}")

    merged = f"{proc.stdout}\n{proc.stderr}"
    rows = parse_rows(merged)
    # 脚本先 dump 'now'、再 dump 'after'（refresh 后的），按 (conv_local, remote) 去重：
    # - conv_local 若拿不到（'?'）就用 remote 兜底，避免所有 '?' 会话被压成一条。
    dedup: dict[tuple[str, str], ConversationHit] = {}
    for row in rows:
        key = (row.conv_local, row.remote) if row.conv_local and row.conv_local != "?" else ("?", row.remote)
        dedup[key] = row
    if not dedup and proc.stderr.strip():
        diag_lines = [
            ln for ln in proc.stderr.splitlines()
            if ln.startswith("[total=") or ln.startswith("[log]") or ln.startswith("[after]") or ln.startswith("[now]")
        ]
        if diag_lines:
            raise RuntimeError("会话枚举返回 0 条。诊断日志：\n" + "\n".join(diag_lines))
    return list(dedup.values())


def list_conversations_and_friends(
    *,
    refresh: bool = False,
    wait_seconds: float = 8.0,
    python_executable: str | None = None,
    module: str = "runtime.wework_re_android.list_conversations_with_names",
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
) -> tuple[list[ConversationHit], list[dict], dict[str, str]]:
    """一次 attach 同时返回会话行、好友快照和标签名映射。

    返回 (conversation_hits, friend_dicts, label_name_map)。
    - friend_dicts 来自 GetCachedContactList()，每条含
      remote/remark/displayName/name/corpName/labelIds/labelNames。
    - label_name_map 是 {label_id: 中文名}，用于写入账号级缓存。
    """
    from app.bootstrap.frozen_dispatch import build_python_module_cmd

    extra = ["--wait-seconds", str(wait_seconds)]
    if refresh:
        extra.append("--refresh")
    if python_executable and not bool(getattr(sys, "frozen", False)):
        cmd = [python_executable, "-m", module, *extra]
    else:
        cmd = build_python_module_cmd(module, *extra)

    proc = run_fn(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip() or "unknown error"
        raise RuntimeError(f"会话查询失败: {err}")

    merged = f"{proc.stdout}\n{proc.stderr}"

    # --- 标签 id→名字 映射（Step 2: 先解析，用于 parse_rows 的三级兜底） ---
    label_map = parse_label_map(merged)

    # --- 会话行（与 list_conversations 相同的去重逻辑） ---
    rows = parse_rows(merged, account_label_map=label_map)
    dedup: dict[tuple[str, str], ConversationHit] = {}
    for row in rows:
        key = (
            (row.conv_local, row.remote)
            if row.conv_local and row.conv_local != "?"
            else ("?", row.remote)
        )
        dedup[key] = row
    if not dedup and proc.stderr.strip():
        diag_lines = [
            ln for ln in proc.stderr.splitlines()
            if ln.startswith("[total=") or ln.startswith("[log]")
            or ln.startswith("[after]") or ln.startswith("[now]")
        ]
        if diag_lines:
            raise RuntimeError(
                "会话枚举返回 0 条。诊断日志：\n" + "\n".join(diag_lines)
            )

    # --- 好友快照 ---
    friends = parse_friends(merged)

    return list(dedup.values()), friends, label_map


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Resolve conversation local id by display name")
    p.add_argument("--name", help="联系人名称或关键字")
    p.add_argument("--label", help="标签关键字")
    p.add_argument("--refresh", action="store_true", help="先拉取会话再解析")
    p.add_argument("--wait-seconds", type=float, default=8.0)
    args = p.parse_args()
    if not args.name and not args.label:
        p.error("必须提供 --name 或 --label")
    return args


def main() -> int:
    args = _parse_args()
    resolver = ConversationResolver(wait_seconds=args.wait_seconds)
    if args.label:
        for conv_local in resolver.resolve_by_label(args.label, refresh=args.refresh):
            print(conv_local)
        return 0

    conv_local = resolver.resolve(str(args.name), refresh=args.refresh)
    print(conv_local)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
