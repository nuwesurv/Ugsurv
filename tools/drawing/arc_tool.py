# -*- coding: utf-8 -*-
"""
ArcTool — 3-point default (start, end, point-on-arc).

Also supports center+angle variant (key 'C' mid-command switches mode).
Arc is densified into a MultiLineString.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes, QgsFeature

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


def _arc_points(center: QgsPointXY, radius: float,
                start_angle: float, end_angle: float,
                segments: int = 36) -> list:
    """Return densified arc as a list of QgsPointXY."""
    # normalise direction: short arc
    while end_angle < start_angle:
        end_angle += 2 * math.pi
    pts = []
    for i in range(segments + 1):
        a = start_angle + (end_angle - start_angle) * i / segments
        pts.append(QgsPointXY(
            center.x() + radius * math.cos(a),
            center.y() + radius * math.sin(a),
        ))
    return pts


def _arc_from_3pts(p1: QgsPointXY, p3: QgsPointXY, pm: QgsPointXY):
    """Return (center, radius, start_angle, end_angle) or None."""
    from .circle_tool import _circle_from_3pts
    ctr, r = _circle_from_3pts(p1, pm, p3)
    if ctr is None:
        return None
    sa = math.atan2(p1.y() - ctr.y(), p1.x() - ctr.x())
    ea = math.atan2(p3.y() - ctr.y(), p3.x() - ctr.x())
    return ctr, r, sa, ea


class ArcTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._mode = "3pt"   # "3pt" | "center_angle"
        self._pts: list[QgsPointXY] = []
        self._preview_rb = None

    def activate(self):
        super().activate()
        self._pts.clear()
        self._transition(ToolState.ACTING)
        self._request_input("xy", "Arc start point:")

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point:
                self._pts.append(sem.point)
                n = len(self._pts)
                if self._mode == "3pt":
                    if n == 1:
                        self._update_prompt("Arc end point:")
                    elif n == 2:
                        self._update_prompt("Point on arc:")
                elif self._mode == "center_angle":
                    if n == 1:
                        self._update_prompt("Arc start point (on arc):")
                    elif n == 2:
                        self._update_prompt("Arc end point (on arc):")
                self._try_commit()

        elif sem.type == EventType.KEY_CHAR and sem.char == 'C':
            self._mode = "center_angle"
            self._pts.clear()
            self._request_input("xy", "Arc centre:")

        elif sem.type == EventType.CONFIRM:
            self._reset()

    def _on_hover(self, sem: SemanticEvent):
        if sem.point and len(self._pts) >= 1:
            self._update_preview(sem.point)

    def _try_commit(self):
        if self._mode == "3pt" and len(self._pts) == 3:
            p1, p3, pm = self._pts[0], self._pts[1], self._pts[2]
            result = _arc_from_3pts(p1, p3, pm)
            if result:
                ctr, r, sa, ea = result
                pts = _arc_points(ctr, r, sa, ea)
                self._write_arc(pts)
            self._go_home()

        elif self._mode == "center_angle" and len(self._pts) == 3:
            center = self._pts[0]
            r = center.distance(self._pts[1])
            sa = math.atan2(self._pts[1].y() - center.y(),
                            self._pts[1].x() - center.x())
            ea = math.atan2(self._pts[2].y() - center.y(),
                            self._pts[2].x() - center.x())
            pts = _arc_points(center, r, sa, ea)
            self._write_arc(pts)
            self._go_home()

    def _update_preview(self, cursor_pt: QgsPointXY):
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_DRAW, _style.RB_WIDTH
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        if len(self._pts) == 2 and self._mode == "3pt":
            result = _arc_from_3pts(self._pts[0], self._pts[1], cursor_pt)
            if result:
                ctr, r, sa, ea = result
                pts = _arc_points(ctr, r, sa, ea)
                for i, p in enumerate(pts):
                    self._preview_rb.addPoint(p, i == len(pts)-1)

    def _write_arc(self, pts: list):
        if len(pts) < 2:
            return
        geom = QgsGeometry.fromPolylineXY(pts)
        self._ctx.storage_manager.add_line(geom, self._ctx.active_cad_layer)

    def _reset(self):
        self._pts.clear()
        self._clear_rubber_bands()
        self._preview_rb = None

    def _on_cancel_hook(self):
        self._reset()
