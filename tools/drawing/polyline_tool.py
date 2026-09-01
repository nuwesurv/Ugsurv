# -*- coding: utf-8 -*-
"""
PolylineTool — collects N points into a single MultiLineString entity.

Lifecycle: ACTING loops collecting points.  Enter/right-click commits.
C = close (add closing segment back to start).  Ctrl+Z = undo last vertex.
Sub-modes (A=arc, W=width) are toggled mid-ACTING via keypress.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes, QgsFeature

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


class PolylineTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._points: list[QgsPointXY] = []
        self._preview_rb = None
        self._arc_mode = False

    def activate(self):
        super().activate()
        self._points.clear()
        self._arc_mode = False
        self._transition(ToolState.ACTING)
        self._request_input("xy", "Specify start point:")

    # ── events ────────────────────────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point:
                self._add_point(sem.point)

        elif sem.type == EventType.CONFIRM:
            if len(self._points) >= 2:
                self._commit()
            self._go_home()

        elif sem.type == EventType.KEY_CHAR:
            ch = sem.char
            if ch == 'C' and len(self._points) >= 2:
                self._close_and_commit()
            elif ch == 'A':
                self._arc_mode = not self._arc_mode
            elif ch == 'U':
                self._undo_last_vertex()

        elif sem.type == EventType.VALUE_ENTERED:
            pass  # distance-only entry without angle: ignore (user should use polar fields)

    def _on_hover(self, sem: SemanticEvent):
        if self._points and sem.point:
            self._update_preview(sem.point)

    def _on_undo_step(self):
        self._undo_last_vertex()

    # ── logic ─────────────────────────────────────────────────────────────
    def _add_point(self, pt: QgsPointXY):
        self._points.append(pt)
        self._last_input_ref = pt
        for c in self._ctx.constraints:
            c.set_reference(pt)
        # update self-snap provider
        if self._ctx.snap_engine:
            for key, prov in self._ctx.snap_engine._providers.items():
                if hasattr(prov, 'set_sketch_points'):
                    prov.set_sketch_points(self._points)
        # After first point switch to polar mode (Dist + Angle fields)
        if len(self._points) == 1:
            self._request_input("polar", "Specify next point:")
        elif len(self._points) > 1:
            self._request_input("polar", "Specify next point [U=undo C=close]:")

    def _undo_last_vertex(self):
        if self._points:
            self._points.pop()
            self._update_preview(None)

    def _update_preview(self, cursor_pt: QgsPointXY | None):
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.PREVIEW_DRAW, 1
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        pts = list(self._points)
        if cursor_pt:
            pts.append(cursor_pt)
        for i, p in enumerate(pts):
            self._preview_rb.addPoint(p, i == len(pts) - 1)

    def _commit(self):
        if len(self._points) < 2:
            return
        geom = QgsGeometry.fromPolylineXY(self._points)
        self._write_feature(geom)

    def _close_and_commit(self):
        pts = list(self._points) + [self._points[0]]
        geom = QgsGeometry.fromPolylineXY(pts)
        self._write_feature(geom)
        self._go_home()

    def _write_feature(self, geom: QgsGeometry):
        layer = self._ctx.storage_manager.lines_layer
        if layer is None:
            return
        if not layer.isEditable():
            layer.startEditing()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat["cad_layer"] = self._ctx.active_cad_layer
        layer.addFeature(feat)

    def _reset(self):
        self._points.clear()
        self._last_input_ref = None
        self._arc_mode = False
        self._clear_rubber_bands()
        self._preview_rb = None
        if self._ctx.snap_engine:
            for prov in self._ctx.snap_engine._providers.values():
                if hasattr(prov, 'clear'):
                    prov.clear()
        self._request_input("xy", "Specify start point:")

    def _on_cancel_hook(self):
        self._reset()
