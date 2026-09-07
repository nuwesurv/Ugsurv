# -*- coding: utf-8 -*-
"""
DynamicInputWidget — floating interactive input prompt near the cursor.

Modes (set by tool.inputModeChanged signal):
  "xy"       – two fields: X, Y  (absolute coordinates)
  "polar"    – two fields: Dist, Angle°
  "value"    – one field:  single numeric value
  "no_value" – display-only prompt label, no input fields
  ""         – hidden / no active tool

All key input is routed here from GlobalKeyFilter via handle_key().
The canvas never loses focus — the widget has NoFocus / WA_TransparentForMouseEvents.

Signals:
  coordinateEntered(x, y)  – emitted in "xy" mode with absolute X, Y
  polarEntered(dist, angle) – emitted in "polar" mode with dist and angle°
  valueEntered(v)           – emitted in "value" mode
  textEntered(s)            – emitted in "text" mode with the raw string
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
    "xy":        [("X",       "0.000"), ("Y",       "0.000")],
    "en":        [("E",       "0.000"), ("N",       "0.000")],   # georeferencing GCP input
    "polar":     [("Dist",    "0.000"), ("Brg",     "0.0°" )],
    "value":     [("Value",   "0.000")],
    "text":      [("",        "pt")],                            # free-text single field
    "unit":      [("Unit",    "ha")],                            # unit selection (area label)
    "integer":   [("",        "3" )],                            # integer entry (decimal places)
    "d1d2":      [("d1",      "2.000"), ("d2",      "2.000")],   # chamfer / fillet distances
    "rowcol":    [("Rows",    "3"),     ("Cols",    "3")],        # array row/col count
    "dxdy":      [("dX",      "1.000"), ("dY",      "1.000")],   # array x/y spacing
    "count_ang": [("Count",   "6"),     ("Angle°",  "360")],     # polar array
}

# Modes that only accept numeric keystrokes
_NUMERIC_MODES = {"polar", "xy", "en", "value", "d1d2", "rowcol", "dxdy", "count_ang"}


class DynamicInputWidget(QWidget):
    coordinateEntered = pyqtSignal(float, float)   # absolute X, Y
    polarEntered      = pyqtSignal(float, float)   # dist, angle°
    valueEntered      = pyqtSignal(float)
    textEntered       = pyqtSignal(str)             # free-text ("text" mode)

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

    @staticmethod
    def _clean_prompt(text: str) -> str:
        """Strip [key-hint] blocks and <value> tokens — those go to fields, not the label."""
        text = _re.sub(r'\s*\[[^\]]+\]', '', text)
        text = _re.sub(r'\s*<[^>]+>', '', text)
        return text.strip()

    def set_mode(self, mode: str, prompt: str = ""):
        """Called by tool's inputModeChanged signal."""
        self._mode   = mode
        self._active = 0

        if mode == "no_value":
            for w in self._field_widgets:
                self._layout.removeWidget(w)
                w.deleteLater()
            self._field_widgets = []
            self._fields        = []
            self._texts         = []
            self._crs_badge.setVisible(False)
            display_prompt = self._clean_prompt(prompt)
            if display_prompt:
                self._prompt_lbl.setText(display_prompt)
                self._prompt_lbl.show()
            else:
                self._prompt_lbl.hide()
            self.adjustSize()
            self._reposition()
            self.show()
            return

        if not mode or mode not in _CONFIGS:
            self.hide()
            return

        self._crs_badge.setVisible(mode in ("xy", "en"))

        display_prompt = self._clean_prompt(prompt)
        if display_prompt:
            self._prompt_lbl.setText(display_prompt)
            self._prompt_lbl.show()
        else:
            self._prompt_lbl.hide()

        self._rebuild_fields(_CONFIGS[mode])
        self._refresh_fields()
        self._refresh_live_placeholders()   # show current live values immediately
        self.adjustSize()
        self._reposition()
        self.show()

    def set_prompt(self, text: str):
        """Update only the prompt label — does NOT rebuild fields or clear typed text."""
        display_prompt = self._clean_prompt(text)
        if display_prompt:
            self._prompt_lbl.setText(display_prompt)
            self._prompt_lbl.show()
        else:
            self._prompt_lbl.hide()
        self.adjustSize()
        self._extract_live_from_prompt(text)

    def _extract_live_from_prompt(self, text: str):
        """Parse <number[unit]> tokens from prompt and push values into live fields."""
        if not self._fields:
            return
        numbers = _re.findall(r'<([-\d.]+)', text)
        for i, raw in enumerate(numbers):
            if i >= len(self._live):
                break
            try:
                self._live[i] = float(raw)
            except ValueError:  # nosec B110
                pass
        if numbers:
            self._refresh_live_placeholders()

    def set_live_polar(self, dist: float, angle_deg: float):
        """Update live cursor values used as fallbacks when a polar field is empty."""
        self._live = [dist, angle_deg]
        self._refresh_fields()

    def set_live_value(self, v: float):
        """Update live cursor value used as fallback when the value field is empty."""
        if self._live:
            self._live[0] = v
        else:
            self._live = [v]
        self._refresh_fields()

    def set_live_pair(self, a: float, b: float):
        """Set live fallback values for two-field modes (rowcol, dxdy, count_ang, d1d2)."""
        self._live = [a, b]
        self._refresh_fields()

    def update_position(self, canvas_pt=None):
        """Called on every canvas xyCoordinates event — follow the cursor."""
        if self.isVisible():
            self._reposition()

    def _refresh_live_placeholders(self):
        """Show live cursor values as actual field text when the user hasn't typed."""
        if self._mode not in _NUMERIC_MODES:
            return
        for i, field in enumerate(self._fields):
            if i < len(self._texts) and not self._texts[i] and i < len(self._live):
                field.setText(f"{self._live[i]:.3f}")

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
            if self._mode in _NUMERIC_MODES and not (ch.isdigit() or ch in '.-'):
                return False
            self._texts[self._active] += ch
            self._refresh_fields()
            return True

        return False

    # ── internal ──────────────────────────────────────────────────────────
    def _submit(self) -> bool:  # noqa: C901
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

        if mode in ("text", "unit", "integer"):
            text = self._texts[0].strip() if self._texts else ""
            if not text:
                return False  # empty → let CONFIRM reach the tool
            self.textEntered.emit(text)
            self._clear()
            return True

        if mode in ("d1d2", "rowcol", "dxdy", "count_ang"):
            t0 = self._texts[0].strip()
            t1 = self._texts[1].strip()
            if not t0 and not t1:
                return False  # nothing typed — let Enter reach the tool as CONFIRM
            try:
                d1 = float(t0) if t0 else self._live[0]
                d2 = float(t1) if t1 else self._live[1]
            except (ValueError, IndexError):
                return False
            self.coordinateEntered.emit(d1, d2)
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
            typed = self._texts[i] if i < len(self._texts) else ""
            if typed:
                field.setText(typed)
            elif i < len(self._live) and self._mode in _NUMERIC_MODES:
                field.setText(f"{self._live[i]:.3f}")
            else:
                field.setText("")
            field.setStyleSheet(_FIELD_ACTIVE if i == self._active else _FIELD_IDLE)
        self.adjustSize()

    def _reposition(self):
        pos = QCursor.pos()
        self.move(pos.x() + 18, pos.y() + 18)
