"""增加素材：F3.5 单一采集口（voice + image + video + oa）+ 本轮已捕获面板。

改动路线：
- F1.6 起 voice 走 `fta_capture_lite`；
- F3.1/F3.4 加时长选择 + `CaptureClient` 起停 + 错误弹窗；
- **F3.5 合并**：`fta_capture_lite` 单一 attach 收全 4 类，删掉 image/video/oa 各自的
  probe 子进程与 kind picker；wxa 因缺 mType 证据暂缓合并，保留 legacy 独立入口。
- 新增 **"本轮已捕获" 面板**：捕获事件到达即入表 + 双击名字列就地重命名 →
  写回各自 store 并去掉 `unnamed` flag → 本轮结束自动清空，条目仍留在
  "已保存的素材"（未命名的以 `未命名 #NNN` 展示，F3.2 扩展）。
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.broadcast import image_templates as image_tpl_mod
from app.broadcast import oa_templates as oa_tpl_mod
from app.broadcast import video_templates as video_tpl_mod
from app.broadcast import wxa_templates as wxa_tpl_mod
from app.broadcast.capture_client import CaptureClient
from app.broadcast.library import VoiceLibrary
from app.broadcast.paths import account_voice_index_path
from app.config import resolve_path
from gui.widgets.pending_template_dialog import PendingTemplateDialog


class _CaptureBridge(QObject):
    """把 :class:`CaptureClient` 工作线程回调转成 Qt signal，跨线程安全。"""

    event = Signal(dict)
    exited = Signal(int)


class LibraryIngestTab(QWidget):
    """FTA 素材采集入口（F3.5 单一采集口）。"""

    assetsChanged = Signal()
    unnamedCountChanged = Signal(int)

    _FTA_REMOTE_ID = "10006"
    _DURATION_OPTIONS = (5, 10, 30)  # 分钟

    # 已捕获面板列 index
    _COL_KIND = 0
    _COL_NAME = 1
    _COL_EXTRA = 2
    _COL_TIME = 3

    def __init__(self) -> None:
        super().__init__()
        self._account_id = "default"

        # F3.5：单一 CaptureClient 覆盖 voice/image/video/oa
        self._client: CaptureClient | None = None
        self._bridge: _CaptureBridge | None = None
        self._started_ms: float = 0.0
        self._deadline_ms: float = 0.0
        self._counts: dict[str, int] = {"voice": 0, "image": 0, "video": 0, "oa": 0}
        # F3.4：Windows 上 SIGTERM 强杀 exit=1 与真异常无法从 code 区分，用旗标区分
        self._user_stopped: bool = False
        # 本轮已捕获条目：{template_id: {kind, name, extra, ts_iso, row_idx}}
        self._session_rows: dict[str, dict[str, Any]] = {}
        self._session_updating = False  # 防 itemChanged 递归

        # wxa 保留 legacy probe（缺 mType 证据，本轮不合并）
        self._wxa_process: QProcess | None = None

        # pending 目录轮询（历史遗留 pending 文件 + wxa 走 pending 流程）
        self._pending_seen: set[str] = set()
        self._pending_dialog_open = False
        self._variant_updating = False

        self._build_ui()
        self.assetsChanged.connect(self._refresh_variant_picker)

        self._pending_timer = QTimer(self)
        self._pending_timer.setInterval(3000)
        self._pending_timer.timeout.connect(self._poll_pending_templates)
        self._pending_timer.start()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)

    # ==================================================================
    # UI build
    # ==================================================================
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        hint = QLabel(
            "开启后，对 FTA 发送的语音 / 图片 / 视频 / 公众号卡片会实时进入下方"
            "「本轮已捕获」列表。双击「名字」列即可命名；关闭采集后条目自动"
            "归档到「已保存的素材」子页。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        dur_bar = QHBoxLayout()
        dur_bar.addWidget(QLabel("采集时长"))
        self._dur_pick = QComboBox()
        for m in self._DURATION_OPTIONS:
            self._dur_pick.addItem(f"{m} 分钟", m)
        self._dur_pick.setCurrentIndex(1)  # 默认 10 分钟
        dur_bar.addWidget(self._dur_pick)
        dur_bar.addStretch(1)
        root.addLayout(dur_bar)

        self._btn_toggle = QPushButton("开始录素材")
        self._btn_toggle.setCheckable(True)
        self._btn_toggle.clicked.connect(self._toggle_capture)
        root.addWidget(self._btn_toggle)

        self._status_label = QLabel("未运行")
        root.addWidget(self._status_label)

        captured_wrap = QWidget()
        captured_layout = QVBoxLayout(captured_wrap)
        captured_layout.setContentsMargins(0, 0, 0, 0)
        captured_layout.addWidget(QLabel("本轮已捕获（双击「名字」列命名）："))
        self._captured_table = QTableWidget(0, 4)
        self._captured_table.setHorizontalHeaderLabels(["类型", "名字", "详情", "时间"])
        header = self._captured_table.horizontalHeader()
        header.setSectionResizeMode(self._COL_KIND, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_EXTRA, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        self._captured_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 支持鼠标长拉 / Ctrl+Click 多选，右键弹菜单删除
        self._captured_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._captured_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._captured_table.itemChanged.connect(self._on_captured_item_changed)
        self._captured_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._captured_table.customContextMenuRequested.connect(self._on_captured_context_menu)
        captured_layout.addWidget(self._captured_table, 1)

        # 手工创建区（F3.6）：文字 / 文字变体 / 小程序链接
        # 与 FTA 抓取共用同一「本轮已捕获」面板，保持视觉与操作一致。
        manual_bar = QHBoxLayout()
        manual_bar.addWidget(self._build_text_group(), 1)
        manual_bar.addWidget(self._build_variant_group(), 1)
        manual_bar.addWidget(self._build_link_group(), 1)

        tool_bar = QHBoxLayout()
        self._btn_poll_now = QPushButton("立即检查待确认（wxa/legacy）")
        self._btn_poll_now.clicked.connect(self._poll_pending_templates)
        tool_bar.addWidget(self._btn_poll_now)
        self._btn_wxa = QPushButton("wxa 采集（legacy · 独立子进程）")
        self._btn_wxa.setCheckable(True)
        self._btn_wxa.clicked.connect(self._toggle_wxa)
        tool_bar.addWidget(self._btn_wxa)
        tool_bar.addStretch(1)

        bottom_wrap = QWidget()
        bottom_layout = QVBoxLayout(bottom_wrap)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addLayout(manual_bar)
        bottom_layout.addLayout(tool_bar)

        bottom_scroll = QScrollArea()
        bottom_scroll.setWidgetResizable(True)
        bottom_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bottom_scroll.setWidget(bottom_wrap)
        captured_wrap.setMinimumHeight(72)
        bottom_scroll.setMinimumHeight(72)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(captured_wrap)
        splitter.addWidget(bottom_scroll)
        splitter.setChildrenCollapsible(True)
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, True)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 280])
        root.addWidget(splitter, 1)

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
        self._pending_seen.clear()
        self._refresh_variant_picker()

    def stop_all(self) -> None:
        self._stop_capture()
        self._stop_wxa_process()
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
        self._counts = {"voice": 0, "image": 0, "video": 0, "oa": 0}
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
            tail = "\n".join((client.stats.stderr_tail if client else [])[-20:])
            logger.warning("FTA 采集异常退出 code={} stderr_tail={}", code, tail)
            QMessageBox.warning(
                self,
                "FTA 采集异常退出",
                "采集进程异常退出，请重新开始采集。",
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
        "text": "文字",
        "link": "小程序链接",
    }

    def _append_captured_row(self, kind: str, payload: dict, *, name: str = "") -> None:
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
        if kind == "oa":
            title = str(payload.get("title") or "").strip()
            return title[:40] or str(payload.get("url", ""))[:40]
        if kind == "text":
            # 手工创建：payload 里带首条变体（`preview`）
            preview = str(payload.get("preview") or "").strip()
            return preview[:20]
        if kind == "link":
            return str(payload.get("url") or "").strip()[:40]
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
        finally:
            self._session_updating = False

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
        try:
            self._rename_asset(kind, item_id, new_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("命名素材失败 kind={} item_id={} err={}", kind, item_id, exc)
            QMessageBox.warning(self, "命名失败", "素材命名失败，请稍后重试。")
            return
        # 更新本地 session 缓存
        if item_id in self._session_rows:
            self._session_rows[item_id]["name"] = new_name
        self.assetsChanged.emit()

    def _rename_asset(self, kind: str, item_id: str, new_name: str) -> None:
        if kind == "voice":
            lib = VoiceLibrary.load(account_id=self._account_id)
            lib.update_note(item_id, new_name)
            lib.save()
            return
        if kind in {"text", "link"}:
            # 手工创建的 TextTemplate（文字 / 小程序链接）改名
            lib = VoiceLibrary.load(account_id=self._account_id)
            lib.update_text_template(item_id, name=new_name)
            lib.save()
            return
        # image / video / oa：load → set name + 去 unnamed flag → save 覆盖
        module = {
            "image": image_tpl_mod,
            "video": video_tpl_mod,
            "oa": oa_tpl_mod,
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

    # ------------------------------------------------------------------
    # 右键菜单：批量删除本轮已捕获（同步删底层素材库）
    # ------------------------------------------------------------------
    def _on_captured_context_menu(self, pos) -> None:
        idx = self._captured_table.indexAt(pos)
        if not idx.isValid():
            return
        selected_ids = self._selected_captured_ids()
        if not selected_ids:
            return
        menu = QMenu(self)
        act = menu.addAction(f"删除选中素材（{len(selected_ids)} 条）")
        chosen = menu.exec(self._captured_table.viewport().mapToGlobal(pos))
        if chosen != act:
            return
        if QMessageBox.question(
            self,
            "确认删除",
            f"将从「已保存的素材」里彻底删除 {len(selected_ids)} 条素材，操作不可撤销。是否继续？",
        ) != QMessageBox.StandardButton.Yes:
            return
        errors = self._delete_captured_items(selected_ids)
        if errors:
            logger.warning("删除部分素材失败 errors={}", errors)
            QMessageBox.warning(
                self, "部分素材删除失败",
                "部分素材删除失败，请稍后重试。",
            )
        self.assetsChanged.emit()

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
            if kind in {"text", "link"}:
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

    # ==================================================================
    # 手工创建：文字 / 小程序链接（F3.6）
    # ------------------------------------------------------------------
    # 本质都是 TextTemplate：text 走多变体 QTextEdit；link 走单条 URL 校验
    # http/https。保存后直接 emit 到已捕获面板 + assetsChanged 让上层刷库。
    # ==================================================================
    def _build_text_group(self) -> QGroupBox:
        box = QGroupBox("增加文字")
        layout = QVBoxLayout(box)
        self._text_name = QLineEdit()
        self._text_name.setPlaceholderText("名字 *（用于素材库检索）")
        self._text_variants = QTextEdit()
        self._text_variants.setPlaceholderText("文字变体，每行 1 条；发送时随机抽 1 条")
        self._text_variants.setAcceptRichText(False)
        self._text_tags = QLineEdit()
        self._text_tags.setPlaceholderText("标签（可选，顿号或逗号分隔）")
        self._btn_save_text = QPushButton("保存文字")
        self._btn_save_text.setEnabled(False)
        layout.addWidget(self._text_name)
        layout.addWidget(self._text_variants)
        layout.addWidget(self._text_tags)
        layout.addWidget(self._btn_save_text)

        self._text_name.textChanged.connect(self._refresh_text_button_state)
        self._text_variants.textChanged.connect(self._refresh_text_button_state)
        self._btn_save_text.clicked.connect(self._save_text_material)
        return box

    def _build_link_group(self) -> QGroupBox:
        box = QGroupBox("增加小程序链接")
        layout = QVBoxLayout(box)
        self._link_name = QLineEdit()
        self._link_name.setPlaceholderText("名字 *（用于素材库检索）")
        self._link_url = QLineEdit()
        self._link_url.setPlaceholderText("URL *（http/https/自定义 scheme 均可）")
        self._link_tags = QLineEdit()
        self._link_tags.setPlaceholderText("标签（可选，顿号或逗号分隔）")
        self._btn_save_link = QPushButton("保存小程序链接")
        self._btn_save_link.setEnabled(False)
        layout.addWidget(self._link_name)
        layout.addWidget(self._link_url)
        layout.addWidget(self._link_tags)
        layout.addWidget(self._btn_save_link)
        # 高度上和文字组视觉大致对齐（多的一行 spacer）
        layout.addStretch(1)

        self._link_name.textChanged.connect(self._refresh_link_button_state)
        self._link_url.textChanged.connect(self._refresh_link_button_state)
        self._btn_save_link.clicked.connect(self._save_link_material)
        return box

    def _build_variant_group(self) -> QGroupBox:
        box = QGroupBox("增加文字变体")
        layout = QVBoxLayout(box)
        self._variant_pick = QComboBox()
        self._variant_list = QListWidget()
        self._variant_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._variant_list.customContextMenuRequested.connect(self._on_variant_list_context_menu)
        self._variant_new_lines = QTextEdit()
        self._variant_new_lines.setPlaceholderText("新变体，每行 1 条")
        self._variant_new_lines.setAcceptRichText(False)
        self._btn_append_variants = QPushButton("追加变体")
        self._btn_append_variants.setEnabled(False)
        layout.addWidget(self._variant_pick)
        layout.addWidget(QLabel("已有变体（右键可删除）："))
        layout.addWidget(self._variant_list)
        layout.addWidget(self._variant_new_lines)
        layout.addWidget(self._btn_append_variants)

        self._variant_pick.currentIndexChanged.connect(self._on_variant_pick_changed)
        self._variant_new_lines.textChanged.connect(self._refresh_variant_append_state)
        self._btn_append_variants.clicked.connect(self._append_text_variants)
        self._refresh_variant_picker()
        return box

    def _refresh_variant_picker(self) -> None:
        current_id = self._current_variant_template_id()
        self._variant_pick.blockSignals(True)
        try:
            self._variant_pick.clear()
            try:
                lib = VoiceLibrary.load(account_id=self._account_id)
                rows = [
                    tpl for tpl in lib.list_text_templates()
                    if getattr(tpl, "kind", "text") == "text"
                ]
            except Exception:  # noqa: BLE001
                rows = []
            for tpl in rows:
                label = f"{tpl.name}（{tpl.id[:8]}）"
                self._variant_pick.addItem(label, tpl.id)
            if current_id:
                idx = self._variant_pick.findData(current_id)
                if idx >= 0:
                    self._variant_pick.setCurrentIndex(idx)
        finally:
            self._variant_pick.blockSignals(False)
        self._on_variant_pick_changed(self._variant_pick.currentIndex())

    def _current_variant_template_id(self) -> str:
        data = self._variant_pick.currentData()
        return str(data).strip() if isinstance(data, str) else ""

    def _on_variant_pick_changed(self, _index: int) -> None:
        template_id = self._current_variant_template_id()
        variants: list[str] = []
        if template_id:
            try:
                lib = VoiceLibrary.load(account_id=self._account_id)
                template = lib.get_text_template(template_id)
                variants = list(template.variants)
            except Exception:  # noqa: BLE001
                variants = []
        self._render_variant_list(variants)
        self._refresh_variant_append_state()

    def _render_variant_list(self, variants: list[str]) -> None:
        self._variant_updating = True
        try:
            self._variant_list.clear()
            for text in variants:
                item = QListWidgetItem(text)
                self._variant_list.addItem(item)
        finally:
            self._variant_updating = False

    def _refresh_variant_append_state(self) -> None:
        has_template = bool(self._current_variant_template_id())
        has_new = bool(self._split_variants(self._variant_new_lines.toPlainText()))
        self._btn_append_variants.setEnabled(has_template and has_new)

    def _persist_variant_list(self, template_id: str, variants: list[str]) -> bool:
        try:
            lib = VoiceLibrary.load(account_id=self._account_id)
            lib.update_text_template(template_id, variants=variants)
            lib.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存文字变体失败 template_id={} err={}", template_id, exc)
            QMessageBox.warning(self, "保存变体失败", "保存失败，请稍后重试。")
            return False
        self._render_variant_list(variants)
        self.assetsChanged.emit()
        return True

    def _append_text_variants(self) -> None:
        template_id = self._current_variant_template_id()
        if not template_id:
            return
        new_lines = self._split_variants(self._variant_new_lines.toPlainText())
        if not new_lines:
            return
        try:
            lib = VoiceLibrary.load(account_id=self._account_id)
            template = lib.get_text_template(template_id)
            merged = list(template.variants) + new_lines
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取文字素材失败 template_id={} err={}", template_id, exc)
            QMessageBox.warning(self, "读取素材失败", "读取素材失败，请刷新后重试。")
            return
        if not self._persist_variant_list(template_id, merged):
            return
        self._variant_new_lines.clear()
        self._refresh_variant_append_state()

    def _on_variant_list_context_menu(self, pos) -> None:
        item = self._variant_list.itemAt(pos)
        if item is None:
            return
        template_id = self._current_variant_template_id()
        if not template_id:
            return
        menu = QMenu(self)
        act = menu.addAction("删除此变体")
        chosen = menu.exec(self._variant_list.viewport().mapToGlobal(pos))
        if chosen != act:
            return
        try:
            lib = VoiceLibrary.load(account_id=self._account_id)
            template = lib.get_text_template(template_id)
            current = list(template.variants)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取文字素材失败 template_id={} err={}", template_id, exc)
            QMessageBox.warning(self, "读取素材失败", "读取素材失败，请刷新后重试。")
            return
        row = self._variant_list.row(item)
        if row < 0 or row >= len(current):
            return
        if len(current) <= 1:
            QMessageBox.information(self, "无法删除", "文字素材至少需要保留 1 条变体。")
            return
        updated = current[:row] + current[row + 1 :]
        self._persist_variant_list(template_id, updated)

    def _refresh_text_button_state(self) -> None:
        has_name = bool(self._text_name.text().strip())
        has_body = bool(self._text_variants.toPlainText().strip())
        self._btn_save_text.setEnabled(has_name and has_body)

    def _refresh_link_button_state(self) -> None:
        has_name = bool(self._link_name.text().strip())
        has_url = bool(self._link_url.text().strip())
        self._btn_save_link.setEnabled(has_name and has_url)

    @staticmethod
    def _split_tags(raw: str) -> list[str]:
        cleaned = raw.replace("、", ",")
        return [t.strip() for t in cleaned.split(",") if t.strip()]

    @staticmethod
    def _split_variants(raw: str) -> list[str]:
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _save_text_material(self) -> None:
        name = self._text_name.text().strip()
        variants = self._split_variants(self._text_variants.toPlainText())
        tags = self._split_tags(self._text_tags.text())
        if not name or not variants:
            return
        template_id = str(uuid.uuid4())
        try:
            lib = VoiceLibrary.load(account_id=self._account_id)
            lib.add_text_template(
                name=name, variants=variants, tags=tags,
                template_id=template_id, kind="text",
            )
            lib.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存文字素材失败 account_id={} err={}", self._account_id, exc)
            QMessageBox.warning(self, "保存文字失败", "保存失败，请稍后重试。")
            return
        self._append_captured_row("text", {"id": template_id, "preview": variants[0]}, name=name)
        self._text_name.clear()
        self._text_variants.clear()
        self._text_tags.clear()
        self._refresh_text_button_state()
        self.assetsChanged.emit()

    def _save_link_material(self) -> None:
        name = self._link_name.text().strip()
        url = self._link_url.text().strip()
        tags = self._split_tags(self._link_tags.text())
        if not name or not url:
            return
        # 放宽校验：链接内容用户自定，只要非空即可（支持自定义 scheme 如 weixin://、
        # 小程序 URL scheme 等；发送侧当 text 直接下发文本）。
        template_id = str(uuid.uuid4())
        try:
            lib = VoiceLibrary.load(account_id=self._account_id)
            lib.add_link_template(name=name, url=url, tags=tags, template_id=template_id)
            lib.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存小程序链接失败 account_id={} err={}", self._account_id, exc)
            QMessageBox.warning(self, "保存小程序链接失败", "保存失败，请稍后重试。")
            return
        self._append_captured_row("link", {"id": template_id, "url": url}, name=name)
        self._link_name.clear()
        self._link_url.clear()
        self._link_tags.clear()
        self._refresh_link_button_state()
        self.assetsChanged.emit()

    # ==================================================================
    # wxa 独立采集（legacy · 缺 mType 证据，本轮不合并）
    # ==================================================================
    def _toggle_wxa(self, checked: bool) -> None:
        if checked:
            if not self._start_wxa_process():
                self._btn_wxa.setChecked(False)
                return
            self._btn_wxa.setText("关闭 wxa 采集")
        else:
            self._stop_wxa_process()
            self._btn_wxa.setText("wxa 采集（legacy · 独立子进程）")

    def _start_wxa_process(self) -> bool:
        proc = self._wxa_process
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            return True
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        # frozen 下 sys.executable = exe，`-m` 会拉起新 GUI 实例；改走 __runmod__。
        from app.bootstrap.frozen_dispatch import build_python_module_cmd

        module_args = [
            "--account-id", self._account_id,
            "--fta-remote-id", self._FTA_REMOTE_ID,
            "--timeout-seconds", "3600",
        ]
        if bool(getattr(sys, "frozen", False)):
            full_cmd = build_python_module_cmd(
                "runtime.wework_re_android.probe_fta_wxa_pipeline", *module_args
            )
            proc.setProgram(full_cmd[0])
            proc.setArguments(full_cmd[1:])
        else:
            proc.setProgram(sys.executable)
            proc.setArguments([
                "-u", "-m", "runtime.wework_re_android.probe_fta_wxa_pipeline",
                *module_args,
            ])
        proc.setWorkingDirectory(str(resolve_path(Path("."))))
        proc.finished.connect(lambda _c, _s: self._on_wxa_finished())
        proc.errorOccurred.connect(lambda _e: self._on_wxa_finished())
        proc.readyReadStandardOutput.connect(proc.readAllStandardOutput)
        proc.start()
        if not proc.waitForStarted(3000):
            QMessageBox.warning(self, "wxa 采集启动失败", "子进程未能在 3s 内启动。")
            return False
        self._wxa_process = proc
        return True

    def _stop_wxa_process(self) -> None:
        proc = self._wxa_process
        if proc is None:
            return
        if proc.state() != QProcess.ProcessState.NotRunning:
            proc.terminate()
            if not proc.waitForFinished(3000):
                proc.kill()
                proc.waitForFinished(1000)
        self._wxa_process = None

    def _on_wxa_finished(self) -> None:
        self._wxa_process = None
        self._btn_wxa.setChecked(False)
        self._btn_wxa.setText("wxa 采集（legacy · 独立子进程）")

    # ==================================================================
    # Pending 轮询（wxa + 历史遗留 pending 文件）
    # ==================================================================
    def _poll_pending_templates(self) -> None:
        if self._pending_dialog_open:
            return
        pending: list[tuple[str, Path]] = []
        try:
            for p in oa_tpl_mod.list_pending(self._account_id):
                pending.append(("oa", p))
            for p in wxa_tpl_mod.list_pending(self._account_id):
                pending.append(("wxa", p))
            for p in image_tpl_mod.list_pending(self._account_id):
                pending.append(("image", p))
            for p in video_tpl_mod.list_pending(self._account_id):
                pending.append(("video", p))
        except Exception:
            return
        alive = {str(p) for _, p in pending}
        self._pending_seen &= alive
        for kind, path in pending:
            key = str(path)
            if key in self._pending_seen:
                continue
            self._show_pending_dialog(kind, path)
            return
        self.unnamedCountChanged.emit(self._read_unnamed_count())

    def _show_pending_dialog(self, kind: str, path: Path) -> None:
        module = {
            "oa": oa_tpl_mod,
            "wxa": wxa_tpl_mod,
            "image": image_tpl_mod,
            "video": video_tpl_mod,
        }[kind]
        payload = PendingTemplateDialog.load_payload(path)
        if not payload:
            module.discard_pending(path)
            return
        self._pending_dialog_open = True
        dlg = PendingTemplateDialog(path, payload, kind=kind, parent=self)
        try:
            dlg.exec()
            decision = dlg.decision()
            if decision == PendingTemplateDialog.RESULT_SAVE:
                try:
                    module.commit_pending(
                        path, name=dlg.entered_name(), tags=dlg.entered_tags(), account_id=self._account_id
                    )
                    self.assetsChanged.emit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("提交 pending 素材失败 kind={} path={} err={}", kind, path, exc)
                    QMessageBox.warning(self, "保存失败", "保存失败，请稍后重试。")
            elif decision == PendingTemplateDialog.RESULT_DISCARD:
                module.discard_pending(path)
            else:
                self._pending_seen.add(str(path))
        finally:
            self._pending_dialog_open = False
            self.unnamedCountChanged.emit(self._read_unnamed_count())

    def _read_unnamed_count(self) -> int:
        count = 0
        try:
            lib = VoiceLibrary.load(account_id=self._account_id)
            count += sum(1 for row in lib.list() if not row.note.strip() and not row.label.strip())
        except Exception:
            pass
        # image/video/oa：payload["unnamed"] == True 计入
        for mod in (image_tpl_mod, video_tpl_mod, oa_tpl_mod):
            try:
                for fp in mod.list_templates(self._account_id):
                    payload = mod.load_template(str(fp), account_id=self._account_id)
                    if bool(payload.get("unnamed")):
                        count += 1
            except Exception:
                continue
        return count
