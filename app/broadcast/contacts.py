"""Step 4: 联系人 facade（GUI 禁止直连 runtime）。"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Callable

from app.broadcast.dto import ContactRowDTO
from app.broadcast.labels import LocalLabelStore
from app.broadcast.paths import account_label_names_path
from app.bootstrap.win_subprocess import run_hidden
from runtime.wework_re_android.conv_resolver import (
    ConversationHit,
    list_conversations,
    list_conversations_and_friends,
)

# 系统会话（FTA=10004 / 团队助手等）remote id 特征：< 100000 的纯数字。
_SYSTEM_REMOTE_MAX = 100000
_SYSTEM_DISPLAY_NAMES = {"企业微信团队"}


def _save_account_label_cache(account_id: str, label_map: dict[str, str]) -> None:
    """Step 2：将 label_id→中文名 写入账号数据目录。

    非空时覆盖已有缓存；失败静默（不影响主流程）。
    """
    try:
        path = account_label_names_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(label_map, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def refresh_contacts(
    account_id: str,
    *,
    refresh: bool,
    include_cached_friends: bool = False,
) -> list[ContactRowDTO]:
    """拉当前账号的联系人列表。

    - 基础来源：`list_conversations`（企微会话列表，只含"打开过对话的"）
    - 可选来源（`include_cached_friends=True`）：同一次 Frida attach 里的
      `ContactService.GetCachedContactList()` 好友快照，追加尚未开过对话但已进入
      好友缓存的 User，remote 冲突时以会话数据为准。
    - 代码中没有 Python 侧条数上限。现状上限等于 GetConversationList() 的消息列表
      缓存长度 + GetCachedContactList() 的本地好友缓存长度。
    """

    # Step 1 优化：一次 attach 同时拿会话 + 好友快照，省掉第二个子进程。
    # Step 2：同时拿标签 id→名字映射，写入账号缓存。
    if include_cached_friends:
        hits, friend_dicts, label_map = list_conversations_and_friends(refresh=refresh)
    else:
        hits = list_conversations(refresh=refresh)
        friend_dicts = []
        label_map = {}

    # Step 2：把标签映射写入账号数据目录（非空时覆盖；空时不清掉旧缓存）
    if label_map:
        _save_account_label_cache(account_id, label_map)

    label_store = LocalLabelStore.load(account_id=account_id)
    rows: list[ContactRowDTO] = []
    seen_remote: set[str] = set()
    for hit in hits:
        remote_norm = str(hit.remote).strip()
        # 过滤系统助手（FTA/审批/公告 …）—— remote 是纯数字且小于 100000
        if remote_norm.isdigit() and int(remote_norm) < _SYSTEM_REMOTE_MAX:
            continue
        # conv_type=3 一般是系统服务号；不入表
        if getattr(hit, "conv_type", -1) == 3:
            continue
        # 只在 name / peer_name 里挑显示名，绝不 fallback 到 conv_local——否则会把
        # 一堆纯数字的杂项非联系人会话（企微内部 push / 已删除会话等）当成"联系人"塞进列表。
        display_name = (hit.name or hit.peer_name).strip()
        peer_name_raw = (hit.peer_name or "").strip()
        if not display_name:
            continue
        if display_name in _SYSTEM_DISPLAY_NAMES or peer_name_raw in _SYSTEM_DISPLAY_NAMES:
            continue
        # 兜底：既没有我们打的备注、又没有客户方原始名、显示名却是一串长数字 —— 视为脏数据，扔掉
        if (
            not hit.our_remark
            and not peer_name_raw
            and display_name.isdigit()
            and len(display_name) >= 8
        ):
            continue
        labels = _merge_labels(hit, label_store=label_store)
        rows.append(
            ContactRowDTO(
                conv_local=hit.conv_local,
                display_name=display_name,
                remote=hit.remote,
                category=_infer_category(hit, display_name=display_name, labels=labels),
                labels=labels,
                peer_name=peer_name_raw,
            )
        )
        if remote_norm:
            seen_remote.add(remote_norm)

    if include_cached_friends:
        for user in friend_dicts:
            remote = str(user.get("remote", "") or user.get("remoteId", "")).strip()
            if not remote or remote in seen_remote:
                continue
            # 过滤系统账号（FTA=10004 等 5 位以内数字）
            if remote.isdigit() and int(remote) < _SYSTEM_REMOTE_MAX:
                continue
            display = (
                str(user.get("remark", "")).strip()
                or str(user.get("displayName", "")).strip()
                or str(user.get("name", "")).strip()
            )
            if not display:
                continue
            corp_name = str(user.get("corpName", "")).strip()
            # 好友快照里已经带了 labelNames（由同一次 attach 解析）
            friend_label_names = [
                str(n) for n in (user.get("labelNames") or []) if str(n).strip()
            ]
            friend_label_ids = [
                str(x).strip() for x in (user.get("labelIds") or []) if str(x).strip()
            ]
            labels = friend_label_names or list(label_store.labels_for(remote))
            has_external_label = bool(user.get("hasExternalLabel")) or bool(friend_label_ids)
            rows.append(
                ContactRowDTO(
                    conv_local=f"friend:{remote}",
                    display_name=display,
                    remote=remote,
                    category=_category_for_friend(
                        display=display,
                        corp_name=corp_name,
                        has_external_label=has_external_label,
                    ),
                    labels=labels,
                    peer_name=str(user.get("name", "")).strip(),
                )
            )
            seen_remote.add(remote)

    return rows


def _list_cached_friends(
    *,
    python_executable: str | None = None,
    module: str = "runtime.wework_re_android.list_all_contacts",
    wait_seconds: float = 8.0,
    run_fn: Callable[..., "subprocess.CompletedProcess[str]"] = run_hidden,
) -> list[dict]:
    """调用 Frida 探针，返回缓存的好友 User dict 列表。失败/空返回 []。"""
    import json
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        out_path = Path(f.name)
    try:
        # 走 build_python_module_cmd：frozen 下 `<exe> __runmod__ <module>`。
        # 老写法 `[sys.executable, "-m", module]` 在 frozen 下会拉起新 GUI 实例，
        # 表现为「刷新联系人失败」+ 后台越堆越多的 exe 僵尸进程。
        from app.bootstrap.frozen_dispatch import build_python_module_cmd
        from loguru import logger

        extra = ["--timeout-seconds", str(wait_seconds), "--out", str(out_path)]
        if python_executable and not bool(getattr(sys, "frozen", False)):
            cmd = [python_executable, "-m", module, *extra]
        else:
            cmd = build_python_module_cmd(module, *extra)
        logger.debug("_list_cached_friends spawning: {}", cmd)
        proc = run_fn(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if proc.returncode not in (0, 3):
            # 显式记录 stderr / stdout 尾部，方便定位子进程为什么挂了（frozen 下
            # 常见原因：模块 import 失败、adb 不通、frida attach 权限、企微没登录）。
            stderr_tail = (proc.stderr or "").strip().splitlines()[-20:]
            stdout_tail = (proc.stdout or "").strip().splitlines()[-5:]
            logger.warning(
                "_list_cached_friends 子进程失败: rc={} stderr_tail={} stdout_tail={}",
                proc.returncode, stderr_tail, stdout_tail,
            )
            return []
        if not out_path.exists():
            logger.warning("_list_cached_friends 子进程成功但未产出 {}", out_path)
            return []
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        users = payload.get("users") or []
        logger.info("_list_cached_friends 读到 {} 位好友", len(users))
        return [u for u in users if isinstance(u, dict)]
    except Exception as exc:
        from loguru import logger
        logger.exception("_list_cached_friends 异常: {}", exc)
        return []
    finally:
        try:
            out_path.unlink()
        except Exception:
            pass


def _merge_labels(hit: ConversationHit, *, label_store: LocalLabelStore) -> list[str]:
    labels = [str(x).strip() for x in hit.labels if str(x).strip()]
    # 新版 Frida payload 会带 label_ids，表示“企微这次实时返回了标签状态”。
    # 这时 labels 是权威结果：即使为空，也不要再把本地 β 标签合并回来，
    # 否则用户在企微里改/删标签后 GUI 会继续显示旧标签。
    if getattr(hit, "label_ids", None) is not None:
        return labels
    for item in label_store.labels_for(hit.conv_local):
        text = str(item).strip()
        if text and text not in labels:
            labels.append(text)
    return labels


def _category_for_friend(*, display: str, corp_name: str, has_external_label: bool) -> str:
    """好友缓存没有 conv_type。

    scene=1 标签或非空公司名都是外部客户。公司名为空不再当成内部同事；
    只在没有任何外部信号、且显示名里带「同事」时，才归到内部同事。
    """
    if has_external_label or corp_name.strip():
        return "外部客户"
    if "同事" in display:
        return "内部同事"
    return "外部客户"


def _infer_category(hit: ConversationHit, *, display_name: str, labels: list[str]) -> str:
    """分类：conv_type=2 内部同事，conv_type=1 外部群，其余默认外部客户。

    scene=1 标签（has_external_label）和公司名只用来确认外部客户，
    不再用「公司名为空」或名字里的「同事」把单聊判成内部。
    """
    del labels
    ct = getattr(hit, "conv_type", -1)
    if ct == 1:
        return "外部群"
    if ct == 2:
        return "内部同事"
    if getattr(hit, "has_external_label", False) or str(getattr(hit, "corp_name", "") or "").strip():
        return "外部客户"
    if len(hit.members) > 1 or "群" in display_name:
        return "外部群"
    return "外部客户"


__all__ = ["refresh_contacts"]
