"""顶部账号栏 —— 一机一号版。

设计目标（2026-09-22 简化）：
    * 启动时探测一次企微 vid（走 adb shared_prefs，无 Frida 注入 = 无风控风险）
    * 顶栏只显示当前 vid + 一个「刷新账号」按钮
    * 不再提供多账号下拉、切换、初始化引导、Frida 拉昵称等能力
    * 探测失败时账号栏红字提示 + 发信号让主窗禁用其他 tab（防呆）
    * 探测到与当前不同的 vid → 弹确认对话框（防误操作），Yes 才切

删除的老功能（如果你在别处看到还有残留调用，说明遗漏了同步）：
    * ``_combo``（多账号下拉）、``list_accounts`` / ``load_last_active_account``
    * ``_btn_init`` / ``_on_init_account`` / ``prompt_init_account`` / ``_manual_init_account``
    * ``_AliasRefreshWorker`` / ``refresh_account_display_name``
    * ``_passive_probe_timer``（30s 被动探测，早已未 start，直接下线）
    * ``hydrate_materials``（跨账号迁移素材）
"""

from __future__ import annotations

from loguru import logger
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QWidget,
)

from app.broadcast.account import get_current_account_id, switch_account
from app.broadcast.paths import DEFAULT_ACCOUNT_ID, normalize_account_id


class _AccountProbeWorker(QObject):
    """后台线程里跑 adb shared_prefs 探针。不走 Frida，无风控风险。"""

    finished = Signal(str, str)  # account_id, error_message

    def __init__(self, timeout_seconds: float = 12.0) -> None:
        super().__init__()
        self._timeout = timeout_seconds

    def run(self) -> None:
        try:
            # use_env=False：不看 TESTVOICE_ACCOUNT_ID 缓存，强制现场读 shared_prefs。
            # allow_frida_fallback=False：绝不 fallback 到 Frida 探针；宁可探不到也不冒
            # 风控风险。"一机一号" 模式下账号是靠模拟器登录状态定义的，读不到就是没登录。
            account_id = get_current_account_id(
                timeout_seconds=self._timeout,
                use_env=False,
                allow_frida_fallback=False,
            )
        except Exception as exc:  # pragma: no cover - 运行时环境相关
            logger.warning("账号探测异常: {}", exc)
            self.finished.emit("", f"探测异常：{exc}")
            return
        if not account_id or account_id == DEFAULT_ACCOUNT_ID:
            self.finished.emit(
                "",
                "未检测到已登录的企微账号。\n\n"
                "请先在 MuMu 模拟器里登录企微（进入主界面），然后再点『刷新账号』重试。",
            )
            return
        self.finished.emit(account_id, "")


