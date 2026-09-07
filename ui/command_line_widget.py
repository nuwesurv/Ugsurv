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

import re as _re
import ast as _ast
import operator as _oper

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTextEdit, QLabel, QListWidget, QListWidgetItem,
    QApplication,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QEvent
from qgis.PyQt.QtGui import QFont, QColor, QPalette, QCursor


def _format_hint_html(text: str) -> str:
    """Convert [K=desc ...] bracket notation to HTML with bold+underlined key chars."""
    parts = []
    last  = 0
    for m in _re.finditer(r'\[([^\]]+)\]', text):
        chunk = text[last:m.start()]
        parts.append(chunk.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
        tokens = []
        for tok in m.group(1).split():
            if '=' in tok:
                k, d = tok.split('=', 1)
                tokens.append(f'<b><u>{k}</u></b>={d}')
            else:
                tokens.append(tok)
        parts.append('[' + ' '.join(tokens) + ']')
        last = m.end()
    parts.append(text[last:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    return ''.join(parts)


# ── Safe arithmetic evaluator ─────────────────────────────────────────────────
_MATH_CHARS = _re.compile(r'^[\d\s\.\+\-\*\/\%\^\(\)]+$')

_MATH_OPS = {
    _ast.Add: _oper.add,
    _ast.Sub: _oper.sub,
    _ast.Mult: _oper.mul,
    _ast.Div: _oper.truediv,
    _ast.Mod: _oper.mod,
    _ast.Pow: _oper.pow,
    _ast.FloorDiv: _oper.floordiv,
    _ast.USub: _oper.neg,
    _ast.UAdd: _oper.pos,
}


def _eval_node(node):
    if isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, _ast.BinOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, _ast.UnaryOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("unsupported node")


def _try_eval_math(text: str) -> str | None:
    """Return formatted result if text is a safe arithmetic expression, else None.

    Requires at least one binary operator so bare numbers don't trigger evaluation.
    Division by zero and other math errors return None silently.
    """
    s = text.strip()
    if not s or not _MATH_CHARS.match(s):
        return None
    # Require a binary operator: +, *, /, %, ^ or a - that follows a digit
    if not _re.search(r'[\+\*\/\%\^]|(?<=\d)-', s):
        return None
    try:
        result = _eval_node(_ast.parse(s.replace('^', '**'), mode='eval').body)
        if isinstance(result, float):
            if result == int(result):
                return str(int(result))
            return f"{result:.10g}"
        return str(result)
    except Exception:
        return None


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
        self._dispatcher    = dispatcher
        self._translator    = input_translator
        self._history: list = []
        self._hist_idx: int = -1
        self._mid_command   = False
        self._ui_commands: dict[str, object] = {}  # alias.upper() → callable

        # log state: permanent entries + one volatile hint
        self._log_entries: list[tuple[str, str]] = []  # (text, color)
        self._current_hint: str | None = None           # formatted HTML hint

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

    def unregister_ui_command(self, *aliases: str):
        """Remove aliases from the command dict so they vanish from suggestions."""
        for alias in aliases:
            self._ui_commands.pop(alias.upper(), None)

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
            if not active:
                self.clear_hint()
        except RuntimeError:  # nosec B110
            pass

    def log_hint(self, text: str):
        """Show a formatted tool hint as the last line of the log (temporary — not saved)."""
        self._current_hint = _format_hint_html(text) if text else None
        self._rebuild_log()

    def clear_hint(self):
        if self._current_hint is not None:
            self._current_hint = None
            self._rebuild_log()

    def log(self, text: str, color: str = "#cccccc"):
        self._log_entries.append((text, color))
        self._rebuild_log()

    def clear_log(self):
        self._log_entries.clear()
        self._current_hint = None
        self._rebuild_log()

    # ── internal log rendering ────────────────────────────────────────────
    def _rebuild_log(self):
        try:
            sb = self._log.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 4

            self._log.blockSignals(True)
            self._log.clear()
            for text, color in self._log_entries:
                self._log.append(f'<span style="color:{color}">{text}</span>')
            if self._current_hint:
                self._log.append(
                    f'<span style="color:#7a9ec0;font-style:italic">'
                    f'{self._current_hint}</span>'
                )
            self._log.blockSignals(False)

            if at_bottom:
                sb.setValue(sb.maximum())
        except RuntimeError:  # nosec B110
            pass

    # ── suggestion popup ──────────────────────────────────────────────────
    def _all_aliases(self) -> list[str]:
        """Sorted union of tool-registry aliases and UI-command aliases."""
        aliases: set[str] = set(self._ui_commands.keys())
        try:
            aliases.update(self._dispatcher._registry.all_aliases())
        except Exception:  # nosec B110
            pass
        return sorted(aliases)

    # ── InputBuffer handlers ──────────────────────────────────────────────
    def _on_buffer_text_changed(self, text: str):
        """Mirror buffer content in the input field without triggering autocomplete."""
        try:
            self._input.blockSignals(True)
            self._input.setText(text)
            self._input.blockSignals(False)
        except RuntimeError:  # nosec B110
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
                math_result = _try_eval_math(text)
                if math_result is not None:
                    self.log(f"= {math_result}  (copied)", "#88ddaa")
                    QApplication.clipboard().setText(math_result)
                else:
                    self.commandEntered.emit(text)
                    self._dispatcher.dispatch(text)

    def _on_buffer_cancelled(self):
        """Called when Esc clears the buffer."""
        try:
            self._input.blockSignals(True)
            self._input.clear()
            self._input.blockSignals(False)
        except RuntimeError:  # nosec B110
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
                math_result = _try_eval_math(text)
                if math_result is not None:
                    self.log(f"= {math_result}  (copied)", "#88ddaa")
                    QApplication.clipboard().setText(math_result)
                else:
                    self.commandEntered.emit(text)
                    self._dispatcher.dispatch(text)

    def eventFilter(self, obj, event):  # noqa: C901
        # When the command-line input gains focus, snap the popup above it
        # (it may have been positioned near the cursor from type-anywhere mode).
        if event.type() == QEvent.Type.FocusIn and obj is self._input:
            if self._popup.isVisible():
                height = self._popup.height()
                gpos = self._input.mapToGlobal(self._input.rect().topLeft())
                self._popup.move(gpos.x(), gpos.y() - height)
            return False

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
