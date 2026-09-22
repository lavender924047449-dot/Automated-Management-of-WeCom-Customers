from __future__ import annotations



from PySide6.QtCore import Qt, Signal

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QScrollArea, QWidget



from gui.i18n import CHOICE_BUTTON_STYLE





class LabelMultiSelect(QWidget):

    """联系人标签选择器（可复选；样式为边框高亮）。"""



    labelToggled = Signal(str, bool)



    def __init__(self) -> None:

        super().__init__()

        self._buttons: dict[str, QPushButton] = {}

        self._build_ui()



    def _build_ui(self) -> None:

        root = QHBoxLayout(self)

        root.setContentsMargins(0, 0, 0, 0)

        self._content = QWidget()

        self._row = QHBoxLayout(self._content)

        self._row.setContentsMargins(4, 2, 4, 2)

        self._row.setSpacing(6)

        self._row.addStretch(1)



        self._scroll = QScrollArea()

        self._scroll.setWidgetResizable(True)

        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._scroll.setWidget(self._content)

        self._scroll.setMaximumHeight(58)

        root.addWidget(self._scroll)



    def set_labels(self, labels: list[str], *, checked: set[str] | None = None) -> None:

        preserve = checked if checked is not None else {k for k, btn in self._buttons.items() if btn.isChecked()}

        for btn in self._buttons.values():

            btn.deleteLater()

        self._buttons.clear()

        while self._row.count() > 1:

            item = self._row.takeAt(0)

            if item and item.widget():

                item.widget().deleteLater()



        for label in labels:

            text = str(label).strip()

            if not text:

                continue

            btn = QPushButton(text)

            btn.setCheckable(True)

            btn.setStyleSheet(CHOICE_BUTTON_STYLE)

            btn.setChecked(text in preserve)

            btn.toggled.connect(lambda checked_state, x=text: self.labelToggled.emit(x, checked_state))

            self._row.insertWidget(self._row.count() - 1, btn)

            self._buttons[text] = btn



    def reset_checks(self) -> None:

        for button in self._buttons.values():

            button.setChecked(False)



    def checked_labels(self) -> set[str]:

        return {text for text, btn in self._buttons.items() if btn.isChecked()}


