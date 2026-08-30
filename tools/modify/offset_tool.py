# -*- coding: utf-8 -*-
"""
OffsetTool — type/click distance → click object → click side → commit.
Loops until Esc.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsFeature,
    QgsFeatureRequest, QgsProject,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class OffsetTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._distance: float = 1.0
        self._target_fid = None
        self._target_layer = None
        self._target_geom = None
        self._preview_rb = None

    def activate(self):
        super().activate()
        self._transition(ToolState.ACTING)

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.VALUE_ENTERED:
            self._distance = abs(float(sem.value))

        elif sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point is None:
                return
            if self._target_geom is None:
                self._pick_object(sem.point)
            else:
                self._apply_offset(sem.point)

        elif sem.type == EventType.CONFIRM:
            self._target_geom = None
            self._clear_rubber_bands()
            self._preview_rb = None

    def _on_hover(self, sem: SemanticEvent):
        if self._target_geom and sem.point:
            self._draw_offset_preview(sem.point)

    def _pick_object(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        tol = 0.01
        rect_geom = QgsGeometry.fromPointXY(pt).buffer(tol, 8).boundingBox()
        for attr in ("lines_layer", "polygons_layer"):
            lyr = getattr(sm, attr, None)
            if lyr and lyr.isValid():
                for feat in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect_geom)):
                    self._target_layer = lyr
                    self._target_fid   = feat.id()
                    self._target_geom  = feat.geometry()
                    return

    def _draw_offset_preview(self, side_pt: QgsPointXY):
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                self._target_geom.type(), QColor(255, 165, 0, 200), 1
            )
        # determine sign of offset from side click
        side = _side_of_geom(self._target_geom, side_pt)
        dist = self._distance * side
        off = self._target_geom.offsetCurve(dist, 8, 0, 0)
        if off and not off.isEmpty():
            self._preview_rb.setToGeometry(off)

    def _apply_offset(self, side_pt: QgsPointXY):
        if self._target_geom is None or self._target_layer is None:
            return
        side = _side_of_geom(self._target_geom, side_pt)
        dist = self._distance * side
        new_geom = self._target_geom.offsetCurve(dist, 8, 0, 0)
        if new_geom and not new_geom.isEmpty():
            new_geom.convertToMultiType()
            if not self._target_layer.isEditable():
                self._target_layer.startEditing()
            feat = QgsFeature(self._target_layer.fields())
            feat.setGeometry(new_geom)
            feat["cad_layer"] = self._ctx.active_cad_layer
            self._target_layer.addFeature(feat)
        self._target_geom = None
        self._clear_rubber_bands()
        self._preview_rb = None
        # loop — remain in ACTING for next offset

    def _on_cancel_hook(self):
        self._target_geom = None
        self._preview_rb = None


def _side_of_geom(geom: QgsGeometry, pt: QgsPointXY) -> int:
    """Return +1 or -1 based on which side of the geometry pt lies."""
    nearest = geom.nearestPoint(QgsGeometry.fromPointXY(pt))
    dx = pt.x() - nearest.asPoint().x()
    dy = pt.y() - nearest.asPoint().y()
    # use cross product of segment direction vs (dx,dy)
    verts = list(geom.vertices())
    if len(verts) >= 2:
        sx = verts[1].x() - verts[0].x()
        sy = verts[1].y() - verts[0].y()
        cross = sx*dy - sy*dx
        return 1 if cross >= 0 else -1
    return 1
