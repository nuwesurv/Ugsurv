# -*- coding: utf-8 -*-
"""
DynamicInputWidget — floating interactive input prompt near the cursor.

Modes (set by tool.inputModeChanged signal):
  "xy"    – two fields: X, Y  (absolute coordinates)
  "polar" – two fields: Dist, Angle°
  "value" – one field:  single numeric value
  ""      – hidden / no active tool

All key input is routed here from GlobalKeyFilter via handle_key().
The canvas never loses focus — the widget has NoFocus / WA_TransparentForMouseEvents.

Signals:
  coordinateEntered(x, y)  – emitted in "xy" mode with absolute X, Y
  polarEntered(dist, angle) – emitted in "polar" mode with dist and angle°
  valueEntered(v)           – emitted in "value" mode
"""

import re as _re

from qgis.PyQt.QtWidgets import QWidget, QHBoxLayout, QLabel, QLineEdit
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor, QFont


_OUTER = (
    "DynamicInputWidget{"
    "background:rgba(18,18,28,240);"
    "border:1px solid #3a3a5a;"
    "border-radius:4px;}"
)
_PROMPT = (
    "color:#8aabdd;background:transparent;"
    "padding:2px 8px 2px 6px;font-size:9pt;"
)
_FIELD_IDLE = (
    "QLineEdit{background:#1e2030;color:#9999bb;"
    "border:1px solid #444466;border-radius:2px;"
    "padding:1px 5px;font-family:Consolas;font-size:9pt;"
    "min-width:75px;max-width:120px;}"
)
_FIELD_ACTIVE = (
    "QLineEdit{background:#122040;color:#ffffff;"
    "border:1px solid #4488cc;border-radius:2px;"
    "padding:1px 5px;font-family:Consolas;font-size:9pt;"
    "min-width:75px;max-width:120px;}"
)
_LBL = (
    "color:#556688;background:transparent;"
    "padding:0 1px 0 3px;font-size:8pt;"
)
_CRS_LBL = (
    "color:#4a9a8a;background:rgba(20,50,45,180);"
    "border:1px solid #2a6a5a;border-radius:2px;"
    "padding:1px 6px;font-size:7pt;font-family:Consolas;"
)

# (field_label, placeholder) lists per mode
_CONFIGS = {
    "xy":    [("X",     "0.000"), ("Y",    "0.000")],
    "en":    [("E",     "0.000"), ("N",    "0.000")],   # georeferencing GCP input
    "polar": [("Dist",  "0.000"), ("Brg",  "0.0°" )],
    "value": [("Value", "0.000")],
}


