# -*- coding: utf-8 -*-
"""
RemoveVertexTool — click vertex grip → Enter/Delete removes it; Esc cancels.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsWkbTypes, QgsProject,
    QgsFeatureRequest, QgsRectangle,
)
from qgis.gui import QgsVertexMarker

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from .grip_edit_tool import _replace_vertex


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
        for attr in ("lines_layer", "polygons_layer"):
            lyr = getattr(sm, attr, None)
            if not (lyr and lyr.isValid()):
                continue
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
        if len(pts) <= 2:
            return  # can't remove below 2 vertices
        del pts[self._sel_idx]
        wtype = int(QgsWkbTypes.geometryType(feat.geometry().wkbType()))
        if wtype == 1:  # Line
            g = QgsGeometry.fromPolylineXY(pts)
            g.convertToMultiType()
        else:
            g = QgsGeometry.fromPolygonXY([pts])
            g.convertToMultiType()
        if not self._sel_layer.isEditable():
            self._sel_layer.startEditing()
        self._sel_layer.changeGeometry(self._sel_fid, g)
        self._on_cancel_hook()

    def _on_cancel_hook(self):
        if self._marker:
            try:
                self.canvas().scene().removeItem(self._marker)
            except Exception:
                pass
            self._marker = None
        self._sel_layer = None
        self._sel_fid   = None
        self._sel_idx   = None
