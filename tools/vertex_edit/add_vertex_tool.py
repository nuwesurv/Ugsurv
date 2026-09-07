# -*- coding: utf-8 -*-
"""
AddVertexTool — click existing segment → new vertex inserted at that point,
immediately draggable → click/Enter commits position.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsWkbTypes,
    QgsProject, QgsFeatureRequest, QgsRectangle,
    QgsPoint, QgsLineString, QgsCompoundCurve,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


def _rebuild_geom(pts: list, layer) -> QgsGeometry:
    """Reconstruct geometry matching the layer's WKB type from a list of QgsPointXY."""
    if layer.wkbType() == QgsWkbTypes.CompoundCurve:
        qp = [QgsPoint(p.x(), p.y()) for p in pts]
        ls = QgsLineString(qp)
        cc = QgsCompoundCurve()
        cc.addCurve(ls)
        return QgsGeometry(cc)
    g = QgsGeometry.fromPolylineXY(pts)
    if QgsWkbTypes.isMultiType(layer.wkbType()):
        g.convertToMultiType()
    return g


class AddVertexTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._pending_layer = None
        self._pending_fid   = None
        self._insert_idx    = None
        self._new_pt: QgsPointXY | None = None
        self._preview_rb    = None

    def activate(self):
        super().activate()
        self._transition(ToolState.ACTING)
        self._request_input("xy", "Click on a line to add a vertex:")

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point is None:
                return
            if self._pending_layer is None:
                self._pick_segment(sem.point)
            else:
                self._commit_insert(sem.point)

        elif sem.type == EventType.CONFIRM and self._new_pt:
            self._commit_insert(self._new_pt)

    def _on_hover(self, sem: SemanticEvent):
        if sem.point:
            dyn = getattr(self._ctx, 'dyn_widget', None)
            if dyn is not None:
                dyn.set_live_pair(sem.point.x(), sem.point.y())
        if self._pending_layer and sem.point:
            self._new_pt = sem.point
            self._update_preview(sem.point)

    def _pick_segment(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        tol = 0.02
        sm_layers = [getattr(sm, "lines_layer", None)]
        all_layers = [l for l in sm_layers if l and l.isValid()]
        all_layers += [l for l in self._ctx.plugin_extra_layers
                       if int(l.geometryType()) == 1]   # line geometry only
        for lyr in all_layers:
            for feat in lyr.getFeatures(
                QgsFeatureRequest().setFilterRect(
                    QgsRectangle(pt.x()-tol, pt.y()-tol, pt.x()+tol, pt.y()+tol)
                )
            ):
                pts = [QgsPointXY(v.x(), v.y()) for v in feat.geometry().vertices()]
                idx = _nearest_segment_index(pts, pt)
                if idx is not None:
                    self._pending_layer = lyr
                    self._pending_fid   = feat.id()
                    self._insert_idx    = idx + 1
                    self._update_prompt("Click to place new vertex:")
                    return

    def _update_preview(self, cursor_pt: QgsPointXY):
        if self._pending_layer is None:
            return
        feat = self._pending_layer.getFeature(self._pending_fid)
        pts  = [QgsPointXY(v.x(), v.y()) for v in feat.geometry().vertices()]
        new_pts = pts[:self._insert_idx] + [cursor_pt] + pts[self._insert_idx:]
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.PREVIEW_DRAW, _style.RB_WIDTH
            )
        g = QgsGeometry.fromPolylineXY(new_pts)
        self._preview_rb.setToGeometry(g)

    def _commit_insert(self, pt: QgsPointXY):
        if self._pending_layer is None:
            return
        feat = self._pending_layer.getFeature(self._pending_fid)
        pts  = [QgsPointXY(v.x(), v.y()) for v in feat.geometry().vertices()]
        new_pts = pts[:self._insert_idx] + [pt] + pts[self._insert_idx:]
        g = _rebuild_geom(new_pts, self._pending_layer)
        if not self._pending_layer.isEditable():
            self._pending_layer.startEditing()
        self._pending_layer.changeGeometry(self._pending_fid, g)
        self._pending_layer = None
        self._pending_fid   = None
        self._insert_idx    = None
        self._new_pt        = None
        self._clear_rubber_bands()
        self._preview_rb = None

    def _on_cancel_hook(self):
        self._pending_layer = None
        self._pending_fid   = None
        self._insert_idx    = None
        self._new_pt        = None
        self._preview_rb    = None


def _nearest_segment_index(pts: list, pt: QgsPointXY) -> int | None:
    best_i, best_d = None, float('inf')
    for i in range(len(pts) - 1):
        d = QgsGeometry.fromPolylineXY([pts[i], pts[i+1]]).distance(
            QgsGeometry.fromPointXY(pt)
        )
        if d < best_d:
            best_d = d
            best_i = i
    return best_i
