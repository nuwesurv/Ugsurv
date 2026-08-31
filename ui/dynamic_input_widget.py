# -*- coding: utf-8 -*-
"""
DynamicInputWidget — frameless floating prompt near the cursor.

Purely a display widget driven by InputBuffer.  No text-input fields.

Shows:
  - A prompt label set by the active tool (_request_input → inputModeChanged)
  - The current buffer text with a blinking underscore cursor

Hidden by default.  Appears as soon as InputBuffer has content.
Disappears when the buffer is cleared (submit or cancel).
Follows the OS cursor on every canvas mouse-move.
"""

from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFont, QCursor


_OUTER_CSS = (
    "DynamicInputWidget { "
    "background: rgba(20, 20, 28, 220); "
    "border: 1px solid #3a3a5a; "
    "border-radius: 4px; "
    "}"
)
_PROMPT_CSS = "color: #7aaadd; background: transparent; padding: 0 2px;"
_TEXT_CSS   = "color: #e8e8e8; background: transparent; padding: 0 2px;"
_CURSOR_CH  = "_"


class DynamicInputWidget(QWidget):
    """Floating input-echo widget — display only, no user interaction."""

    def __init__(self, canvas, input_translator=None, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._canvas    = canvas
        self._buf_text  = ""
        self._cursor_on = True

        self._build_ui()
        self._blink = QTimer(self)
        self._blink.setInterval(530)
        self._blink.timeout.connect(self._toggle_cursor)

        self.setStyleSheet(_OUTER_CSS)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    # ── construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(1)

        font_sm = QFont("Consolas", 9)
        font_lg = QFont("Consolas", 10)

        self._prompt_lbl = QLabel()
        self._prompt_lbl.setFont(font_sm)
        self._prompt_lbl.setStyleSheet(_PROMPT_CSS)
        self._prompt_lbl.hide()
        lay.addWidget(self._prompt_lbl)

        self._text_lbl = QLabel()
        self._text_lbl.setFont(font_lg)
        self._text_lbl.setStyleSheet(_TEXT_CSS)
        lay.addWidget(self._text_lbl)

    # ── InputBuffer slots (connected in Ugsurv.py) ────────────────────────
    def on_buffer_text_changed(self, text: str):
        self._buf_text = text
        if text:
            self._refresh_text()
            self._reposition()
            if not self.isVisible():
                self.show()
                self._blink.start()
        else:
            self.hide()
            self._blink.stop()
            self._cursor_on = True

    def on_buffer_cancelled(self):
        self._buf_text = ""
        self.hide()
        self._blink.stop()
        self._cursor_on = True

    # ── prompt ─────────────────────────────────────────────────────────────
    def set_prompt(self, prompt: str):
        if prompt:
            self._prompt_lbl.setText(prompt)
            self._prompt_lbl.show()
        else:
            self._prompt_lbl.hide()
        self.adjustSize()

    def set_mode(self, mode: str, prompt: str = ""):
        """Compat shim — called via inputModeChanged signal from tools."""
        self.set_prompt(prompt)

    # ── position ──────────────────────────────────────────────────────────
    def update_position(self, canvas_pt=None):
        """Called on every canvas xyCoordinates event to follow the cursor."""
        self._reposition()

    def show_for_tool(self, tool_is_active: bool):
        """Legacy compat — visibility is buffer-driven; just update position."""
        self._reposition()

    # ── internal ─────────────────────────────────────────────────────────
    def _reposition(self):
        pos = QCursor.pos()
        self.move(pos.x() + 18, pos.y() + 18)

    def _toggle_cursor(self):
        self._cursor_on = not self._cursor_on
        self._refresh_text()

    def _refresh_text(self):
        suffix = _CURSOR_CH if self._cursor_on else " "
        self._text_lbl.setText(self._buf_text + suffix)
        self.adjustSize()
