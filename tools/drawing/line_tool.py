# -*- coding: utf-8 -*-
"""
LineTool — draws individual line segments (each click-click is one segment).

Lifecycle: ACTING loops: click point → click point → … → Enter/Esc ends.
Each pair of points commits one MultiLineString feature.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


class LineTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._start_pt: QgsPointXY | None = None
        self._preview_rb = None

    def activate(self):
        super().activate()
        self._start_pt = None
        self._transition(ToolState.ACTING)

    # ── events ────────────────────────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.COORDINATE_ENTERED and sem.point:
            self._handle_point(sem.point)
        elif sem.type == EventType.POINT_PICKED and sem.point:
            self._handle_point(sem.point)
        elif sem.type == EventType.CONFIRM:
            self._start_pt = None
            self._clear_rubber_bands()
            self._transition(ToolState.IDLE)
            self._go_home()
        elif sem.type == EventType.VALUE_ENTERED and self._start_pt:
            pt = self._try_extension_distance(sem.value)
            if pt:
                self._handle_point(pt)

    def _on_hover(self, sem: SemanticEvent):
        if self._start_pt and sem.point:
            self._update_preview(sem.point)
        self._update_extension_guide(sem.snap_type, sem.point)

    def _on_undo_step(self):
        self._start_pt = None
        self._clear_rubber_bands()

    # ── logic ─────────────────────────────────────────────────────────────
    def _handle_point(self, pt: QgsPointXY):
        if self._start_pt is None:
            self._start_pt = pt
            # update ortho/polar reference
            for c in self._ctx.constraints:
                c.set_reference(pt)
        else:
            self._commit_segment(self._start_pt, pt)
            self._start_pt = pt
            for c in self._ctx.constraints:
                c.set_reference(pt)
            self._clear_rubber_bands()
            self._preview_rb = None

    def _update_preview(self, cursor_pt: QgsPointXY):
        if not self._start_pt:
            return
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_DRAW, _style.RB_WIDTH
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        self._preview_rb.addPoint(self._start_pt)
        self._preview_rb.addPoint(cursor_pt)

    def _commit_segment(self, p1: QgsPointXY, p2: QgsPointXY):
        if abs(p1.x() - p2.x()) < 1e-10 and abs(p1.y() - p2.y()) < 1e-10:
            return  # zero-length, skip
        geom = QgsGeometry.fromPolylineXY([p1, p2])
        factory = self._ctx.storage_manager
        ef = self._ctx.entity_factory if hasattr(self._ctx, 'entity_factory') else None
        if ef:
            ef.commit(geom, self._ctx.active_cad_layer)
        else:
            factory.add_line(geom, self._ctx.active_cad_layer)

    def _on_cancel_hook(self):
        self._start_pt = None
        self._preview_rb = None
