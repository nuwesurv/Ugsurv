# -*- coding: utf-8 -*-
"""
PolygonTool (regular polygon) — specify side count → click center →
click/type circumradius → commit.

Stored as a closed LineString in the lines layer.
Default: inscribed (circumradius).  'I' key toggles to edge-midpoint
(inradius) mode.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes, QgsFeature

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


def _regular_polygon_ring(center: QgsPointXY, radius: float, sides: int,
                          inscribed: bool = True) -> QgsGeometry:
    """Return a closed LineString for the regular polygon boundary."""
    if not inscribed:
        radius = radius / math.cos(math.pi / sides)
    pts = []
    for i in range(sides + 1):
        a = 2 * math.pi * i / sides
        pts.append(QgsPointXY(
            center.x() + radius * math.cos(a),
            center.y() + radius * math.sin(a),
        ))
    return QgsGeometry.fromPolylineXY(pts)


def _regular_polygon(center: QgsPointXY, radius: float, sides: int,
                     inscribed: bool = True) -> QgsGeometry:
    """Return a filled polygon for rubber-band preview only."""
    if not inscribed:
        radius = radius / math.cos(math.pi / sides)
    pts = []
    for i in range(sides + 1):
        a = 2 * math.pi * i / sides
        pts.append(QgsPointXY(
            center.x() + radius * math.cos(a),
            center.y() + radius * math.sin(a),
        ))
    return QgsGeometry.fromPolygonXY([pts])


class PolygonTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._sides    = 6
        self._center: QgsPointXY | None = None
        self._inscribed = True
        self._preview_rb = None

    def activate(self):
        super().activate()
        self._center = None
        self._transition(ToolState.ACTING)

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.VALUE_ENTERED:
            v = int(sem.value)
            if v >= 3:
                self._sides = v

        elif sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point is None:
                return
            if self._center is None:
                self._center = sem.point
            else:
                radius = self._center.distance(sem.point)
                self._commit(self._center, radius)
                self._center = None
                self._clear_rubber_bands()
                self._preview_rb = None

        elif sem.type == EventType.KEY_CHAR and sem.char == 'I':
            self._inscribed = not self._inscribed

        elif sem.type == EventType.CONFIRM:
            self._center = None
            self._clear_rubber_bands()
            self._preview_rb = None

    def _on_hover(self, sem: SemanticEvent):
        if self._center and sem.point:
            r = self._center.distance(sem.point)
            if self._preview_rb is None:
                self._preview_rb = self._new_rubber_band(
                    QgsWkbTypes.PolygonGeometry, QColor(255, 165, 0, 150), 1
                )
            geom = _regular_polygon(self._center, r, self._sides, self._inscribed)
            self._preview_rb.setToGeometry(geom)

    def _commit(self, center: QgsPointXY, radius: float):
        if radius <= 0:
            return
        geom = _regular_polygon_ring(center, radius, self._sides, self._inscribed)
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
        self._center = None
        self._preview_rb = None
