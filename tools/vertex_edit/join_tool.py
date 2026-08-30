# -*- coding: utf-8 -*-
"""
JoinTool — SELECTING multiple line objects sharing endpoints (Enter confirms)
→ immediate commit, merges them into one polyline.
"""

from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsProject, QgsWkbTypes, QgsFeature,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class JoinTool(BaseTool):
    CURSOR = Qt.CursorShape.ArrowCursor

    def activate(self):
        super().activate()
        self._transition(ToolState.SELECTING)

    def _on_event(self, sem: SemanticEvent):
        if self._state == ToolState.SELECTING:
            if sem.type == EventType.CONFIRM:
                self._do_join()

    def _do_join(self):
        sel = self._ctx.selection_model
        if len(sel) < 2:
            return

        # collect lines in selection
        lines = []
        first_layer = None
        for lid, fid in sel:
            layer = QgsProject.instance().mapLayer(lid)
            if layer is None:
                continue
            feat = layer.getFeature(fid)
            if not feat.isValid():
                continue
            if int(QgsWkbTypes.geometryType(feat.geometry().wkbType())) != 1:  # not Line
                continue
            lines.append((layer, feat, [QgsPointXY(v.x(), v.y())
                                        for v in feat.geometry().vertices()]))
            if first_layer is None:
                first_layer = (layer, feat)

        if len(lines) < 2 or first_layer is None:
            return

        # simple ordered join: chain from the first line's endpoint
        joined = _chain_lines([pts for _, _, pts in lines])
        if joined is None:
            return

        geom = QgsGeometry.fromPolylineXY(joined)
        geom.convertToMultiType()

        main_layer, main_feat = first_layer
        if not main_layer.isEditable():
            main_layer.startEditing()
        main_layer.changeGeometry(main_feat.id(), geom)

        # delete the remaining selected features
        for layer, feat, _ in lines[1:]:
            if not layer.isEditable():
                layer.startEditing()
            layer.deleteFeature(feat.id())

        sel.clear()
        self._transition(ToolState.IDLE)


def _chain_lines(line_pts_list: list) -> list | None:
    """Order and join a list of point-lists end-to-end. Returns flat list or None."""
    if not line_pts_list:
        return None
    result = list(line_pts_list[0])
    remaining = list(line_pts_list[1:])
    TOL = 0.001
    while remaining:
        matched = False
        for i, pts in enumerate(remaining):
            if _close(result[-1], pts[0], TOL):
                result.extend(pts[1:])
                remaining.pop(i)
                matched = True
                break
            elif _close(result[-1], pts[-1], TOL):
                result.extend(reversed(pts[:-1]))
                remaining.pop(i)
                matched = True
                break
            elif _close(result[0], pts[-1], TOL):
                result = list(pts) + result[1:]
                remaining.pop(i)
                matched = True
                break
            elif _close(result[0], pts[0], TOL):
                result = list(reversed(pts)) + result[1:]
                remaining.pop(i)
                matched = True
                break
        if not matched:
            return None
    return result


def _close(a: QgsPointXY, b: QgsPointXY, tol: float) -> bool:
    import math
    return math.hypot(a.x()-b.x(), a.y()-b.y()) <= tol
