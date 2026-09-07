# -*- coding: utf-8 -*-
"""
PolylineTool — collects N points into a single MultiLineString entity.

Lifecycle: ACTING loops collecting points.  Enter/right-click commits.
C = close (add closing segment back to start).  U = undo last vertex.
Sub-modes (A=arc) are toggled mid-ACTING via keypress.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import QGraphicsTextItem
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes, QgsFeature

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style

_ARC_PX      = 48    # angle-arc radius in screen pixels
_REF_PX      = 60    # horizontal reference line length in screen pixels
_LBL_PAD_PX  = 16    # extra gap past arc for angle-label

_DIST_CLR  = _style.RB_DIST_LABEL
_ANGLE_CLR = _style.RB_ANGLE_ARC


class PolylineTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._points: list[QgsPointXY] = []
        self._committed_rb = None  # solid — all already-clicked segments
        self._preview_rb   = None  # dashed — last point → cursor only
        self._arc_mode     = False
        # dimensional-indicator graphics (created lazily, cleared on reset)
        self._arc_rb    = None   # QgsRubberBand  — angle arc
        self._ref_rb    = None   # QgsRubberBand  — horizontal reference line
        self._dist_item = None   # QGraphicsTextItem — distance label
        self._angle_item = None  # QGraphicsTextItem — angle label

    def activate(self):
        super().activate()
        self._points.clear()
        self._arc_mode = False
        self._transition(ToolState.ACTING)
        self._request_input("xy", "Specify start point:")

    # ── events ────────────────────────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):  # noqa: C901
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point:
                self._add_point(sem.point)

        elif sem.type == EventType.CONFIRM:
            if len(self._points) >= 2:
                self._commit()
            self._go_home()

        elif sem.type == EventType.KEY_CHAR:
            ch = sem.char
            if ch == 'C' and len(self._points) >= 2:
                self._close_and_commit()
            elif ch == 'A':
                self._arc_mode = not self._arc_mode
            elif ch == 'U':
                self._undo_last_vertex()

        elif sem.type == EventType.VALUE_ENTERED:
            pt = self._try_extension_distance(sem.value)
            if pt:
                self._add_point(pt)

    def _on_hover(self, sem: SemanticEvent):
        if self._points and sem.point:
            self._update_preview(sem.point)
        self._update_extension_guide(sem.snap_type, sem.point)

    def _on_undo_step(self):
        self._undo_last_vertex()

    # ── logic ─────────────────────────────────────────────────────────────
    def _add_point(self, pt: QgsPointXY):
        self._points.append(pt)
        self._last_input_ref = pt
        for c in self._ctx.constraints:
            c.set_reference(pt)
        if self._ctx.snap_engine:
            for prov in self._ctx.snap_engine._providers.values():
                if hasattr(prov, 'set_sketch_points'):
                    prov.set_sketch_points(self._points)
        if len(self._points) == 1:
            self._request_input("polar", "Specify next point:")
        elif len(self._points) > 1:
            self._request_input("polar", "Specify next point [U=undo C=close]:")
        self._update_committed()

    def _undo_last_vertex(self):
        if self._points:
            self._points.pop()
        self._update_committed()
        self._update_preview(None)

    def _update_committed(self):
        """Solid rubber band showing all already-clicked segments."""
        if self._committed_rb is None:
            self._committed_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_DRAW, _style.RB_WIDTH + 1
            )
            self._committed_rb.setLineStyle(Qt.PenStyle.SolidLine)
        self._committed_rb.reset(QgsWkbTypes.LineGeometry)
        if len(self._points) >= 2:
            for i, p in enumerate(self._points):
                self._committed_rb.addPoint(p, i == len(self._points) - 1)

    def _update_preview(self, cursor_pt: QgsPointXY | None):
        """Dashed rubber band: last committed point → current cursor only."""
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_DRAW, _style.RB_WIDTH
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        if cursor_pt and self._points:
            self._preview_rb.addPoint(self._points[-1], False)
            self._preview_rb.addPoint(cursor_pt, True)

        if cursor_pt and len(self._points) >= 1:
            self._draw_dim_indicators(self._points[-1], cursor_pt)
        else:
            self._hide_dim_indicators()

    # ── dimensional indicators ────────────────────────────────────────────
    def _draw_dim_indicators(self, ref_pt: QgsPointXY, cur_pt: QgsPointXY):
        canvas = self.canvas()
        mup    = canvas.mapUnitsPerPixel()

        dx   = cur_pt.x() - ref_pt.x()
        dy   = cur_pt.y() - ref_pt.y()
        dist = math.hypot(dx, dy)
        if dist < 1e-10:
            self._hide_dim_indicators()
            return

        # Convert to bearing: 0=North, clockwise, 0-360°
        math_angle_deg = math.degrees(math.atan2(dy, dx))
        bearing        = (90.0 - math_angle_deg) % 360.0
        bearing_rad    = math.radians(bearing)

        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn is not None:
            dyn.set_live_polar(dist, bearing)

        # ── distance label — at midpoint, offset perpendicular to segment ─
        math_angle_rad = math.radians(math_angle_deg)
        mid_pt   = QgsPointXY((ref_pt.x() + cur_pt.x()) / 2,
                               (ref_pt.y() + cur_pt.y()) / 2)
        perp     = math_angle_rad + math.pi / 2
        off      = 14 * mup
        dist_pos = QgsPointXY(mid_pt.x() + off * math.cos(perp),
                              mid_pt.y() + off * math.sin(perp))
        self._set_label('_dist_item', f"{dist:.3f}", dist_pos, _DIST_CLR)

        # ── North reference line (12-o'clock) at ref_pt ───────────────────
        ref_len = _REF_PX * mup
        if self._ref_rb is None:
            self._ref_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _ANGLE_CLR, _style.RB_WIDTH
            )
        self._ref_rb.reset(QgsWkbTypes.LineGeometry)
        self._ref_rb.addPoint(ref_pt, False)
        self._ref_rb.addPoint(QgsPointXY(ref_pt.x(), ref_pt.y() + ref_len), True)

        # ── arc from North clockwise to bearing ───────────────────────────
        arc_r   = _ARC_PX * mup
        n_steps = max(4, int(bearing / 4))

        if self._arc_rb is None:
            self._arc_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _ANGLE_CLR, _style.RB_WIDTH
            )
        self._arc_rb.reset(QgsWkbTypes.LineGeometry)
        for i in range(n_steps + 1):
            b = (i / n_steps) * bearing_rad
            p = QgsPointXY(ref_pt.x() + arc_r * math.sin(b),
                           ref_pt.y() + arc_r * math.cos(b))
            self._arc_rb.addPoint(p, i == n_steps)

        # ── bearing label — near mid-arc ───────────────────────────────────
        mid_b     = bearing_rad / 2
        lbl_r     = (_ARC_PX + _LBL_PAD_PX) * mup
        angle_pos = QgsPointXY(ref_pt.x() + lbl_r * math.sin(mid_b),
                               ref_pt.y() + lbl_r * math.cos(mid_b))
        self._set_label('_angle_item', f"{bearing:.1f}°", angle_pos, _ANGLE_CLR)

    def _set_label(self, attr: str, text: str, map_pt: QgsPointXY, color: QColor):
        canvas = self.canvas()
        item   = getattr(self, attr)
        if item is None:
            item = QGraphicsTextItem()
            item.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            item.setZValue(100)
            canvas.scene().addItem(item)
            setattr(self, attr, item)
        item.setDefaultTextColor(color)
        item.setPlainText(text)
        sp = canvas.mapSettings().mapToPixel().transform(map_pt)
        item.setPos(sp.x(), sp.y())
        item.show()

    def _hide_dim_indicators(self):
        for attr in ('_dist_item', '_angle_item'):
            item = getattr(self, attr)
            if item:
                item.hide()
        for rb in (self._arc_rb, self._ref_rb):
            if rb:
                rb.reset(QgsWkbTypes.LineGeometry)

    def _clear_dim_indicators(self):
        scene = self.canvas().scene()
        for attr in ('_dist_item', '_angle_item'):
            item = getattr(self, attr)
            if item:
                try:
                    scene.removeItem(item)
                except Exception:  # nosec B110
                    pass
                setattr(self, attr, None)
        # rubber bands are removed by _clear_rubber_bands(); just clear refs
        self._arc_rb = None
        self._ref_rb = None

    # ── commit ────────────────────────────────────────────────────────────
    def _commit(self):
        if len(self._points) < 2:
            return
        geom = QgsGeometry.fromPolylineXY(self._points)
        self._write_feature(geom)

    def _close_and_commit(self):
        pts = list(self._points) + [self._points[0]]
        geom = QgsGeometry.fromPolylineXY(pts)
        self._write_feature(geom)
        self._go_home()

    def _write_feature(self, geom: QgsGeometry):
        self._ctx.storage_manager.add_line(geom, self._ctx.active_cad_layer)

    def _reset(self):
        self._points.clear()
        self._last_input_ref = None
        self._arc_mode = False
        self._clear_dim_indicators()
        self._clear_rubber_bands()
        self._committed_rb = None
        self._preview_rb   = None
        self._ext_guide_rb = None
        if self._ctx.snap_engine:
            for prov in self._ctx.snap_engine._providers.values():
                if hasattr(prov, 'clear'):
                    prov.clear()
        self._request_input("xy", "Specify start point:")

    def _on_cancel_hook(self):
        self._reset()
