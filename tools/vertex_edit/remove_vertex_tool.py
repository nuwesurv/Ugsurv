# -*- coding: utf-8 -*-
"""
RemoveVertexTool — click vertex grip → Enter/Delete removes it; Esc cancels.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsWkbTypes, QgsProject,
    QgsFeatureRequest, QgsRectangle,
    QgsPoint, QgsLineString, QgsCompoundCurve,
)
from qgis.gui import QgsVertexMarker

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


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


class RemoveVertexTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._sel_layer  = None
        self._sel_fid    = None
        self._sel_idx    = None
        self._marker     = None

    def activate(self):
        super().activate()
        self._transition(ToolState.ACTING)
        self._request_input("no_value", "Click a vertex to remove:")

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point:
                self._pick_vertex(sem.point)

        elif sem.type in (EventType.CONFIRM, EventType.DELETE):
            if self._sel_layer is not None:
                self._commit_remove()

    def _pick_vertex(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        tol = 0.02
        rect = QgsRectangle(pt.x()-tol, pt.y()-tol, pt.x()+tol, pt.y()+tol)
        sm_layers = [getattr(sm, "lines_layer", None)]
        all_layers = [l for l in sm_layers if l and l.isValid()]
        all_layers += [l for l in self._ctx.plugin_extra_layers
                       if int(l.geometryType()) == 1]
        for lyr in all_layers:
            for feat in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                for vi, v in enumerate(feat.geometry().vertices()):
                    vpt = QgsPointXY(v.x(), v.y())
                    if math.hypot(vpt.x()-pt.x(), vpt.y()-pt.y()) <= tol:
                        self._sel_layer = lyr
                        self._sel_fid   = feat.id()
                        self._sel_idx   = vi
                        self._show_marker(vpt)
                        return

    def _show_marker(self, pt: QgsPointXY):
        if self._marker:
            self.canvas().scene().removeItem(self._marker)
        m = QgsVertexMarker(self.canvas())
        m.setCenter(pt)
        m.setColor(Qt.GlobalColor.red)
        m.setIconSize(12)
        m.setIconType(QgsVertexMarker.ICON_X)
        self._marker = m

    def _commit_remove(self):
        feat = self._sel_layer.getFeature(self._sel_fid)
        pts  = [QgsPointXY(v.x(), v.y()) for v in feat.geometry().vertices()]

        _EPS = 1e-6
        is_closed = (len(pts) > 1 and
                     math.hypot(pts[0].x() - pts[-1].x(),
                                pts[0].y() - pts[-1].y()) < _EPS)

        if is_closed:
            unique = pts[:-1]           # work with unique vertices, drop closing repeat
            idx    = self._sel_idx
            if idx >= len(unique):      # user clicked the closing phantom — treat as vertex 0
                idx = 0
            if len(unique) <= 3:        # removing would leave a degenerate 2-pt closed shape
                return
            del unique[idx]
            new_pts = unique + [unique[0]]   # re-close: last point mirrors new first
        else:
            if len(pts) <= 2:
                return
            new_pts = pts[:]
            del new_pts[self._sel_idx]

        g = _rebuild_geom(new_pts, self._sel_layer)
        if not self._sel_layer.isEditable():
            self._sel_layer.startEditing()
        self._sel_layer.changeGeometry(self._sel_fid, g)
        self._on_cancel_hook()

    def _on_cancel_hook(self):
        if self._marker:
            try:
                self.canvas().scene().removeItem(self._marker)
            except Exception:  # nosec B110
                pass
            self._marker = None
        self._sel_layer = None
        self._sel_fid   = None
        self._sel_idx   = None
