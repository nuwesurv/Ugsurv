# -*- coding: utf-8 -*-
"""
BreakTool — click object → click break point(s) → commit implicit on final click.

Single-point break: splits line at one location.
Two-point break:    removes segment between the two points (key '2' mid-command).
"""

from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsWkbTypes,
    QgsFeatureRequest, QgsRectangle, QgsFeature,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ..modify.trim_tool import _split_at_intersection


class BreakTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._mode = "1pt"   # "1pt" | "2pt"
        self._target_layer = None
        self._target_fid   = None
        self._target_geom  = None
        self._bp1: QgsPointXY | None = None

    def activate(self):
        super().activate()
        self._transition(ToolState.ACTING)

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.KEY_CHAR:
            if sem.char == '2': self._mode = "2pt"
            elif sem.char == '1': self._mode = "1pt"

        elif sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point is None:
                return
            if self._target_geom is None:
                self._pick_object(sem.point)
            else:
                self._handle_break_point(sem.point)

        elif sem.type == EventType.CONFIRM:
            self._reset()

    def _pick_object(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        tol = 0.02
        rect = QgsRectangle(pt.x()-tol, pt.y()-tol, pt.x()+tol, pt.y()+tol)
        lyr = sm.lines_layer
        if not (lyr and lyr.isValid()):
            return
        for feat in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
            self._target_layer = lyr
            self._target_fid   = feat.id()
            self._target_geom  = feat.geometry()
            return

    def _handle_break_point(self, pt: QgsPointXY):
        if self._mode == "1pt":
            self._do_break_1pt(pt)
            self._reset()
        else:
            if self._bp1 is None:
                self._bp1 = pt
            else:
                self._do_break_2pt(self._bp1, pt)
                self._reset()

    def _do_break_1pt(self, pt: QgsPointXY):
        snap_pt = QgsGeometry.fromPointXY(pt)
        inter   = self._target_geom.intersection(
            snap_pt.buffer(0.001, 4)
        )
        if inter.isEmpty():
            return
        parts = _split_at_intersection(self._target_geom, snap_pt)
        self._write_parts(parts)

    def _do_break_2pt(self, p1: QgsPointXY, p2: QgsPointXY):
        pts = [QgsPointXY(v.x(), v.y()) for v in self._target_geom.vertices()]
        # find indices nearest to p1 and p2
        idx1 = _nearest_pt_index(pts, p1)
        idx2 = _nearest_pt_index(pts, p2)
        if idx1 > idx2:
            idx1, idx2 = idx2, idx1
        part = pts[:idx1+1] + pts[idx2:]
        if len(part) >= 2:
            g = QgsGeometry.fromPolylineXY(part)
            g.convertToMultiType()
            if not self._target_layer.isEditable():
                self._target_layer.startEditing()
            self._target_layer.changeGeometry(self._target_fid, g)

    def _write_parts(self, parts: list):
        if not parts:
            return
        lyr = self._target_layer
        if not lyr.isEditable():
            lyr.startEditing()
        first = True
        for p in parts:
            if p.isEmpty():
                continue
            p.convertToMultiType()
            if first:
                lyr.changeGeometry(self._target_fid, p)
                first = False
            else:
                feat = QgsFeature(lyr.fields())
                feat.setGeometry(p)
                feat["cad_layer"] = self._ctx.active_cad_layer
                lyr.addFeature(feat)

    def _reset(self):
        self._target_layer = None
        self._target_fid   = None
        self._target_geom  = None
        self._bp1          = None

    def _on_cancel_hook(self):
        self._reset()


def _nearest_pt_index(pts: list, pt: QgsPointXY) -> int:
    import math
    best_i, best_d = 0, float('inf')
    for i, p in enumerate(pts):
        d = math.hypot(p.x()-pt.x(), p.y()-pt.y())
        if d < best_d:
            best_d = d
            best_i = i
    return best_i
