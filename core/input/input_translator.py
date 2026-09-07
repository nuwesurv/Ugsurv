# -*- coding: utf-8 -*-
"""
InputTranslator — converts raw canvas mouse/key events into SemanticEvents.

Pipeline for mouse-move:
    canvasMoveEvent → SnapEngine.resolve(raw_point) → snapped_point
                    → SemanticEvent(POINT_HOVER, snapped_point, snap_type)

Tools never parse Qt events directly; they consume SemanticEvents only.
"""

import math
import re

from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY

from ..events import SemanticEvent, EventType, SnapType


# Regex for typed coordinate forms: "x,y"  "@dx,dy"  "@dist<angle"
_RE_ABS   = re.compile(r'^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$')
_RE_REL   = re.compile(r'^@\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$')
_RE_POLAR = re.compile(r'^@\s*(\d+\.?\d*)\s*<\s*(-?\d+\.?\d*)$')


class InputTranslator:
    """Stateless translator; holds a reference to the SnapEngine."""

    def __init__(self, snap_engine, constraint_stack=None):
        self._snap  = snap_engine
        self._constraints = constraint_stack or []

    # ── public API ────────────────────────────────────────────────────────
    def translate_press(self, qt_event, tool_context) -> SemanticEvent:
        btn  = qt_event.button()
        mods = int(qt_event.modifiers())
        raw  = self._canvas_point(qt_event, tool_context)
        snap_pt, snap_type = self._resolve(raw, tool_context)
        snap_pt = self._apply_constraints(snap_pt, tool_context)

        if btn == Qt.MouseButton.RightButton:
            return SemanticEvent(EventType.CONFIRM, snap_pt, snap_type,
                                 modifiers=mods, raw=qt_event)

        if mods & Qt.KeyboardModifier.ShiftModifier:
            return SemanticEvent(EventType.SHIFT_CLICK, snap_pt, snap_type,
                                 modifiers=mods, raw=qt_event)

        if mods & Qt.KeyboardModifier.ControlModifier:
            return SemanticEvent(EventType.CTRL_CLICK, snap_pt, snap_type,
                                 modifiers=mods, raw=qt_event)

        return SemanticEvent(EventType.POINT_PICKED, snap_pt, snap_type,
                             modifiers=mods, raw=qt_event)

    def translate_move(self, qt_event, tool_context) -> SemanticEvent:
        raw = self._canvas_point(qt_event, tool_context)
        snap_pt, snap_type = self._resolve(raw, tool_context)
        snap_pt = self._apply_constraints(snap_pt, tool_context)
        return SemanticEvent(EventType.POINT_HOVER, snap_pt, snap_type,
                             raw=qt_event)

    def translate_key(self, qt_event, tool_context) -> SemanticEvent:
        key  = qt_event.key()
        mods = int(qt_event.modifiers())

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            return SemanticEvent(EventType.CONFIRM, modifiers=mods, raw=qt_event)

        if key == Qt.Key.Key_Escape:
            return SemanticEvent(EventType.CANCEL, modifiers=mods, raw=qt_event)

        if key == Qt.Key.Key_Delete or key == Qt.Key.Key_Backspace:
            return SemanticEvent(EventType.DELETE, modifiers=mods, raw=qt_event)

        if key == Qt.Key.Key_Z and (mods & Qt.KeyboardModifier.ControlModifier):
            return SemanticEvent(EventType.UNDO_STEP, modifiers=mods, raw=qt_event)

        ch = qt_event.text().strip()
        if ch:
            return SemanticEvent(EventType.KEY_CHAR, char=ch.upper(),
                                 modifiers=mods, raw=qt_event)

        return None

    def translate_typed_text(self, text: str, last_point, tool_context) -> SemanticEvent:
        """Parse a value typed into the command line or dynamic-input widget."""
        text = text.strip()
        if not text:
            return None

        # Try coordinate forms first
        m = _RE_POLAR.match(text)
        if m:
            dist  = float(m.group(1))
            angle = math.radians(float(m.group(2)))
            if last_point:
                x = last_point.x() + dist * math.cos(angle)
                y = last_point.y() + dist * math.sin(angle)
                return SemanticEvent(EventType.COORDINATE_ENTERED,
                                     point=QgsPointXY(x, y), value=text)
        m = _RE_REL.match(text)
        if m:
            dx, dy = float(m.group(1)), float(m.group(2))
            if last_point:
                x = last_point.x() + dx
                y = last_point.y() + dy
                return SemanticEvent(EventType.COORDINATE_ENTERED,
                                     point=QgsPointXY(x, y), value=text)
        m = _RE_ABS.match(text)
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            return SemanticEvent(EventType.COORDINATE_ENTERED,
                                 point=QgsPointXY(x, y), value=text)

        # Single numeric value (radius, distance, angle…)
        try:
            num = float(text)
            return SemanticEvent(EventType.VALUE_ENTERED, value=num)
        except ValueError:  # nosec B110
            pass

        # Single character key-word (C for close, A for arc, etc.)
        if len(text) == 1:
            return SemanticEvent(EventType.KEY_CHAR, char=text.upper())

        return None

    # ── private ───────────────────────────────────────────────────────────
    @staticmethod
    def _canvas_point(qt_event, tool_context):
        canvas = tool_context.canvas
        return canvas.getCoordinateTransform().toMapCoordinates(
            qt_event.pos().x(), qt_event.pos().y()
        )

    def _resolve(self, raw_point, tool_context):
        if self._snap and tool_context.snap_engine:
            result = tool_context.snap_engine.resolve(raw_point, tool_context.canvas)
            if result:
                return result.point, result.snap_type
        return raw_point, SnapType.NONE

    def _apply_constraints(self, point, tool_context):
        for constraint in tool_context.constraints:
            if constraint.enabled:
                point = constraint.apply(point)
        return point
