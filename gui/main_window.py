from __future__ import annotations

from pathlib import Path

from loguru import logger
from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import QMainWindow, QMenu, QMessageBox, QProgressDialog, QTabWidget, QToolBar

from app import __version__
from app.broadcast.context import AppContext
from app.broadcast.paths import DEFAULT_ACCOUNT_ID
from app.config import resolve_path
from app.updater.apply_update import (
    UpdateError,
    download_update,
    portable_install_root,
    spawn_update_helper,
    validate_update_archive,
)
from app.updater.version_check import (
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_RELEASES_PAGE,
    PROGRAM_UPDATE_ASSET,
    VersionCheckResult,
    check_latest_version,
    get_local_app_version,
)
from gui.i18n import APP_TITLE
from gui.tabs.library_tab import LibraryTab
from gui.tabs.notice_publisher import NoticePublisherPanel
from gui.tabs.plan_composer import PlanComposerPanel
from gui.tabs.run_monitor import RunMonitorPanel
from gui.tray_icon import AppTrayIcon
from gui.widgets.account_bar import AccountBar


class _ProgramUpdateThread(QThread):
    """后台下载 program-update.zip，避免卡住界面。"""

    progress = Signal(int, int)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, dest: str, expected_version: str, parent: QMainWindow | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._dest = dest
        self._expected_version = expected_version
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            def on_progress(done: int, total: int) -> None:
                if self._cancel:
                    raise UpdateError("已取消")
                self.progress.emit(done, total)

            download_update(self._url, Path(self._dest), on_progress=on_progress)
            if self._cancel:
                raise UpdateError("已取消")
            validate_update_archive(Path(self._dest), expected_version=self._expected_version)
            self.succeeded.emit(self._dest)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    """GUI 主窗口壳：账号栏 + 3 主 Tab。"""

    def __init__(self, *, voice_index_path: str | Path | None = None) -> None:
        super().__init__()
        self._voice_index_path = voice_index_path
        self._force_quit = False
        self._update_thread: _ProgramUpdateThread | None = None
        self._update_progress: QProgressDialog | None = None
        self._pending_update_version = ""
        # 一机一号：启动时不再从 env 读账号（env 里的可能是上次运行的残留 vid，
        # 与本次企微登录状态无关）。AppContext 先用 default 占位，等 AccountBar
        # 探测完成 → accountChanged → _on_account_changed → rebind 到真 vid。
        self._app_context = AppContext(DEFAULT_ACCOUNT_ID)
        self.setWindowTitle(f"{APP_TITLE} · v{__version__}")
        # 宽在 853 基础上再 ×4/3，高在 547 基础上再 ×5/4；最小尺寸明显小于屏幕，保证左右边缘可自由拖拽。
        self.setMinimumSize(720, 480)
        self.resize(1137, 684)
        self._build_ui()
        self._bind_events()
        self._setup_menu()
        self._setup_tray()
        # 探测结果出来前先禁用所有业务 tab；detectionStateChanged 会解锁。
        self._set_business_tabs_enabled(False)
        QTimer.singleShot(0, self._post_startup_tasks)

    def _build_ui(self) -> None:
        self._account_bar = AccountBar()
        self._toolbar = QToolBar("账号")
        self._toolbar.setMovable(False)
        self._toolbar.addWidget(self._account_bar)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)

        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        self._library_tab = LibraryTab(preview_service_provider=lambda: self._app_context.preview_service)
        self._composer_tab = PlanComposerPanel(
            scheduler_provider=lambda: self._app_context.scheduler,
            preview_service_provider=lambda: self._app_context.preview_service,
        )
        self._notice_tab = NoticePublisherPanel(submit_plan=self._composer_tab.submit_prepared_plan)
        self._monitor_tab = RunMonitorPanel(
            plan_provider=self._composer_tab.build_plan,
            preview_service_provider=lambda: self._app_context.preview_service,
        )

        self._tabs.addTab(self._library_tab, "素材库")
        self._tabs.addTab(self._composer_tab, "编辑和发送消息")
        self._tabs.addTab(self._notice_tab, "@和群公告发布")
        self._tabs.addTab(self._monitor_tab, "执行结果")

    def _setup_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = QMenu("文件", self)
        self._act_quit = QAction("完全退出", self)
        self._act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self._act_quit.triggered.connect(self._quit_app)
        file_menu.addAction(self._act_quit)
        menu_bar.addMenu(file_menu)

        help_menu = QMenu("帮助", self)
        self._act_user_manual = QAction("使用手册", self)
        self._act_check_update = QAction("检查更新", self)
        self._act_about = QAction("关于", self)
        self._act_user_manual.triggered.connect(self._open_user_manual)
        self._act_check_update.triggered.connect(lambda: self._check_update_prompt(silent=False))
        self._act_about.triggered.connect(self._show_about_dialog)
        help_menu.addAction(self._act_user_manual)
        help_menu.addAction(self._act_check_update)
        help_menu.addSeparator()
        help_menu.addAction(self._act_about)
        menu_bar.addMenu(help_menu)

    def _setup_tray(self) -> None:
        self._tray: AppTrayIcon | None = None
        if not AppTrayIcon.is_available():
            return
        self._tray = AppTrayIcon(self, self._quit_app)
        self._tray.show()

    def _bind_events(self) -> None:
        self._account_bar.accountChanged.connect(self._on_account_changed)
        self._account_bar.detectionStateChanged.connect(self._set_business_tabs_enabled)
        self._library_tab.libraryChanged.connect(self._composer_tab.set_voice_entries)
        self._library_tab.libraryAssetsChanged.connect(self._composer_tab.set_library_assets)
        self._library_tab.assetsPurged.connect(self._composer_tab.drop_removed_assets)
        self._composer_tab.contactsSnapshotChanged.connect(self._notice_tab.set_contact_rows)
        self._composer_tab.contactsRefreshStateChanged.connect(self._notice_tab.set_refreshing)
        self._notice_tab.refreshRequested.connect(self._composer_tab.refresh_contacts)
        self._composer_tab.runPlanRequested.connect(self._monitor_tab.start_run_with_plan)
        self._composer_tab.scheduleDialogAboutToShow.connect(self._raise_main_window)
        self._composer_tab.pendingPlansChanged.connect(self._monitor_tab.refresh_pending_plans)
        self._monitor_tab.pendingTimerReschedule.connect(self._composer_tab.reschedule_pending)
        self._monitor_tab.pendingTimerCancel.connect(self._composer_tab.cancel_pending)
        self._monitor_tab.pendingCronCancel.connect(self._composer_tab.cancel_cron)
        self._app_context.add_schedule_listener(self._composer_tab.enqueue_schedule_entry)

    def _set_business_tabs_enabled(self, enabled: bool) -> None:
        """启动/切账号成功前禁用业务 tab，防止误操作。

        参数 True = 探到有效企微 vid；False = 未检测 / 检测失败。
        """
        for idx in range(self._tabs.count()):
            self._tabs.setTabEnabled(idx, enabled)

    def _on_account_changed(self, account_id: str) -> None:
        # 探测失败会 emit 空串；此时不 rebind、不同步 tab（tab 已被 disable）。
        if not account_id or account_id == DEFAULT_ACCOUNT_ID:
            return
        # 切账号时先停掉旧账号下的素材监听，防止 pending 目录污染。
        self._library_tab.stop_listeners()
        self._app_context.rebind(account_id)
        self._library_tab.set_account(account_id)
        self._composer_tab.set_account(account_id)
        self._notice_tab.set_account(account_id)
        self._monitor_tab.set_account(account_id)
        self._composer_tab.set_voice_entries(self._library_tab.active_voice_entries())
        self._composer_tab.set_library_assets(self._library_tab.active_assets())

    def _raise_main_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        self._force_quit = True
        self.close()
        # 显式关掉事件循环：closeEvent 里已 shutdown 后端，但如果还有非 daemon
        # QThread 阻塞（例如联系人刷新途中），exec() 不会自动返回。强制 quit
        # 让主进程立刻结束；子进程由 Job Object 兜底回收（win_job.py）。
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _post_startup_tasks(self) -> None:
        # 一机一号：启动就无条件跑一次账号探测。
        # 探测结果通过 detectionStateChanged 决定 tab 是否启用；
        # accountChanged 会触发 _on_account_changed 做 rebind 与 tab 数据装载。
        self._account_bar.try_auto_detect_on_startup()
        self._check_update_prompt(silent=True)

    def _check_update_prompt(self, *, silent: bool) -> None:
        result = check_latest_version()
        if result.error:
            logger.warning("检查更新失败: {}", result.error)
            if not silent:
                QMessageBox.warning(self, "检查更新失败", "当前无法获取版本信息，请稍后重试。")
            return
        if not result.has_update:
            if not silent:
                QMessageBox.information(self, "检查更新", f"当前已是最新版本：{result.current_version}")
            return
        self._prompt_install_update(result)

    def _prompt_install_update(self, result: VersionCheckResult) -> None:
        root = portable_install_root()
        can_apply = root is not None and bool(result.asset_url)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("发现新版本")
        if can_apply:
            box.setText(
                f"当前版本：{result.current_version}\n"
                f"最新版本：{result.latest_version}\n\n"
                "将下载更新并覆盖「程序」，然后重新打开。\n"
                "联系人、素材、计划和 vendor 不会被替换。"
            )
            update_btn = box.addButton("更新并重启", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("稍后再说", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is update_btn and root is not None:
                self._begin_program_update(result, root)
            return

        if root is None:
            detail = "当前不是通过「启动.exe」打开的目录分发包，不能自动覆盖「程序」。"
        else:
            detail = f"发布里没有 {PROGRAM_UPDATE_ASSET}，不能自动更新。"
        box.setText(
            f"当前版本：{result.current_version}\n最新版本：{result.latest_version}\n\n{detail}\n是否打开发布页面？"
        )
        open_btn = box.addButton("打开发布页", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("稍后再说", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(result.releases_url))

    def _begin_program_update(self, result: VersionCheckResult, root: Path) -> None:
        if self._update_thread is not None:
            QMessageBox.information(self, "请稍候", "正在下载更新。")
            return
        dest = root / ".update-staging" / PROGRAM_UPDATE_ASSET
        self._pending_update_version = result.latest_version
        progress = QProgressDialog("正在下载更新…", "取消", 0, 0, self)
        progress.setWindowTitle("更新")
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.canceled.connect(self._on_update_canceled)
        progress.show()
        self._update_progress = progress

        thread = _ProgramUpdateThread(result.asset_url, str(dest), result.latest_version, self)
        thread.progress.connect(self._on_update_progress)
        thread.succeeded.connect(self._on_update_downloaded)
        thread.failed.connect(self._on_update_failed)
        thread.finished.connect(self._cleanup_update_thread)
        self._update_thread = thread
        thread.start()

    def _on_update_progress(self, done: int, total: int) -> None:
        progress = self._update_progress
        if progress is None:
            return
        if total > 0:
            progress.setRange(0, total)
            progress.setValue(min(done, total))
            progress.setLabelText(f"正在下载更新… {done // 1024} KB / {total // 1024} KB")
        else:
            progress.setLabelText(f"正在下载更新… {done // 1024} KB")

    def _on_update_canceled(self) -> None:
        if self._update_thread is not None:
            self._update_thread.cancel()

    def _on_update_downloaded(self, zip_path: str) -> None:
        self._close_update_progress()
        root = portable_install_root()
        if root is None:
            QMessageBox.warning(self, "无法更新", "没有找到目录分发安装位置。")
            return
        try:
            spawn_update_helper(root, Path(zip_path), expected_version=self._pending_update_version)
        except Exception as exc:
            logger.exception("启动更新失败")
            QMessageBox.warning(self, "无法更新", f"没能启动更新程序：\n{exc}")
            return
        self._quit_app()

    def _on_update_failed(self, message: str) -> None:
        self._close_update_progress()
        if message == "已取消":
            return
        QMessageBox.warning(self, "更新失败", message)

    def _close_update_progress(self) -> None:
        progress = self._update_progress
        self._update_progress = None
        if progress is not None:
            progress.close()

    def _cleanup_update_thread(self) -> None:
        thread = self._update_thread
        self._update_thread = None
        if thread is not None:
            thread.deleteLater()

    def _open_user_manual(self) -> None:
        manual = resolve_path(Path("docs") / "USER_MANUAL.md")
        if not manual.exists():
            logger.warning("使用手册不存在: {}", manual)
            QMessageBox.warning(self, "使用手册不存在", "未找到使用手册，请联系同事获取最新文档。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(manual)))

    def _show_about_dialog(self) -> None:
        app_ver = get_local_app_version()
        repo_home = DEFAULT_RELEASES_PAGE.removesuffix("/releases")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("关于")
        box.setText(
            "\n".join(
                [
                    APP_TITLE,
                    f"版本号：{app_ver}",
                    f"企微适配版本：{DEFAULT_ADAPTER_VERSION}",
                    f"项目地址：{repo_home}",
                ]
            )
        )
        open_btn = box.addButton("打开项目主页", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(repo_home))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_quit or self._tray is None:
            if self._tray is not None:
                self._tray.hide()
            self._library_tab.stop_listeners()
            self._app_context.shutdown()
            event.accept()
            return
        event.ignore()
        self.hide()
        self._tray.notify_minimized()
