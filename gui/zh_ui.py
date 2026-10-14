"""界面字体用简体中文，并把 Qt 自带的英文按钮改成中文。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QAbstractButton, QApplication, QFileDialog, QWidget

_FONT_CANDIDATES = ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei", "SimSun", "宋体")

_BUTTON_ZH = {
    "ok": "确定",
    "yes": "是",
    "no": "否",
    "cancel": "取消",
    "save": "保存",
    "close": "关闭",
    "open": "打开",
    "discard": "放弃",
    "apply": "应用",
    "reset": "重置",
    "retry": "重试",
    "ignore": "忽略",
    "abort": "中止",
    "yes to all": "全部是",
    "no to all": "全部否",
    "save all": "全部保存",
    "don't save": "不保存",
    "restore defaults": "恢复默认",
    "help": "帮助",
    "select": "选择",
}


def _plain(text: str) -> str:
    return text.replace("&", "").strip().lower()


class _ZhUiFilter(QObject):
    """弹框显示时，把 Qt 标准按钮和文件对话框标签改成简体中文。"""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget):
            self._localize(watched)
        return False

    def _localize(self, widget: QWidget) -> None:
        if isinstance(widget, QFileDialog):
            self._localize_file_dialog(widget)
        for button in widget.findChildren(QAbstractButton):
            zh = _BUTTON_ZH.get(_plain(button.text()))
            if zh and button.text() != zh:
                button.setText(zh)

    def _localize_file_dialog(self, dialog: QFileDialog) -> None:
        labels = {
            QFileDialog.DialogLabel.LookIn: "查找范围：",
            QFileDialog.DialogLabel.FileName: "文件名：",
            QFileDialog.DialogLabel.FileType: "文件类型：",
            QFileDialog.DialogLabel.Accept: "打开",
            QFileDialog.DialogLabel.Reject: "取消",
        }
        if dialog.acceptMode() == QFileDialog.AcceptMode.AcceptSave:
            labels[QFileDialog.DialogLabel.Accept] = "保存"
        for label, text in labels.items():
            dialog.setLabelText(label, text)


def install_zh_ui(app: QApplication) -> None:
    """设置简体中文字体，并挂上弹框中文化过滤器。"""
    families = set(QFontDatabase.families())
    for name in _FONT_CANDIDATES:
        if name not in families:
            continue
        font = QFont(name)
        font.setStyleHint(QFont.StyleHint.SansSerif)
        point = app.font().pointSize()
        if point > 0:
            font.setPointSize(point)
        app.setFont(font)
        break
    filt = _ZhUiFilter(app)
    app.installEventFilter(filt)
    # 过滤器挂在 QApplication 上，这里再留一个引用，避免被回收。
    app.setProperty("_zh_ui_filter", filt)
