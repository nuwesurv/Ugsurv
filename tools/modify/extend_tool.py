# -*- coding: utf-8 -*-
"""
ExtendTool — SELECTING boundary edges → click target endpoints to extend
to nearest boundary.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsFeatureRequest, QgsRectangle,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class ExtendTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._boundaries: list[QgsGeometry] = []

    def activate(self):
        super().activate()
        self._boundaries.clear()
        self._transition(ToolState.SELECTING)

    def _on_event(self, sem: SemanticEvent):
        if self._state == ToolState.SELECTING:
            if sem.type == EventType.CONFIRM:
                if self._boundaries:
                    self._transition(ToolState.ACTING)
            elif sem.type == EventType.POINT_PICKED and sem.point:
                self._pick_boundary(sem.point)

        elif self._state == ToolState.ACTING:
            if sem.type == EventType.POINT_PICKED and sem.point:
                self._extend_at(sem.point)
            elif sem.type == EventType.CONFIRM:
                self._boundaries.clear()
                self._transition(ToolState.IDLE)

    def _pick_boundary(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        for lyr in _line_layers(sm):
            for feat in lyr.getFeatures(_rect_q(pt, 0.01)):
                self._boundaries.append(feat.geometry())

    def _extend_at(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        for lyr in _line_layers(sm):
            for feat in lyr.getFeatures(_rect_q(pt, 0.01)):
                geom = feat.geometry()
                pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
                if len(pts) < 2:
                    continue
                # determine which end is closest to click
                d_start = pts[0].distance(pt)
                d_end   = pts[-1].distance(pt)
                extend_end = d_end < d_start

                extended = _extend_geometry(pts, self._boundaries, extend_end)
                if extended:
                    new_geom = QgsGeometry.fromPolylineXY(extended)
                    new_geom.convertToMultiType()
                    if not lyr.isEditable():
                        lyr.startEditing()
                    lyr.changeGeometry(feat.id(), new_geom)
                return


def _extend_geometry(pts, boundaries, extend_end: bool):
    """Extend pts towards nearest boundary. Returns new pts list or None."""
    import math
    if extend_end:
        tip = pts[-1]
        prev = pts[-2]
    else:
        tip = pts[0]
        prev = pts[1]
    dx = tip.x() - prev.x()
    dy = tip.y() - prev.y()
    length = math.hypot(dx, dy)
    if length < 1e-10:
        return None
    dx /= length; dy /= length

    # extend far enough to intersect boundaries
    MAX = 1e6
    ray = QgsGeometry.fromPolylineXY([
        tip, QgsPointXY(tip.x() + dx*MAX, tip.y() + dy*MAX)
    ])
    best_dist = MAX
    best_pt = None
    for bnd in boundaries:
        inter = ray.intersection(bnd)
        if inter.isEmpty():
            continue
        ip = inter.asPoint()
        d = tip.distance(QgsPointXY(ip.x(), ip.y()))
        if d < best_dist:
            best_dist = d
            best_pt = QgsPointXY(ip.x(), ip.y())

    if best_pt is None:
        return None
    if extend_end:
        return pts[:-1] + [best_pt]
    else:
        return [best_pt] + pts[1:]


def _line_layers(sm):
    lyr = getattr(sm, "lines_layer", None)
    return [lyr] if lyr and lyr.isValid() else []


def _rect_q(pt: QgsPointXY, tol: float):
    from qgis.core import QgsRectangle
    return QgsFeatureRequest().setFilterRect(
        QgsRectangle(pt.x()-tol, pt.y()-tol, pt.x()+tol, pt.y()+tol)
    )