class AccountBar(QWidget):
    """顶部账号栏 —— 展示当前 vid + 一个『刷新账号』按钮。"""

    # account_id: 探测到的 vid；detected: True=真实探到，False=空/失败
    accountChanged = Signal(str)
    detectionStateChanged = Signal(bool)  # True=探到有效账号；False=没账号

    def __init__(self) -> None:
        super().__init__()
        # 当前生效的账号（仅由 startup / refresh 探测结果 + rebind 更新）。
        # 与其他 tab 通信仍走 accountChanged 信号，不直接读取。
        self._current_account: str = ""
        self._probe_thread: QThread | None = None
        self._probe_worker: _AccountProbeWorker | None = None
        self._probe_dialog: QProgressDialog | None = None
        self._probe_purpose: str = "startup"  # startup | refresh
        self._build_ui()
        self._render_no_account("正在检测当前企微账号…")

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        self._label_prefix = QLabel("当前账号：")
        self._label_value = QLabel("—")
        # 让 QLabel 支持通过 setStyleSheet 变红/变正常。
        self._label_value.setAutoFillBackground(False)
        self._btn_refresh = QPushButton("刷新账号")
        self._btn_refresh.setToolTip(
            "重新读取 MuMu 模拟器里企微当前登录的账号 vid（无 Frida，无风控风险）"
        )
        root.addWidget(self._label_prefix)
        root.addWidget(self._label_value, 1)
        root.addWidget(self._btn_refresh)
        root.addStretch(0)

        self._btn_refresh.clicked.connect(self._on_refresh_clicked)

    def _render_ok(self, account_id: str) -> None:
        self._label_prefix.setText("当前账号：")
        self._label_value.setText(account_id)
        self._label_value.setStyleSheet("color: #16A34A; font-weight: 600;")
        self._label_value.setToolTip(f"vid: {account_id}\n数据目录: data/{account_id}/")

    def _render_no_account(self, tip: str) -> None:
        self._label_prefix.setText("")
        self._label_value.setText(tip)
        self._label_value.setStyleSheet("color: #DC2626; font-weight: 600;")
        self._label_value.setToolTip("请先在 MuMu 里登录企微，然后点『刷新账号』重试")

    # ---------------- 对外接口 ----------------

    def current_account(self) -> str:
        return self._current_account

    def try_auto_detect_on_startup(self) -> None:
        """GUI 启动时无条件探测一次企微当前登录账号。

        与旧实现不同：不再判断本地有没有历史 data/<vid>/ 目录，
        因为「一机一号」意味着当前企微登录状态就是唯一真相。
        """
        self._start_probe(
            "startup",
            "正在检测企微当前登录账号…\n请稍候（无 Frida、无风控风险）。",
        )

    # ---------------- 探测流程 ----------------

    def _on_refresh_clicked(self) -> None:
        started = self._start_probe(
            "refresh",
            "正在读取企微当前登录账号…\n请确保 MuMu 里企微已登录。",
        )
        if not started:
            QMessageBox.information(
                self,
                "请稍候",
                "已有一次账号探测在进行中，请等它跑完再点。",
            )

    def _start_probe(self, purpose: str, message: str) -> bool:
        if self._probe_thread is not None:
            return False  # 已有探测在跑
        self._probe_purpose = purpose
        self._probe_dialog = QProgressDialog(message, "取消", 0, 0, self)
        self._probe_dialog.setWindowTitle("正在读取企微账号")
        self._probe_dialog.setMinimumDuration(0)
        self._probe_dialog.setCancelButton(None)  # 后台任务禁止中途取消
        self._probe_dialog.show()

        self._probe_thread = QThread(self)
        self._probe_worker = _AccountProbeWorker(timeout_seconds=12.0)
        self._probe_worker.moveToThread(self._probe_thread)
        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.finished.connect(self._on_probe_finished)
        self._probe_worker.finished.connect(self._probe_thread.quit)
        self._probe_thread.finished.connect(self._cleanup_probe_thread)
        self._probe_thread.start()
        return True

    def _cleanup_probe_thread(self) -> None:
        if self._probe_dialog is not None:
            self._probe_dialog.close()
            self._probe_dialog = None
        if self._probe_worker is not None:
            self._probe_worker.deleteLater()
            self._probe_worker = None
        if self._probe_thread is not None:
            self._probe_thread.deleteLater()
            self._probe_thread = None

    def _on_probe_finished(self, account_id: str, error: str) -> None:
        purpose = self._probe_purpose
        if account_id:
            self._handle_detected(account_id, purpose)
            return

        # 探测失败
        logger.warning("[{}] 账号探测失败: {}", purpose, error or "unknown")
        self._render_no_account("未检测到企微账号，请先在 MuMu 登录企微后点『刷新账号』")
        # 把失败状态广播出去（主窗依此禁用业务 tab）
        if self._current_account:
            self._current_account = ""
            self.accountChanged.emit("")
        self.detectionStateChanged.emit(False)

        if purpose == "refresh":
            # 手动刷新失败要给个明确弹窗，否则用户不知道点了没反应
            QMessageBox.information(
                self,
                "未检测到登录账号",
                (error or "未能读取到企微 vid。").strip()
                + "\n\n提示：确认 MuMu 已启动、adb 可连、企微已进入主界面（非登录页）。",
            )
        # startup 失败保持沉默（顶栏红字已经说明状况）

    def _handle_detected(self, account_id: str, purpose: str) -> None:
        normalized = normalize_account_id(account_id)
        if not normalized or normalized == DEFAULT_ACCOUNT_ID:
            # 理论上 worker 已经拦下了，这里再兜底一次。
            self._render_no_account("探测结果无效，请点『刷新账号』重试")
            self.detectionStateChanged.emit(False)
            return

        # 首启 or 当前无账号 → 直接生效
        if purpose == "startup" or not self._current_account:
            self._apply_account(normalized)
            return

        # 刷新 + vid 相同 → 无变化，仅提示
        if normalized == self._current_account:
            QMessageBox.information(
                self,
                "账号无变化",
                f"当前账号仍为 {normalized}，无需切换。",
            )
            return

        # 刷新 + vid 不同 → 弹确认（防误操作 & 提醒风控代价）
        ans = QMessageBox.question(
            self,
            "确认切换账号",
            (
                f"检测到 MuMu 里企微当前登录账号为：\n\n    {normalized}\n\n"
                f"当前 GUI 账号为：{self._current_account}\n\n"
                "切换后 GUI 状态会重新加载（联系人、素材、计划都会切到新账号的目录）。\n"
                "请确认新账号已在 MuMu 里稳定登录（避免切换后又被踢回登录页）。\n\n"
                "确认切换到新账号吗？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # 默认 No，防手滑
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._apply_account(normalized)

    def _apply_account(self, account_id: str) -> None:
        """把探测到的 vid 落到 env / data root，通知主窗 rebind。"""
        switch_account(account_id, ensure_data_root=True)
        self._current_account = account_id
        self._render_ok(account_id)
        self.detectionStateChanged.emit(True)
        self.accountChanged.emit(account_id)


__all__ = ["AccountBar"]
