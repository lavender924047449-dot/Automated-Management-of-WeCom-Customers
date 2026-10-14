"""增加素材：从企微文件传输助手采集文字 / 语音 / 图片 / 视频 / 公众号 / 小程序。

采集结果先进入「本轮已捕获」。双击名字列命名后才写入素材库。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.broadcast import image_templates as image_tpl_mod
from app.broadcast import oa_templates as oa_tpl_mod
from app.broadcast import video_templates as video_tpl_mod
from app.broadcast import wxa_templates as wxa_tpl_mod
from app.broadcast.asset_bundles import canon_asset_type
from app.broadcast.asset_names import asset_name_conflict
from app.broadcast.capture_client import CaptureClient
from app.broadcast.library import VoiceLibrary
from app.broadcast.paths import account_voice_index_path
from app.broadcast.preview import PreviewService
from app.config import resolve_path
from gui.copy_support import add_copy_action, prepare_context_selection
from gui.i18n import asset_type_to_zh
from gui.widgets.asset_preview import AssetPreviewPanel


def _capture_exit_reason(tail: list[str]) -> str:
    """从采集子进程 stderr 里抽出一行能看懂的原因，跳过补丁日志和 traceback。"""
    skip_bits = (
        "CREATE_NO_WINDOW",
        "Traceback",
        "File \"",
        "File '",
        "raise SystemExit",
        "subprocess.",
        "^^^^^^^^",
        "frozen runpy",
    )
    for line in reversed(tail):
        text = line.strip()
        if not text:
            continue
        if any(bit in text for bit in skip_bits):
            continue
        return text[:300]
    return ""


class _CaptureBridge(QObject):
    """把 :class:`CaptureClient` 工作线程回调转成 Qt signal，跨线程安全。"""

    event = Signal(dict)
    exited = Signal(int)


class LibraryIngestTab(QWidget):
    """FTA 素材采集入口（F3.5 单一采集口）。"""

    assetsChanged = Signal()

    _FTA_REMOTE_ID = "10006"
    _DURATION_OPTIONS = (5, 10, 30)  # 分钟

    # 已捕获面板列 index
    _COL_KIND = 0
    _COL_NAME = 1
    _COL_EXTRA = 2
    _COL_TIME = 3
    _COL_STATUS = 4

    def __init__(self, *, preview_service_provider: Callable[[], PreviewService] | None = None) -> None:
        super().__init__()
        self._account_id = "default"
        self._preview_service_provider = preview_service_provider

        # F3.5：单一 CaptureClient 覆盖 voice/image/video/oa
        self._client: CaptureClient | None = None
        self._bridge: _CaptureBridge | None = None
        self._started_ms: float = 0.0
        self._deadline_ms: float = 0.0
        self._counts: dict[str, int] = {
            "voice": 0,
            "image": 0,
            "video": 0,
            "oa": 0,
            "doc": 0,
            "channel": 0,
            "wxa": 0,
            "text": 0,
        }
        # F3.4：Windows 上 SIGTERM 强杀 exit=1 与真异常无法从 code 区分，用旗标区分
        self._user_stopped: bool = False
        # 本轮已捕获条目：{template_id: {kind, name, extra, ts_iso, row_idx}}
        self._session_rows: dict[str, dict[str, Any]] = {}
        self._session_updating = False  # 防 itemChanged 递归

        self._build_ui()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)

    # ==================================================================
    # UI build
    # ==================================================================
    def _build_ui(self) -> None:
        root = QHBoxLayout(self)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)

        hint = QLabel(
            "开启后，在企微「文件传输助手」里发送的文字 / 语音 / 图片 / 视频 / 公众号 / "
            "企微文档 / 视频号 / 小程序会进入下方「本轮已捕获」列表。"
            "双击「名字」列命名后才会写入素材库。"
        )
        hint.setWordWrap(True)
        left_layout.addWidget(hint)

        dur_bar = QHBoxLayout()
        dur_bar.addWidget(QLabel("采集时长"))
        self._dur_pick = QComboBox()
        for m in self._DURATION_OPTIONS:
            self._dur_pick.addItem(f"{m} 分钟", m)
        self._dur_pick.setCurrentIndex(1)  # 默认 10 分钟
        dur_bar.addWidget(self._dur_pick)
        dur_bar.addStretch(1)
        left_layout.addLayout(dur_bar)

        notice = QLabel("注意：只有被命名的素材会保存到素材库")
        notice.setWordWrap(True)
        notice.setStyleSheet("font-weight: 600;")
        left_layout.addWidget(notice)

        self._btn_toggle = QPushButton("开始录素材")
        self._btn_toggle.setCheckable(True)
        self._btn_toggle.clicked.connect(self._toggle_capture)
        left_layout.addWidget(self._btn_toggle)

        self._status_label = QLabel("未运行")
        left_layout.addWidget(self._status_label)

        left_layout.addWidget(QLabel("本轮已捕获（双击「名字」列命名）："))
        self._captured_table = QTableWidget(0, 5)
        self._captured_table.setHorizontalHeaderLabels(["类型", "名字", "详情", "时间", "状态"])
        header = self._captured_table.horizontalHeader()
        header.setSectionResizeMode(self._COL_KIND, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_EXTRA, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        self._captured_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._captured_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._captured_table.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._captured_table.cellDoubleClicked.connect(self._on_captured_cell_double_clicked)
        self._captured_table.itemChanged.connect(self._on_captured_item_changed)
        self._captured_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._captured_table.customContextMenuRequested.connect(self._on_captured_context_menu)
        left_layout.addWidget(self._captured_table, 1)
        clear_row = QHBoxLayout()
        self._btn_clear_captured = QPushButton("清空")
        self._btn_clear_captured.clicked.connect(self._clear_session_panel)
        clear_row.addWidget(self._btn_clear_captured)
        clear_row.addStretch(1)
        left_layout.addLayout(clear_row)

        root.addWidget(left, 1)

    # ==================================================================
    # 账号切换 / 全停
    # ==================================================================
    def set_account(self, account_id: str) -> None:
        target = account_id.strip() or "default"
        if target == self._account_id:
            return
        self.stop_all()
        self._account_id = target
        # 避免跨账号污染当前会话捕获视图。
        self._clear_session_panel()

    def stop_all(self) -> None:
        self._stop_capture()
        self._btn_toggle.setChecked(False)
        self._btn_toggle.setText("开始录素材")
        self._dur_pick.setEnabled(True)
        self._refresh_status()

    # ==================================================================
    # 主采集（F3.5 统一入口）
    # ==================================================================
    def _toggle_capture(self, checked: bool) -> None:
        if checked:
            if not self._start_capture():
                self._btn_toggle.setChecked(False)
                return
            self._btn_toggle.setText("关闭当前采集")
            self._dur_pick.setEnabled(False)
        else:
            self._stop_capture()
            self._btn_toggle.setText("开始录素材")
            self._dur_pick.setEnabled(True)

    def _start_capture(self) -> bool:
        if self._client is not None and self._client.is_running():
            return True
        minutes = int(self._dur_pick.currentData() or self._DURATION_OPTIONS[1])
        timeout_seconds = max(60, minutes * 60)
        bridge = _CaptureBridge(self)  # parent=self 让 Qt 接管生命周期（F3.4 修）
        bridge.event.connect(self._on_capture_event)
        bridge.exited.connect(self._on_capture_exit)
        client = CaptureClient(
            on_event=bridge.event.emit,
            on_exit=bridge.exited.emit,
            stderr_tail_size=20,
        )
        try:
            client.start(
                account_id=self._account_id,
                timeout_seconds=float(timeout_seconds),
                fta_remote_id=self._FTA_REMOTE_ID,
                index_path=str(account_voice_index_path(self._account_id)),
                cwd=str(resolve_path(Path("."))),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("FTA 采集启动失败 account_id={} err={}", self._account_id, exc)
            QMessageBox.warning(self, "FTA 采集启动失败", "未能启动采集，请确认模拟器和企微状态后重试。")
            return False
        self._client = client
        self._bridge = bridge
        self._started_ms = time.time()
        self._deadline_ms = self._started_ms + timeout_seconds
        self._counts = {"voice": 0, "image": 0, "video": 0, "oa": 0, "wxa": 0, "text": 0}
        self._user_stopped = False
        self._status_timer.start()
        return True

    def _stop_capture(self) -> None:
        self._status_timer.stop()
        client = self._client
        if client is None:
            return
        self._user_stopped = True
        try:
            client.stop(grace_seconds=3.0)
        except Exception:  # noqa: BLE001
            pass

    def _on_capture_event(self, payload: dict) -> None:
        t = str(payload.get("t", ""))
        if t == "captured":
            kind = str(payload.get("kind", ""))
            if kind in self._counts:
                self._counts[kind] += 1
            self._append_captured_row(kind, payload)
            self.assetsChanged.emit()
        elif t == "capture_failed":
            # 静默记录，避免频繁弹窗；stderr tail 里能看到细节
            pass
        elif t == "heartbeat_lost":
            self._status_label.setText("重连中…")
            return
        elif t == "reconnected":
            self._status_label.setText("已重连，继续采集中")
            return
        self._refresh_status()

    def _on_capture_exit(self, code: int) -> None:
        # 已在主线程（signal.AutoConnection）：安全释放 bridge + client
        self._status_timer.stop()
        client = self._client
        bridge = self._bridge
        user_stopped = self._user_stopped
        self._user_stopped = False
        self._client = None
        self._bridge = None
        if bridge is not None:
            bridge.deleteLater()

        # F3.4：Windows terminate exit=1 与真异常无法从 code 区分，用旗标抑制误报
        if code != 0 and not user_stopped:
            tail_lines = (client.stats.stderr_tail if client else [])[-20:]
            tail = "\n".join(tail_lines)
            logger.warning("FTA 采集异常退出 code={} stderr_tail={}", code, tail)
            reason = _capture_exit_reason(tail_lines)
            detail = f"\n\n{reason}" if reason else ""
            QMessageBox.warning(
                self,
                "FTA 采集异常退出",
                "采集进程异常退出，请重新开始采集。" + detail,
            )

        # 收尾：按钮复位、刷新已保存的素材列表。
        # 当前会话已捕获素材保留到程序退出（不在关闭采集时清空）。
        self._btn_toggle.setChecked(False)
        self._btn_toggle.setText("开始录素材")
        self._dur_pick.setEnabled(True)
        self._refresh_status()
        # 触发 saved tab 重载（未命名条目仍以 `未命名 #NNN` 存在，用户可后续再命名）
        self.assetsChanged.emit()

    def _refresh_status(self) -> None:
        if self._client is None or not self._client.is_running():
            self._status_label.setText("未运行")
            return
        remain = max(0.0, self._deadline_ms - time.time())
        mm, ss = divmod(int(remain), 60)
        elapsed = int(time.time() - self._started_ms)
        emm, ess = divmod(elapsed, 60)
        total = sum(self._counts.values())
        parts = [f"{k}:{v}" for k, v in self._counts.items() if v > 0]
        detail = "、".join(parts) if parts else "无"
        self._status_label.setText(
            f"采集中 · {emm:02d}:{ess:02d} / 剩 {mm:02d}:{ss:02d} · "
            f"共 {total} 条（{detail}）"
        )

    # ==================================================================
    # 本轮已捕获面板
    # ==================================================================
    _KIND_ZH = {
        "voice": "语音",
        "image": "图片",
        "video": "视频",
        "oa": "公众号",
        "doc": "企微文档",
        "channel": "视频号",
        "wxa": "小程序",
        "text": "文字",
    }

    def _append_captured_row(self, kind: str, payload: dict, *, name: str = "", saved: bool = False) -> None:
        item_id = str(payload.get("id", "")).strip()
        if not item_id:
            return
        ts_iso = datetime.now().strftime("%m-%d %H:%M:%S")
        display_name = name.strip() or self._compute_placeholder(kind)
        extra = self._extra_desc(kind, payload)
        self._session_rows[item_id] = {
            "kind": kind,
            "name": display_name,
            "extra": extra,
            "ts": ts_iso,
            "saved": bool(saved),
            "source": dict(payload),
        }
        self._render_session_panel()
        # 选中最新加的行，方便用户直接按 F2 或双击命名
        for r in range(self._captured_table.rowCount()):
            it = self._captured_table.item(r, self._COL_NAME)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == item_id:
                self._captured_table.setCurrentCell(r, self._COL_NAME)
                break

    def _compute_placeholder(self, kind: str) -> str:
        # 本轮同类型条目倒序编号
        same_kind = [tid for tid, row in self._session_rows.items() if row["kind"] == kind]
        idx = len(same_kind) + 1  # 加一是因为当前这条还没写入 session_rows
        return f"未命名 #{idx:03d}"

    def _extra_desc(self, kind: str, payload: dict) -> str:
        if kind == "voice":
            d = int(payload.get("duration") or 0)
            return f"{d}s" if d > 0 else ""
        if kind == "image":
            sz = int(payload.get("size") or -1)
            return f"{sz} B" if sz > 0 else ""
        if kind == "video":
            sz = int(payload.get("size") or -1)
            return f"{sz} B" if sz > 0 else ""
        if kind in {"oa", "doc", "channel"}:
            title = str(payload.get("title") or "").strip()
            return title[:40] or str(payload.get("url", ""))[:40]
        if kind == "wxa":
            title = str(payload.get("title") or "").strip()
            app_name = str(payload.get("appName") or "").strip()
            return title[:40] or app_name[:40]
        if kind == "text":
            preview = str(payload.get("preview") or payload.get("body") or "").strip()
            return preview[:20]
        return ""

    def _render_session_panel(self) -> None:
        self._session_updating = True
        try:
            # 保持插入顺序显示（最早捕获在最上），编号已在 _compute_placeholder 定
            items = list(self._session_rows.items())
            self._captured_table.setRowCount(len(items))
            for r, (item_id, row) in enumerate(items):
                self._captured_table.setItem(r, self._COL_KIND, self._readonly(self._KIND_ZH.get(row["kind"], row["kind"])))
                name_item = QTableWidgetItem(row["name"])
                name_item.setData(Qt.ItemDataRole.UserRole, item_id)
                name_item.setData(Qt.ItemDataRole.UserRole + 1, row["kind"])
                self._captured_table.setItem(r, self._COL_NAME, name_item)
                self._captured_table.setItem(r, self._COL_EXTRA, self._readonly(row["extra"]))
                self._captured_table.setItem(r, self._COL_TIME, self._readonly(row["ts"]))
                self._captured_table.setItem(
                    r,
                    self._COL_STATUS,
                    self._readonly("✓ 已保存" if row.get("saved") else "待命名，尚未保存"),
                )
        finally:
            self._session_updating = False

    def _on_captured_cell_double_clicked(self, row: int, column: int) -> None:
        if column == self._COL_NAME:
            item = self._captured_table.item(row, self._COL_NAME)
            if item is not None:
                self._captured_table.editItem(item)
            return
        self._open_captured_detail(row)

    def _on_captured_item_changed(self, item: QTableWidgetItem) -> None:
        if self._session_updating:
            return
        if item.column() != self._COL_NAME:
            return
        item_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        kind = str(item.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
        new_name = item.text().strip()
        if not item_id or not kind:
            return
        # 拒绝把占位串 `未命名 #NNN` 落到 store（用户没实际改）
        if not new_name or new_name.startswith("未命名 #"):
            return
        conflict = asset_name_conflict(
            self._account_id,
            canon_asset_type(kind),
            new_name,
            exclude_item_id=item_id,
        )
        if conflict:
            QMessageBox.warning(self, "名称重复", conflict)
            self._render_session_panel()
            return
        try:
            self._rename_asset(kind, item_id, new_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("命名素材失败 kind={} item_id={} err={}", kind, item_id, exc)
            QMessageBox.warning(self, "命名失败", "素材命名失败，请稍后重试。")
            return
        if item_id in self._session_rows:
            self._session_rows[item_id]["name"] = new_name
            self._session_rows[item_id]["saved"] = True
        self._render_session_panel()
        self.assetsChanged.emit()

    def _rename_asset(self, kind: str, item_id: str, new_name: str) -> None:
        if kind == "voice":
            lib = VoiceLibrary.load(account_id=self._account_id)
            lib.update_note(item_id, new_name)
            lib.save()
            return
        if kind == "text":
            lib = VoiceLibrary.load(account_id=self._account_id)
            lib.update_text_template(item_id, name=new_name)
            lib.save()
            return
        module = {
            "image": image_tpl_mod,
            "video": video_tpl_mod,
            "oa": oa_tpl_mod,
            "doc": oa_tpl_mod,
            "channel": oa_tpl_mod,
            "wxa": wxa_tpl_mod,
        }.get(kind)
        if module is None:
            raise ValueError(f"未知素材类型: {kind}")
        payload = module.load_template(item_id, account_id=self._account_id)
        payload["name"] = new_name
        payload["unnamed"] = False
        module.save_template(payload, template_id=item_id, account_id=self._account_id, pending=False)

    def _clear_session_panel(self) -> None:
        self._session_rows.clear()
        self._session_updating = True
        try:
            self._captured_table.setRowCount(0)
        finally:
            self._session_updating = False

    def _captured_id_at(self, row: int) -> str:
        item = self._captured_table.item(row, self._COL_NAME)
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

    def _captured_preview_payload(self, item_id: str) -> dict[str, Any]:
        row = self._session_rows.get(item_id) or {}
        kind = str(row.get("kind", "")).strip()
        src = row.get("source") if isinstance(row.get("source"), dict) else {}
        name = str(row.get("name", "")).strip() or "未命名"
        payload: dict[str, Any] = {
            "asset_type": kind,
            "item_id": item_id,
            "name": name,
        }
        if kind == "voice":
            payload["path"] = str(src.get("dstPath") or src.get("file") or "")
            payload["file"] = str(src.get("file") or "")
        elif kind == "image":
            payload["path"] = str(src.get("dstPath") or "")
        elif kind == "video":
            payload["path"] = str(src.get("dstPath") or "")
            payload["thumb_path"] = str(src.get("thumbPath") or "")
        elif kind in {"oa", "doc", "channel"}:
            payload["oa_title"] = str(src.get("title") or "")
            payload["oa_desc"] = str(src.get("desc") or "")
            payload["oa_url"] = str(src.get("url") or "")
            payload["oa_source"] = str(src.get("appname") or src.get("sourcename") or "")
        elif kind == "wxa":
            payload["wxa_title"] = str(src.get("title") or "")
            payload["wxa_app"] = str(src.get("appName") or "")
            payload["wxa_user"] = str(src.get("username") or "")
        elif kind == "text":
            preview = str(src.get("preview") or src.get("body") or row.get("extra") or "").strip()
            payload["variants"] = [preview] if preview else []
        return payload

    def _open_captured_detail(self, row: int) -> None:
        item_id = self._captured_id_at(row)
        if not item_id or item_id not in self._session_rows:
            return
        payload = self._captured_preview_payload(item_id)
        name = str(payload.get("name", "")).strip() or "未命名"
        provider = self._preview_service_provider
        if provider is None:
            QMessageBox.information(
                self,
                "素材详情",
                f"类型：{asset_type_to_zh(str(payload.get('asset_type', '')))}\n名称：{name}",
            )
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"素材详情 · {name}")
        dlg.resize(520, 560)
        layout = QVBoxLayout(dlg)
        panel = AssetPreviewPanel(preview_service_provider=provider)
        panel.set_asset(payload)
        layout.addWidget(panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()

    # ------------------------------------------------------------------
    # 右键菜单：从本轮列表移除，不删素材库
    # ------------------------------------------------------------------
    def _on_captured_context_menu(self, pos) -> None:
        idx = self._captured_table.indexAt(pos)
        if not idx.isValid():
            return
        prepare_context_selection(self._captured_table, idx)
        selected_ids = self._selected_captured_ids()
        if not selected_ids:
            return
        menu = QMenu(self)
        add_copy_action(menu, self._captured_table)
        act_rename = menu.addAction("重命名")
        act_detail = menu.addAction("查看详情")
        delete_label = f"从本轮列表移除（{len(selected_ids)} 条）" if len(selected_ids) > 1 else "从本轮列表移除"
        act_delete = menu.addAction(delete_label)
        chosen = menu.exec(self._captured_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_detail:
            self._open_captured_detail(idx.row())
            return
        if chosen == act_rename:
            # 多选时重命名只改当前行
            name_item = self._captured_table.item(idx.row(), self._COL_NAME)
            if name_item is not None:
                self._captured_table.editItem(name_item)
            return
        if chosen == act_delete:
            # Step 4: 子模块删除——只从本轮列表移除，不删素材库
            for tid in selected_ids:
                self._session_rows.pop(tid, None)
            self._render_session_panel()
            return

    def _selected_captured_ids(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in self._captured_table.selectedItems():
            r = it.row()
            name_item = self._captured_table.item(r, self._COL_NAME)
            if name_item is None:
                continue
            tid = str(name_item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if tid and tid not in seen:
                seen.add(tid)
                out.append(tid)
        return out

    def remove_items(self, deleted: list[tuple[str, str]]) -> None:
        """Step 4: 主模块删除时，从本轮已捕获列表同步移除对应条目。"""
        if not deleted:
            return
        deleted_ids = {str(row[1]).strip() for row in deleted if len(row) > 1 and str(row[1]).strip()}
        changed = False
        for tid in list(self._session_rows):
            if tid in deleted_ids:
                self._session_rows.pop(tid, None)
                changed = True
        if changed:
            self._render_session_panel()

    def _delete_captured_items(self, item_ids: list[str]) -> list[str]:
        """按 kind 分派删除；返回失败信息列表。"""
        errors: list[str] = []
        voice_lib_needed = False
        text_lib_needed = False
        # 先分组 kind，避免多次 load VoiceLibrary
        groups: dict[str, list[str]] = {}
        for tid in item_ids:
            row = self._session_rows.get(tid)
            if row is None:
                continue
            groups.setdefault(str(row.get("kind", "")), []).append(tid)
        lib: VoiceLibrary | None = None

        def _lib() -> VoiceLibrary:
            nonlocal lib
            if lib is None:
                lib = VoiceLibrary.load(account_id=self._account_id)
            return lib

        for kind, ids in groups.items():
            if kind == "voice":
                for tid in ids:
                    try:
                        _lib().delete(tid)
                        voice_lib_needed = True
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"voice #{tid[:8]}: {exc}")
                continue
            if kind == "text":
                for tid in ids:
                    try:
                        _lib().delete_text_template(tid)
                        text_lib_needed = True
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{kind} #{tid[:8]}: {exc}")
                continue
            module = {
                "image": image_tpl_mod,
                "video": video_tpl_mod,
                "oa": oa_tpl_mod,
                "doc": oa_tpl_mod,
                "channel": oa_tpl_mod,
                "wxa": wxa_tpl_mod,
            }.get(kind)
            if module is None:
                errors.append(f"未知类型 {kind}")
                continue
            # image/video/oa 落盘为 <template_root>/<tid>.json，直接 unlink
            root_dir = module.template_root(self._account_id)
            for tid in ids:
                fp = root_dir / f"{tid}.json"
                try:
                    fp.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{kind} #{tid[:8]}: {exc}")

        if voice_lib_needed or text_lib_needed:
            try:
                _lib().save()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"保存 VoiceLibrary 失败: {exc}")

        # 从本轮面板中移除（哪怕底层删除失败也移除，避免用户困惑）
        for tid in item_ids:
            self._session_rows.pop(tid, None)
        self._render_session_panel()
        return errors

    @staticmethod
    def _readonly(value: str) -> QTableWidgetItem:
        it = QTableWidgetItem(value)
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return it
