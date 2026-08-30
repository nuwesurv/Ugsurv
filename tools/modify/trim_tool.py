# -*- coding: utf-8 -*-
"""
TrimTool — SELECTING boundary edges (Enter confirms) → click target segments.
Each click trims the segment at the first boundary intersection.
"""

from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsFeatureRequest,
    QgsWkbTypes, QgsProject,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class TrimTool(BaseTool):
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
                self._trim_at(sem.point)
            elif sem.type == EventType.CONFIRM:
                self._boundaries.clear()
                self._transition(ToolState.IDLE)

    def _pick_boundary(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        for lyr in _line_layers(sm):
            for feat in lyr.getFeatures(_rect_around(pt, 0.01)):
                self._boundaries.append(feat.geometry())

    def _trim_at(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        for lyr in _line_layers(sm):
            for feat in lyr.getFeatures(_rect_around(pt, 0.01)):
                geom = feat.geometry()
                for bnd in self._boundaries:
                    inter = geom.intersection(bnd)
                    if inter.isEmpty():
                        continue
                    # split the geom at the intersection, keep the far part
                    parts = _split_at_intersection(geom, inter)
                    if len(parts) == 2:
                        # keep the part that does NOT contain the click point
                        keep = parts[1] if parts[0].contains(
                            QgsGeometry.fromPointXY(pt)) else parts[0]
                        keep.convertToMultiType()
                        if not lyr.isEditable():
                            lyr.startEditing()
                        lyr.changeGeometry(feat.id(), keep)
                        return


def _line_layers(sm):
    lyr = getattr(sm, "lines_layer", None)
    return [lyr] if lyr and lyr.isValid() else []


def _rect_around(pt: QgsPointXY, tol: float):
    from qgis.core import QgsRectangle
    return QgsFeatureRequest().setFilterRect(
        QgsRectangle(pt.x()-tol, pt.y()-tol, pt.x()+tol, pt.y()+tol)
    )


def _split_at_intersection(geom: QgsGeometry, inter: QgsGeometry) -> list:
    """Simple split: return two sub-geometries at the first intersection point."""
    pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
    if not pts:
        return [geom]
    inter_pt = inter.asPoint() if inter.wkbType() in (
        QgsWkbTypes.Point, QgsWkbTypes.MultiPoint) else inter.centroid().asPoint()
    ipt = QgsPointXY(inter_pt.x(), inter_pt.y())
    # find insertion index
    best_i, best_d = 0, float('inf')
    import math
    for i in range(len(pts) - 1):
        d = QgsGeometry.fromPolylineXY([pts[i], pts[i+1]]).distance(
            QgsGeometry.fromPointXY(ipt))
        if d < best_d:
            best_d = d
            best_i = i
    part1 = pts[:best_i+1] + [ipt]
    part2 = [ipt] + pts[best_i+1:]
    g1 = QgsGeometry.fromPolylineXY(part1) if len(part1) >= 2 else QgsGeometry()
    g2 = QgsGeometry.fromPolylineXY(part2) if len(part2) >= 2 else QgsGeometry()
    return [g1, g2]
