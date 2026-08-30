# -*- coding: utf-8 -*-
"""
DynamicInputWidget — frameless floating widget near the cursor, showing
live distance/angle/coordinate fields during ACTING.

Tab cycles fields.  Values typed here are routed through InputTranslator
as VALUE_ENTERED or COORDINATE_ENTERED events.

Reuses QgsAdvancedDigitizingDockWidget's coordinate-parsing API where
available, or falls back to InputTranslator.translate_typed_text().
"""

from qgis.PyQt.QtWidgets import QWidget, QHBoxLayout, QLineEdit, QLabel
from qgis.PyQt.QtCore import Qt, pyqtSignal, QPoint
from qgis.PyQt.QtGui import QFont, QColor, QPalette, QCursor
from qgis.core import QgsPointXY


class DynamicInputWidget(QWidget):
    valueEntered = pyqtSignal(str)   # text committed by the user

    def __init__(self, canvas, input_translator, parent=None):
        super().__init__(parent, Qt.WindowType.Tool |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self._canvas      = canvas
        self._translator  = input_translator
        self._last_pt     = None
        self._fields: list[QLineEdit] = []
        self._active_field = 0

        self._build_ui()
        self.hide()
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _build_ui(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(4)

        font = QFont("Consolas", 9)
        bg   = QColor(40, 40, 40, 220)

        for field_name in ["X:", "Y:"]:
            lbl = QLabel(field_name)
            lbl.setFont(font)
            lbl.setStyleSheet("color:#aaa")
            lay.addWidget(lbl)

            ed = QLineEdit()
            ed.setFont(font)
            ed.setFixedWidth(90)
            ed.setStyleSheet(
                "background:#282828; color:#fff; border:1px solid #555; padding:1px;"
            )
            ed.returnPressed.connect(self._on_commit)
            self._fields.append(ed)
            lay.addWidget(ed)

        self.setStyleSheet("background:rgba(40,40,40,200); border-radius:3px;")
        self.adjustSize()

    # ── public API ────────────────────────────────────────────────────────
    def update_position(self, canvas_pt: QgsPointXY):
        """Move widget near the canvas cursor position."""
        self._last_pt = canvas_pt
        cursor_screen = QCursor.pos()
        self.move(cursor_screen.x() + 16, cursor_screen.y() + 16)
        # Update X/Y display
        if len(self._fields) >= 2:
            if not self._fields[0].hasFocus():
                self._fields[0].setText(f"{canvas_pt.x():.3f}")
            if not self._fields[1].hasFocus():
                self._fields[1].setText(f"{canvas_pt.y():.3f}")

    def show_for_tool(self, tool_is_active: bool):
        if tool_is_active:
            self.show()
        else:
            self.hide()

    # ── Tab cycling ──────────────────────────────────────────────────────
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Tab:
            self._active_field = (self._active_field + 1) % len(self._fields)
            self._fields[self._active_field].setFocus()
            return
        super().keyPressEvent(event)

    def _on_commit(self):
        vals = [f.text().strip() for f in self._fields]
        text = ",".join(v for v in vals if v)
        if text:
            self.valueEntered.emit(text)
            for f in self._fields:
                f.clear()
