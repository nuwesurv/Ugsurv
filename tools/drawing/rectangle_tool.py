# -*- coding: utf-8 -*-
"""
RectangleTool — click corner 1 → live preview → click corner 2 → commit.

Produces a closed LineString stored in the lines layer.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsRectangle,
    QgsWkbTypes, QgsFeature,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class RectangleTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._corner1: QgsPointXY | None = None
        self._preview_rb = None

    def activate(self):
        super().activate()
        self._corner1 = None
        self._transition(ToolState.ACTING)

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point is None:
                return
            if self._corner1 is None:
                self._corner1 = sem.point
                for c in self._ctx.constraints:
                    c.set_reference(sem.point)
            else:
                self._commit(self._corner1, sem.point)
                self._corner1 = None
                self._clear_rubber_bands()
                self._preview_rb = None

        elif sem.type == EventType.CONFIRM:
            self._corner1 = None
            self._clear_rubber_bands()
            self._preview_rb = None

    def _on_hover(self, sem: SemanticEvent):
        if self._corner1 and sem.point:
            self._update_preview(self._corner1, sem.point)

    def _update_preview(self, c1: QgsPointXY, c2: QgsPointXY):
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.PolygonGeometry, QColor(255, 165, 0, 150), 1
            )
        geom = QgsGeometry.fromRect(QgsRectangle(c1, c2))
        self._preview_rb.setToGeometry(geom)

    def _commit(self, c1: QgsPointXY, c2: QgsPointXY):
        poly_geom = QgsGeometry.fromRect(QgsRectangle(c1, c2))
        ring = poly_geom.asPolygon()[0]   # exterior ring (closed, last == first)
        geom = QgsGeometry.fromPolylineXY(ring)
        layer = self._ctx.storage_manager.lines_layer
        if layer is None:
            return
        if not layer.isEditable():
            layer.startEditing()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat["cad_layer"] = self._ctx.active_cad_layer
        layer.addFeature(feat)

    def _on_cancel_hook(self):
        self._corner1 = None
        self._preview_rb = None
