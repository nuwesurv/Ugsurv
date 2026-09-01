# -*- coding: utf-8 -*-
"""
CircleTool — click center → live radius preview → click/type radius → commit.

Stored as a densified closed LineString in the lines layer.
Supports typed-radius entry.

2-point and 3-point variants are accessible via key press (2 / 3) mid-command.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes, QgsFeature

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


_SEGMENTS = 72   # polygon approximation of circle


def _circle_ring(center: QgsPointXY, radius: float) -> QgsGeometry:
    """Return a closed LineString approximating a circle."""
    pts = []
    for i in range(_SEGMENTS + 1):
        a = 2 * math.pi * i / _SEGMENTS
        pts.append(QgsPointXY(
            center.x() + radius * math.cos(a),
            center.y() + radius * math.sin(a),
        ))
    return QgsGeometry.fromPolylineXY(pts)


def _circle_polygon(center: QgsPointXY, radius: float) -> QgsGeometry:
    """Return a filled polygon for rubber-band preview only."""
    pts = []
    for i in range(_SEGMENTS + 1):
        a = 2 * math.pi * i / _SEGMENTS
        pts.append(QgsPointXY(
            center.x() + radius * math.cos(a),
            center.y() + radius * math.sin(a),
        ))
    return QgsGeometry.fromPolygonXY([pts])


class CircleTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._mode = "center_radius"   # center_radius | 2pt | 3pt
        self._pts: list[QgsPointXY] = []
        self._preview_rb = None

    def activate(self):
        super().activate()
        self._pts.clear()
        self._transition(ToolState.ACTING)
        self._request_input("xy", "Specify center point:")

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point:
                self._handle_point(sem.point)

        elif sem.type == EventType.VALUE_ENTERED and len(self._pts) == 1:
            # typed radius in center_radius mode
            radius = float(sem.value)
            self._commit_circle(self._pts[0], radius)
            self._go_home()

        elif sem.type == EventType.KEY_CHAR:
            if sem.char == '2':
                self._mode = "2pt"; self._pts.clear()
            elif sem.char == '3':
                self._mode = "3pt"; self._pts.clear()

        elif sem.type == EventType.CONFIRM:
            self._reset()

    def _on_hover(self, sem: SemanticEvent):
        if self._pts and sem.point:
            if self._mode == "center_radius" and len(self._pts) == 1:
                radius = self._pts[0].distance(sem.point)
                self._draw_preview(self._pts[0], radius)
            elif self._mode == "2pt" and len(self._pts) == 1:
                ctr = QgsPointXY(
                    (self._pts[0].x() + sem.point.x()) / 2,
                    (self._pts[0].y() + sem.point.y()) / 2,
                )
                r = self._pts[0].distance(sem.point) / 2
                self._draw_preview(ctr, r)

    def _handle_point(self, pt: QgsPointXY):
        self._pts.append(pt)
        if self._mode == "center_radius" and len(self._pts) == 1:
            # Center picked — now ask for radius
            self._last_input_ref = pt
            self._request_input("value", "Specify radius:")
        if self._mode == "center_radius" and len(self._pts) == 2:
            r = self._pts[0].distance(self._pts[1])
            self._commit_circle(self._pts[0], r)
            self._go_home()
        elif self._mode == "2pt" and len(self._pts) == 2:
            ctr = QgsPointXY(
                (self._pts[0].x() + self._pts[1].x()) / 2,
                (self._pts[0].y() + self._pts[1].y()) / 2,
            )
            r = self._pts[0].distance(self._pts[1]) / 2
            self._commit_circle(ctr, r)
            self._go_home()
        elif self._mode == "3pt" and len(self._pts) == 3:
            ctr, r = _circle_from_3pts(*self._pts)
            if ctr and r:
                self._commit_circle(ctr, r)
            self._go_home()

    def _draw_preview(self, center: QgsPointXY, radius: float):
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.PolygonGeometry, _style.RB_DRAW, _style.RB_WIDTH
            )
            self._preview_rb.setFillColor(_style.RB_DRAW_FILL)
        geom = _circle_polygon(center, radius)
        self._preview_rb.setToGeometry(geom)

    def _commit_circle(self, center: QgsPointXY, radius: float):
        if radius <= 0:
            return
        geom = _circle_ring(center, radius)
        self._ctx.storage_manager.add_line(geom, self._ctx.active_cad_layer)

    def _reset(self):
        self._pts.clear()
        self._last_input_ref = None
        self._clear_rubber_bands()
        self._preview_rb = None
        self._request_input("xy", "Specify center point:")

    def _on_cancel_hook(self):
        self._reset()


def _circle_from_3pts(p1, p2, p3):
    """Return (center, radius) from 3 points, or (None, None) if collinear."""
    ax, ay = p1.x(), p1.y()
    bx, by = p2.x(), p2.y()
    cx, cy = p3.x(), p3.y()
    d = 2 * (ax*(by - cy) + bx*(cy - ay) + cx*(ay - by))
    if abs(d) < 1e-10:
        return None, None
    ux = ((ax**2 + ay**2)*(by - cy) + (bx**2 + by**2)*(cy - ay) +
          (cx**2 + cy**2)*(ay - by)) / d
    uy = ((ax**2 + ay**2)*(cx - bx) + (bx**2 + by**2)*(ax - cx) +
          (cx**2 + cy**2)*(bx - ax)) / d
    ctr = QgsPointXY(ux, uy)
    r = math.hypot(ax - ux, ay - uy)
    return ctr, r