class DynamicInputWidget(QWidget):
    coordinateEntered = pyqtSignal(float, float)   # absolute X, Y
    polarEntered      = pyqtSignal(float, float)   # dist, angle°
    valueEntered      = pyqtSignal(float)

    def __init__(self, canvas, input_translator=None, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._canvas        = canvas
        self._mode          = ""
        self._texts         = ["", ""]   # typed text per field
        self._active        = 0          # index of active (highlighted) field
        self._fields        = []         # QLineEdit list (rebuilt on mode change)
        self._field_widgets = []         # all added widgets (labels + fields)
        self._live          = [0.0, 0.0] # live cursor values (dist/angle or x/y)

        self.setStyleSheet(_OUTER)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._build_ui()
        self.hide()

    # ── construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(6, 4, 6, 4)
        self._layout.setSpacing(2)

        self._crs_badge = QLabel("WGS84 36N")
        self._crs_badge.setStyleSheet(_CRS_LBL)
        self._layout.addWidget(self._crs_badge)

        self._prompt_lbl = QLabel()
        self._prompt_lbl.setStyleSheet(_PROMPT)
        self._prompt_lbl.hide()
        self._layout.addWidget(self._prompt_lbl)

    def _rebuild_fields(self, config):
        for w in self._field_widgets:
            self._layout.removeWidget(w)
            w.deleteLater()
        self._field_widgets = []
        self._fields = []
        self._texts  = [""] * len(config)

        for i, (label_text, placeholder) in enumerate(config):
            if i > 0:
                sep = QLabel("|")
                sep.setStyleSheet("color:#333355;background:transparent;padding:0 3px;")
                self._layout.addWidget(sep)
                self._field_widgets.append(sep)

            lbl = QLabel(label_text)
            lbl.setStyleSheet(_LBL)
            self._layout.addWidget(lbl)
            self._field_widgets.append(lbl)

            field = QLineEdit()
            field.setPlaceholderText(placeholder)
            field.setReadOnly(True)
            field.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            field.setStyleSheet(_FIELD_IDLE)
            self._layout.addWidget(field)
            self._field_widgets.append(field)
            self._fields.append(field)

    # ── public API ────────────────────────────────────────────────────────
    def set_crs_label(self, text: str):
        """Update the CRS badge (e.g. 'WGS84 36N') shown in the widget."""
        self._crs_badge.setText(text)

    def set_mode(self, mode: str, prompt: str = ""):
        """Called by tool's inputModeChanged signal."""
        self._mode   = mode
        self._active = 0

        if not mode or mode not in _CONFIGS:
            self.hide()
            return

        # Strip key-hint blocks like [U=undo C=close] — those belong in the
        # command line only; the cursor-side prompt stays concise.
        display_prompt = _re.sub(r'\s*\[[^\]]+\]', '', prompt).strip()
        if display_prompt:
            self._prompt_lbl.setText(display_prompt)
            self._prompt_lbl.show()
        else:
            self._prompt_lbl.hide()

        self._rebuild_fields(_CONFIGS[mode])
        self._refresh_fields()
        self.adjustSize()
        self._reposition()
        self.show()

    def set_live_polar(self, dist: float, angle_deg: float):
        """Update live cursor values used as fallbacks when a polar field is empty."""
        self._live = [dist, angle_deg]

    def update_position(self, canvas_pt=None):
        """Called on every canvas xyCoordinates event — follow the cursor."""
        if self.isVisible():
            self._reposition()

    # ── InputBuffer compat slots (no-op: interactive widget owns its text) ─
    def on_buffer_text_changed(self, text: str):
        pass

    def on_buffer_cancelled(self):
        if self._mode and self._fields:
            self._texts  = [""] * len(self._fields)
            self._active = 0
            self._refresh_fields()

    # kept for wiring compatibility
    def show_for_tool(self, tool_is_active: bool):
        self._reposition()

    # ── Key routing (called by GlobalKeyFilter._drawing) ──────────────────
    def handle_key(self, event) -> bool:
        """
        Process one key event for the active field.
        Returns True if consumed, False to let the caller fall through.
        """
        if not self._mode or not self._fields:
            return False

        key = event.key()
        ch  = event.text()

        # Submit all fields
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return self._submit()

        # Cycle to next field — Tab or comma
        if key == Qt.Key.Key_Tab or ch == ',':
            self._cycle_field()
            return True

        # Backspace — remove last char from active field
        if key == Qt.Key.Key_Backspace:
            t = self._texts[self._active]
            if t:
                self._texts[self._active] = t[:-1]
                self._refresh_fields()
                return True
            return False   # empty — let Esc/tool handle it

        # Escape — pass through so BaseTool/_handle_esc fires
        if key == Qt.Key.Key_Escape:
            return False

        # Printable character (excluding comma which is handled above).
        # In numeric modes only digits, '.', and '-' are absorbed; letters
        # fall through so single-key commands (C=close, U=undo, A=arc…)
        # still reach the tool even while fields have content.
        if ch and ch.isprintable() and ch not in ('\t', ','):
            if self._mode in ('polar', 'xy', 'en', 'value') and not (ch.isdigit() or ch in '.-'):
                return False
            self._texts[self._active] += ch
            self._refresh_fields()
            return True

        return False

    # ── internal ──────────────────────────────────────────────────────────
    def _submit(self) -> bool:
        mode = self._mode

        if mode in ("xy", "en"):
            try:
                x = float(self._texts[0].strip())
                y = float(self._texts[1].strip())
            except (ValueError, IndexError):
                return False
            self.coordinateEntered.emit(x, y)
            self._clear()
            return True

        if mode == "polar":
            t0 = self._texts[0].strip()
            t1 = self._texts[1].strip()
            if not t0 and not t1:
                return False  # nothing typed — let Enter pass through to tool (confirm/end)
            try:
                dist  = float(t0) if t0 else self._live[0]
                angle = float(t1) if t1 else self._live[1]
            except (ValueError, IndexError):
                return False
            self.polarEntered.emit(dist, angle)
            self._clear()
            return True

        if mode == "value":
            try:
                v = float(self._texts[0].strip())
            except (ValueError, IndexError):
                return False
            self.valueEntered.emit(v)
            self._clear()
            return True

        return False

    def _cycle_field(self):
        n = len(self._fields)
        if n > 1:
            self._active = (self._active + 1) % n
            self._refresh_fields()

    def _clear(self):
        self._texts  = [""] * len(self._fields)
        self._active = 0
        self._refresh_fields()

    def _refresh_fields(self):
        for i, field in enumerate(self._fields):
            field.setText(self._texts[i] if i < len(self._texts) else "")
            field.setStyleSheet(_FIELD_ACTIVE if i == self._active else _FIELD_IDLE)
        self.adjustSize()

    def _reposition(self):
        pos = QCursor.pos()
        self.move(pos.x() + 18, pos.y() + 18)
