# -*- coding: utf-8 -*-
"""
StretchTool — SELECTING via crossing-window only → base point → destination
point → commit.  Only crossing-caught vertices move; edges connecting to
uncaught vertices shorten/lengthen accordingly.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsRectangle,
    QgsWkbTypes, QgsProject, QgsFeatureRequest,
)
from qgis.gui import QgsRubberBand

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core.pending_action import PendingAction
from ...core import style as _style


class StretchTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._crossing_rect: QgsRectangle | None = None
        self._base_pt: QgsPointXY | None = None
        self._caught = {}   # {(layer_id, fid): [vertex_indices]}
        self._drag_start = None
        self._crossing_rb: QgsRubberBand | None = None

    def canvasPressEvent(self, event):
        from qgis.PyQt.QtCore import Qt as _Qt
        if event.button() == _Qt.MouseButton.LeftButton:
            self._drag_start = self._translator._canvas_point(event, self._ctx)
        super().canvasPressEvent(event)

    def canvasReleaseEvent(self, event):
        from qgis.PyQt.QtCore import Qt as _Qt
        if event.button() == _Qt.MouseButton.LeftButton and self._drag_start:
            end = self._translator._canvas_point(event, self._ctx)
            if self._state == ToolState.IDLE:
                self._finish_crossing(self._drag_start, end)
            self._drag_start = None
            if self._crossing_rb:
                self._crossing_rb.reset()
                self._crossing_rb = None

    def canvasMoveEvent(self, event):
        if self._drag_start and self._state == ToolState.IDLE:
            end = self._translator._canvas_point(event, self._ctx)
            self._update_crossing_preview(self._drag_start, end)
        super().canvasMoveEvent(event)

    def _on_event(self, sem: SemanticEvent):
        if self._state == ToolState.ACTING:
            if sem.type == EventType.POINT_PICKED:
                self._apply_stretch(sem.point)

    def _on_hover(self, sem: SemanticEvent):
        if self._state == ToolState.ACTING and self._base_pt and sem.point:
            dx = sem.point.x() - self._base_pt.x()
            dy = sem.point.y() - self._base_pt.y()
            self._update_preview(dx, dy)

    def _finish_crossing(self, start: QgsPointXY, end: QgsPointXY):
        rect = QgsRectangle(
            min(start.x(), end.x()), min(start.y(), end.y()),
            max(start.x(), end.x()), max(start.y(), end.y()),
        )
        self._crossing_rect = rect
        self._caught.clear()
        sm = self._ctx.storage_manager
        for lyr in _geo_layers(sm):
            for feat in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                caught_indices = []
                geom = feat.geometry()
                for i, v in enumerate(geom.vertices()):
                    pt = QgsPointXY(v.x(), v.y())
                    if rect.contains(pt):
                        caught_indices.append(i)
                if caught_indices:
                    self._caught[(lyr.id(), feat.id())] = (lyr, geom, caught_indices)
        if self._caught:
            self._base_pt = None
            self._transition(ToolState.ACTING)
            # rubber-band for preview built on first hover

    def _update_crossing_preview(self, start: QgsPointXY, end: QgsPointXY):
        if self._crossing_rb is None:
            self._crossing_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            self._crossing_rb.setColor(_style.STRETCH_CROSS_BORDER)
            self._crossing_rb.setFillColor(_style.STRETCH_CROSS_FILL)
            self._crossing_rb.setWidth(1)
        rect_geom = QgsGeometry.fromRect(QgsRectangle(start, end))
        self._crossing_rb.setToGeometry(rect_geom)

    def _update_preview(self, dx: float, dy: float):
        self._clear_rubber_bands()
        for (lid, fid), (layer, orig_geom, indices) in self._caught.items():
            pts = [QgsPointXY(v.x(), v.y()) for v in orig_geom.vertices()]
            for i in indices:
                if i < len(pts):
                    pts[i] = QgsPointXY(pts[i].x() + dx, pts[i].y() + dy)
            new_geom = self._rebuild_geometry(orig_geom, pts)
            rb = self._new_rubber_band(
                new_geom.type(),
                _style.PREVIEW_STRETCH, 1
            )
            rb.setToGeometry(new_geom)

    def _apply_stretch(self, dest_pt: QgsPointXY):
        if not self._base_pt:
            self._base_pt = dest_pt
            return
        dx = dest_pt.x() - self._base_pt.x()
        dy = dest_pt.y() - self._base_pt.y()
        for (lid, fid), (layer, orig_geom, indices) in self._caught.items():
            pts = [QgsPointXY(v.x(), v.y()) for v in orig_geom.vertices()]
            for i in indices:
                if i < len(pts):
                    pts[i] = QgsPointXY(pts[i].x() + dx, pts[i].y() + dy)
            new_geom = self._rebuild_geometry(orig_geom, pts)
            if not layer.isEditable():
                layer.startEditing()
            layer.changeGeometry(fid, new_geom)
        self._caught.clear()
        self._transition(ToolState.IDLE)

    @staticmethod
    def _rebuild_geometry(orig: QgsGeometry, new_pts: list) -> QgsGeometry:
        wkb_type = int(QgsWkbTypes.geometryType(orig.wkbType()))
        if wkb_type == 1:   # Line
            return QgsGeometry.fromPolylineXY(new_pts)
        if wkb_type == 2:   # Polygon
            return QgsGeometry.fromPolygonXY([new_pts])
        return orig

    def _on_cancel_hook(self):
        if self._crossing_rb:
            self._crossing_rb.reset()
            self._crossing_rb = None
        self._caught.clear()
        self._drag_start = None


def _geo_layers(sm):
    result = []
    for attr in ("points_layer", "lines_layer", "polygons_layer"):
        lyr = getattr(sm, attr, None)
        if lyr and lyr.isValid():
            result.append(lyr)
    return result
