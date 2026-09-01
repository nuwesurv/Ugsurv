# -*- coding: utf-8 -*-
"""
CommandLineWidget — floating command-line dock that mirrors AutoCAD's
command window.

- Text input: returnPressed → CommandDispatcher.dispatch()
- Up/down arrow: navigate suggestions popup when open, else history
- Tab: complete to first / currently highlighted suggestion
- Mid-command typed input is routed as semantic events via InputTranslator,
  not treated as a new command
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTextEdit, QLabel, QListWidget, QListWidgetItem,
    QApplication,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QEvent
from qgis.PyQt.QtGui import QFont, QColor, QPalette, QCursor


_POPUP_STYLE = """
QListWidget {
    background: #1a1a2e;
    color: #cccccc;
    border: 1px solid #4488cc;
    outline: none;
}
QListWidget::item {
    padding: 2px 6px;
}
QListWidget::item:selected, QListWidget::item:hover {
    background: #1e3a6e;
    color: #ffffff;
}
"""


class CommandLineWidget(QDockWidget):
    commandEntered   = pyqtSignal(str)   # raw token or typed text
    textValueEntered = pyqtSignal(str)   # used for mid-command text

    def __init__(self, dispatcher, input_translator, parent=None):
        super().__init__("Command", parent)
        self.setObjectName("UgsurvCommandLine")
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea |
                             Qt.DockWidgetArea.TopDockWidgetArea)
        self._dispatcher  = dispatcher
        self._translator  = input_translator
        self._history: list = []
        self._hist_idx: int = -1
        self._mid_command = False
        self._ui_commands: dict[str, object] = {}  # alias.upper() → callable

        self._build_ui()
        self._build_popup()

    # ── UI construction ───────────────────────────────────────────────────
    def _build_ui(self):
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 4, 4, 4)
        vlay.setSpacing(2)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(30)
        self._log.setFont(QFont("Consolas", 9))
        p = self._log.palette()
        p.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
        p.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
        self._log.setPalette(p)
        vlay.addWidget(self._log, 1)

        row = QHBoxLayout()
        self._prompt = QLabel("Command: ")
        self._prompt.setFont(QFont("Consolas", 9))
        row.addWidget(self._prompt)

        self._input = QLineEdit()
        self._input.setFont(QFont("Consolas", 9))
        self._input.setPlaceholderText("type command or value…")
        self._input.returnPressed.connect(self._on_enter)
        self._input.textChanged.connect(self._on_text_changed)
        self._input.installEventFilter(self)
        row.addWidget(self._input)
        vlay.addLayout(row, 0)

        self.setWidget(w)

    def _build_popup(self):
        # ToolTip window type: frameless, stays on top, never steals focus
        self._popup = QListWidget()
        self._popup.setWindowFlags(
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
        )
        self._popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._popup.setFont(QFont("Consolas", 9))
        self._popup.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._popup.setStyleSheet(_POPUP_STYLE)
        self._popup.itemClicked.connect(self._on_suggestion_clicked)
        self._popup.hide()

        # Global key interceptor: capture Up/Down/Enter/Esc while popup is open,
        # regardless of which widget currently has keyboard focus.
        QApplication.instance().installEventFilter(self)

    def closeEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(event)

    # ── public API ────────────────────────────────────────────────────────
    def register_ui_command(self, *aliases: str, callback):
        """Register aliases (e.g. 'SNAP', 'OS') that open a UI dialog."""
        for alias in aliases:
            self._ui_commands[alias.upper()] = callback

    def connect_buffer(self, buf):
        """Wire an InputBuffer so the command line mirrors and responds to it."""
        buf.textChanged.connect(self._on_buffer_text_changed)
        buf.submitted.connect(self._on_buffer_submitted)
        buf.cancelled.connect(self._on_buffer_cancelled)

    def set_mid_command(self, active: bool, prompt: str = ""):
        self._mid_command = active
        self._popup_hide()
        try:
            self._prompt.setText(prompt + ": " if prompt else "Command: ")
        except RuntimeError:
            pass

    def log(self, text: str, color: str = "#cccccc"):
        try:
            self._log.append(f'<span style="color:{color}">{text}</span>')
        except RuntimeError:
            pass

    def clear_log(self):
        try:
            self._log.clear()
        except RuntimeError:
            pass

    # ── suggestion popup ──────────────────────────────────────────────────
    def _all_aliases(self) -> list[str]:
        """Sorted union of tool-registry aliases and UI-command aliases."""
        aliases: set[str] = set(self._ui_commands.keys())
        try:
            aliases.update(self._dispatcher._registry.all_aliases())
        except Exception:
            pass
        return sorted(aliases)

    # ── InputBuffer handlers ──────────────────────────────────────────────
    def _on_buffer_text_changed(self, text: str):
        """Mirror buffer content in the input field without triggering autocomplete."""
        try:
            self._input.blockSignals(True)
            self._input.setText(text)
            self._input.blockSignals(False)
        except RuntimeError:
            pass

    def _on_buffer_submitted(self, text: str):
        """Called when the user commits typed input via Enter/Space."""
        text = text.strip()
        if not text:
            return
        self._history.append(text)
        self._hist_idx = -1
        self.log(f"&gt; {text}", "#aaaaaa")
        if self._mid_command:
            self.textValueEntered.emit(text)
        else:
            ui_cb = self._ui_commands.get(text.upper())
            if ui_cb is not None:
                ui_cb()
            else:
                self.commandEntered.emit(text)
                self._dispatcher.dispatch(text)

    def _on_buffer_cancelled(self):
        """Called when Esc clears the buffer."""
        try:
            self._input.blockSignals(True)
            self._input.clear()
            self._input.blockSignals(False)
        except RuntimeError:
            pass

    def _on_text_changed(self, text: str):
        if self._mid_command or not text.strip():
            self._popup_hide()
            return
        prefix = text.strip().upper()
        matches = [a for a in self._all_aliases() if a.startswith(prefix)]
        if not matches:
            self._popup_hide()
            return
        self._popup_show(matches)

    def _popup_show(self, matches: list[str]):
        self._popup.blockSignals(True)
        self._popup.clear()
        for m in matches:
            self._popup.addItem(m)
        self._popup.blockSignals(False)

        item_h = self._popup.sizeHintForRow(0) + 2
        height = min(len(matches) * item_h + 6, 200)
        fm     = self._popup.fontMetrics()
        width  = max(fm.horizontalAdvance(m) for m in matches) + 28
        self._popup.setFixedSize(width, height)

        if self._input.hasFocus():
            # command line focused: show above the input field
            gpos = self._input.mapToGlobal(self._input.rect().topLeft())
            self._popup.move(gpos.x(), gpos.y() - height)
        else:
            # type-anywhere from canvas: show below-right of the mouse cursor
            cursor_pos = QCursor.pos()
            self._popup.move(cursor_pos.x() + 16, cursor_pos.y() + 16)

        self._popup.show()
        self._popup.setCurrentRow(0)

    def _popup_hide(self):
        self._popup.hide()

    def _popup_complete(self):
        """Fill input with the currently highlighted suggestion and execute."""
        item = self._popup.currentItem()
        if item is None and self._popup.count():
            item = self._popup.item(0)
        if item:
            self._input.setText(item.text())
        self._popup_hide()
        self._on_enter()

    def _on_suggestion_clicked(self, item: QListWidgetItem):
        self._input.setText(item.text())
        self._popup_hide()
        self._on_enter()

    # ── input handling ────────────────────────────────────────────────────
    def _on_enter(self):
        # If popup is open: complete & execute the highlighted item
        if self._popup.isVisible():
            self._popup_complete()
            return

        text = self._input.text().strip()
        self._input.clear()
        self._popup_hide()
        if not text:
            # repeat last command when idle (AutoCAD-style Enter/Space behaviour)
            if not self._mid_command and self._history:
                text = self._history[-1]
                self.log(f"&gt; {text}  (repeat)", "#888888")
                ui_cb = self._ui_commands.get(text.upper())
                if ui_cb is not None:
                    ui_cb()
                else:
                    self.commandEntered.emit(text)
                    self._dispatcher.dispatch(text)
            return
        self._history.append(text)
        self._hist_idx = -1
        self.log(f"&gt; {text}", "#aaaaaa")
        if self._mid_command:
            self.textValueEntered.emit(text)
        else:
            ui_cb = self._ui_commands.get(text.upper())
            if ui_cb is not None:
                ui_cb()
            else:
                self.commandEntered.emit(text)
                self._dispatcher.dispatch(text)

    def eventFilter(self, obj, event):
        # Track cursor movement so the popup follows the mouse
        if (event.type() == QEvent.Type.MouseMove
                and self._popup.isVisible()
                and not self._input.hasFocus()
                and obj is not self._popup):
            cursor_pos = QCursor.pos()
            self._popup.move(cursor_pos.x() + 16, cursor_pos.y() + 16)
            return False

        if event.type() == QEvent.Type.KeyPress:
            key = event.key()

            # ── Global popup navigation (any focused widget) ──────────────
            # Only intercept when the popup is visible AND focus is NOT on
            # _input (that case is handled by the _input branch below).
            if self._popup.isVisible() and obj is not self._input:
                if key == Qt.Key.Key_Up:
                    row = self._popup.currentRow()
                    self._popup.setCurrentRow(max(0, row - 1))
                    return True
                if key == Qt.Key.Key_Down:
                    row = self._popup.currentRow()
                    self._popup.setCurrentRow(
                        min(self._popup.count() - 1, row + 1)
                    )
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                           Qt.Key.Key_Space):
                    self._popup_complete()
                    return True
                if key == Qt.Key.Key_Escape:
                    self._popup_hide()
                    return True

            # ── Input-field-specific handling ─────────────────────────────
            if obj is self._input:
                # Tab → complete from popup (or do nothing)
                if key == Qt.Key.Key_Tab:
                    if self._popup.isVisible():
                        self._popup_complete()
                    return True

                # Escape → hide popup and clear any typed text.
                # If input is already empty, pass through so QGIS / SelectTool
                # can handle it (e.g. deselect features).
                if key == Qt.Key.Key_Escape:
                    if self._popup.isVisible():
                        self._popup_hide()
                    if self._input.text():
                        self._input.clear()
                        return True

                # Up → popup navigation if open, else history
                if key == Qt.Key.Key_Up:
                    if self._popup.isVisible():
                        row = self._popup.currentRow()
                        self._popup.setCurrentRow(max(0, row - 1))
                        return True
                    self._navigate_history(-1)
                    return True

                # Down → popup navigation if open, else history
                if key == Qt.Key.Key_Down:
                    if self._popup.isVisible():
                        row = self._popup.currentRow()
                        self._popup.setCurrentRow(
                            min(self._popup.count() - 1, row + 1)
                        )
                        return True
                    self._navigate_history(1)
                    return True

                # Space → execute (same as Enter)
                if key == Qt.Key.Key_Space:
                    self._on_enter()
                    return True

        return super().eventFilter(obj, event)

    def _navigate_history(self, direction: int):
        if not self._history:
            return
        self._hist_idx = max(-1, min(len(self._history) - 1,
                                     self._hist_idx + direction))
        if self._hist_idx >= 0:
            self._input.setText(self._history[-(self._hist_idx + 1)])
        else:
            self._input.clear()
