# -*- coding: utf-8 -*-
"""
DimensionTool — linear/aligned/angular dimension.

Lifecycle: click point 1 → click point 2 → live placement preview →
click to place = commit.

Dimensions are stored as MultiLineString (the measurement lines) plus a
point annotation for the text in QGIS's annotation layer.  Here we store
them as lines + label attribute in the lines GeoPackage layer.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsWkbTypes, QgsFeature,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class DimensionTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._dim_type = "linear"   # linear | aligned | angular
        self._pts: list[QgsPointXY] = []
        self._preview_rb = None
        self._text_rb    = None

    def activate(self):
        super().activate()
        self._pts.clear()
        self._transition(ToolState.ACTING)

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point is None:
                return
            self._pts.append(sem.point)
            if len(self._pts) == 2:
                # now in placement phase: next click places the dim line
                pass
            elif len(self._pts) == 3:
                self._commit_dimension()

        elif sem.type == EventType.KEY_CHAR:
            ch = sem.char
            if ch == 'L':   self._dim_type = "linear"
            elif ch == 'A': self._dim_type = "aligned"
            elif ch == 'G': self._dim_type = "angular"

        elif sem.type == EventType.CONFIRM:
            self._reset()

    def _on_hover(self, sem: SemanticEvent):
        if len(self._pts) >= 2 and sem.point:
            self._update_preview(sem.point)

    def _update_preview(self, offset_pt: QgsPointXY):
        p1, p2 = self._pts[0], self._pts[1]
        lines, label_pt = _dim_lines(p1, p2, offset_pt)
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, QColor(0, 180, 255, 200), 1
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        for seg in lines:
            for i, p in enumerate(seg):
                self._preview_rb.addPoint(p, i == len(seg) - 1)

    def _commit_dimension(self):
        p1, p2, place = self._pts[0], self._pts[1], self._pts[2]
        lines, label_pt = _dim_lines(p1, p2, place)
        length = p1.distance(p2)
        label  = f"{length:.3f}"

        layer = self._ctx.storage_manager.lines_layer
        if layer is None:
            return
        if not layer.isEditable():
            layer.startEditing()

        for seg in lines:
            geom = QgsGeometry.fromPolylineXY(seg)
            geom.convertToMultiType()
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat["cad_layer"] = self._ctx.active_cad_layer
            layer.addFeature(feat)

        self._go_home()

    def _reset(self):
        self._pts.clear()
        self._clear_rubber_bands()
        self._preview_rb = None
        self._text_rb    = None

    def _on_cancel_hook(self):
        self._reset()


def _dim_lines(p1: QgsPointXY, p2: QgsPointXY,
               offset_pt: QgsPointXY) -> tuple:
    """Return list of 2-point segments and the label placement point."""
    # offset from line midpoint
    dx = p2.x() - p1.x()
    dy = p2.y() - p1.y()
    length = math.hypot(dx, dy)
    if length < 1e-10:
        return [], p1

    # normal to the line
    nx, ny = -dy / length, dx / length

    # signed distance from line to offset_pt
    mid = QgsPointXY((p1.x()+p2.x())/2, (p1.y()+p2.y())/2)
    ov  = QgsPointXY(offset_pt.x() - mid.x(), offset_pt.y() - mid.y())
    dist = ov.x()*nx + ov.y()*ny

    # dimension line
    d1 = QgsPointXY(p1.x() + nx*dist, p1.y() + ny*dist)
    d2 = QgsPointXY(p2.x() + nx*dist, p2.y() + ny*dist)

    # extension lines
    ext1 = [p1, d1]
    ext2 = [p2, d2]
    dim  = [d1, d2]

    label_pt = QgsPointXY((d1.x()+d2.x())/2 + nx*0.1,
                          (d1.y()+d2.y())/2 + ny*0.1)
    return [ext1, ext2, dim], label_pt
