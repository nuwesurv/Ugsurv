# -*- coding: utf-8 -*-
"""
FilletTool — set radius/distance → click first edge → click second edge →
auto-commit on second click.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsFeatureRequest, QgsRectangle, QgsFeature,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core.circle_utils import is_circle


def _closest_endpoint(pts: list, click_pt: QgsPointXY) -> QgsPointXY:
    start_d = click_pt.distance(pts[0])
    end_d   = click_pt.distance(pts[-1])
    return pts[0] if start_d < end_d else pts[-1]


def _fillet_two_lines(pts1, pts2, radius: float):
    """Compute a fillet arc between two line-strings at their shared/nearest endpoints."""
    ep1 = _closest_endpoint(pts1, pts2[-1])
    ep2 = _closest_endpoint(pts2, pts1[-1])

    # direction vectors at endpoints
    def _dir(pts, ep):
        if ep == pts[0]:
            d = QgsPointXY(pts[1].x() - pts[0].x(), pts[1].y() - pts[0].y())
        else:
            d = QgsPointXY(pts[-2].x() - pts[-1].x(), pts[-2].y() - pts[-1].y())
        l = math.hypot(d.x(), d.y())
        return QgsPointXY(d.x()/l, d.y()/l) if l > 0 else d

    dir1 = _dir(pts1, ep1)
    dir2 = _dir(pts2, ep2)

    # angle between directions
    cos_a = dir1.x()*dir2.x() + dir1.y()*dir2.y()
    cos_a = max(-1.0, min(1.0, cos_a))
    half_angle = math.acos(cos_a) / 2
    if half_angle < 1e-6:
        return None
    tan_dist = radius / math.tan(half_angle)

    # trim/extend endpoints
    p1_new = QgsPointXY(ep1.x() + dir1.x()*tan_dist, ep1.y() + dir1.y()*tan_dist)
    p2_new = QgsPointXY(ep2.x() + dir2.x()*tan_dist, ep2.y() + dir2.y()*tan_dist)

    # arc center
    bisect_x = dir1.x() + dir2.x()
    bisect_y = dir1.y() + dir2.y()
    bl = math.hypot(bisect_x, bisect_y)
    if bl < 1e-6:
        return None
    center_dist = radius / math.sin(half_angle)
    cx = ep1.x() + (bisect_x/bl) * center_dist
    cy = ep1.y() + (bisect_y/bl) * center_dist
    center = QgsPointXY(cx, cy)

    # arc points
    sa = math.atan2(p1_new.y() - cy, p1_new.x() - cx)
    ea = math.atan2(p2_new.y() - cy, p2_new.x() - cx)
    segs = 18
    arc_pts = []
    for i in range(segs + 1):
        a = sa + (ea - sa) * i / segs
        arc_pts.append(QgsPointXY(cx + radius*math.cos(a), cy + radius*math.sin(a)))

    return p1_new, p2_new, arc_pts


class FilletTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._radius = 1.0
        self._first_pick = None   # (layer, feat, pts)

    def activate(self):
        super().activate()
        self._first_pick = None
        self._transition(ToolState.ACTING)
        self._request_input("value", f"Radius ({self._radius:.3f}) or click first edge:")

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.VALUE_ENTERED:
            self._radius = max(0.0, float(sem.value))

        elif sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point is None:
                return
            if self._first_pick is None:
                self._first_pick = self._pick_line(sem.point)
                if self._first_pick:
                    self._update_prompt("Click second edge:")
            else:
                second = self._pick_line(sem.point)
                if second:
                    self._apply_fillet(self._first_pick, second)
                self._first_pick = None
                self._update_prompt(f"Radius ({self._radius:.3f}) or click first edge:")

        elif sem.type == EventType.CONFIRM:
            self._first_pick = None

    def _pick_line(self, pt: QgsPointXY):
        sm = self._ctx.storage_manager
        lyr = sm.lines_layer
        if not (lyr and lyr.isValid()):
            return None
        tol = 0.01
        for feat in lyr.getFeatures(
            QgsFeatureRequest().setFilterRect(
                QgsRectangle(pt.x()-tol, pt.y()-tol, pt.x()+tol, pt.y()+tol)
            )
        ):
            if is_circle(feat.geometry()):
                continue
            pts = [QgsPointXY(v.x(), v.y()) for v in feat.geometry().vertices()]
            return (lyr, feat, pts)
        return None

    def _apply_fillet(self, fp, sp):
        lyr1, feat1, pts1 = fp
        lyr2, feat2, pts2 = sp
        result = _fillet_two_lines(pts1, pts2, self._radius)
        if result is None:
            return
        p1_new, p2_new, arc_pts = result

        # write arc as a new line feature
        arc_geom = QgsGeometry.fromPolylineXY(arc_pts)
        arc_geom.convertToMultiType()
        if not lyr1.isEditable():
            lyr1.startEditing()
        af = QgsFeature(lyr1.fields())
        af.setGeometry(arc_geom)
        af["cad_layer"] = self._ctx.active_cad_layer
        lyr1.addFeature(af)

    def _on_cancel_hook(self):
        self._first_pick = None
