# -*- coding: utf-8 -*-
"""
AutoCAD-style OFFSET tool.

Workflow
────────
1. Hover → yellow highlight.  Click → selects the line.
2. Move cursor → dashed orange preview on cursor's side; shows live distance.
   Click              → create offset at that distance.
   Type distance + Enter → create offset at typed distance on cursor's current side.
   RMB                → deselect / back to step 1.
   Esc                → exit.
   Repeat step 2 for more parallel copies of the same line.
"""

import contextlib
import math

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType, SnapType
from ...core import style as _style
from ..layer_utils import polyline_attrs
from ...core.circle_utils import (
    is_circle, circle_params, build_circle_geom, set_circle_attrs_on_feature,
)

_C_PREVIEW  = _style.RB_PREVIEW
_C_HOVER    = _style.RB_HOVER
_C_SELECTED = _style.RB_SOURCE
_C_PERP     = _style.RB_OFFSET_GUIDE

_HIT_PX = 10

_ST_SELECT    = 0
_ST_PICK_DIST = 1

_SNAP_ICONS = {
    SnapType.VERTEX:        (_style.SNAP_ICON['endpoint'],     _style._CC_COLOR),
    SnapType.POINT:         (_style.SNAP_ICON['point'],        _style._CC_COLOR),
    SnapType.MIDPOINT:      (_style.SNAP_ICON['midpoint'],     _style._CC_COLOR),
    SnapType.CENTER:        (_style.SNAP_ICON['center'],       _style._CC_COLOR),
    SnapType.INTERSECTION:  (_style.SNAP_ICON['intersection'], _style._CC_COLOR),
    SnapType.PERPENDICULAR: (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.EXTENSION:     (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.NEAREST:       (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.SELF:          (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.GRID:          (QgsVertexMarker.ICON_CROSS,       _style.SNAP_GRID_COLOR),
}


class OffsetTool(QgsMapTool):
    """AutoCAD-style OFFSET — select a line then click or type the distance."""

    inputModeChanged = pyqtSignal(str, str)
    promptChanged    = pyqtSignal(str)

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state       = _ST_SELECT
        self._sel_layer   = None
        self._sel_fid     = None
        self._sel_geom    = None
        self._last_map_pt = None
        self._snap_marker = None

        self._hover_band   = self._make_band(_C_HOVER,    width=_style.RB_WIDTH)
        self._hover_band.setVisible(False)
        self._sel_band     = self._make_band(_C_SELECTED, width=_style.RB_WIDTH)
        self._sel_band.setVisible(False)
        self._preview_band = self._make_band(_C_PREVIEW,  width=_style.RB_WIDTH, dashed=True)
        self._preview_band.setVisible(False)
        self._perp_band    = self._make_band(_C_PERP,     width=_style.RB_WIDTH, dashed=True)
        self._perp_band.setVisible(False)

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _make_band(self, color, width=_style.RB_WIDTH, dashed=False):
        band = QgsRubberBand(self._canvas, QgsWkbTypes.GeometryType.LineGeometry)
        band.setColor(color)
        band.setWidth(width)
        band.setLineStyle(_style.RB_LINE_STYLE)
        return band

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

    def _snapped(self, raw_pt):
        engine = getattr(self._ctx, 'snap_engine', None)
        if engine:
            result = engine.resolve(raw_pt, self._canvas)
            if result:
                self._show_snap_marker(result.point, result.snap_type)
                return result.point
        self._hide_snap_marker()
        return raw_pt

    def _show_snap_marker(self, pt, snap_type):
        icon, color = _SNAP_ICONS.get(snap_type, (QgsVertexMarker.ICON_BOX, _style._CC_COLOR))
        if self._snap_marker is None:
            self._snap_marker = QgsVertexMarker(self._canvas)
            self._snap_marker.setIconSize(_style.SNAP_ICON_SIZE)
            self._snap_marker.setPenWidth(_style.SNAP_PEN_WIDTH)
        self._snap_marker.setIconType(icon)
        self._snap_marker.setColor(color)
        self._snap_marker.setCenter(pt)
        self._snap_marker.show()

    def _hide_snap_marker(self):
        if self._snap_marker:
            self._snap_marker.hide()

    def _clear_snap_marker(self):
        if self._snap_marker is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(self._snap_marker)
            self._snap_marker = None

    def _line_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.isSpatial()
            and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.GeometryType.LineGeometry
        ]

    def _find_line_near(self, map_pt):
        tol  = self._hit_tol()
        rect = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                            map_pt.x()+tol, map_pt.y()+tol)
        cg   = QgsGeometry.fromPointXY(map_pt)
        best_layer, best_feat, best_d = None, None, float('inf')
        for lyr in self._line_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                d = feat.geometry().distance(cg)
                if d < best_d:
                    best_d, best_layer, best_feat = d, lyr, feat
        return (best_layer, best_feat) if best_d <= tol else (None, None)

    def _dist_to_sel(self, map_pt):
        if self._sel_geom is None:
            return 0.0
        return self._sel_geom.distance(QgsGeometry.fromPointXY(map_pt))

    # --- Offset geometry maths ---

    @staticmethod
    def _offset_seg(p1, p2, dist):
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        length = math.sqrt(dx*dx + dy*dy)
        if length < 1e-10:
            return p1, p2
        nx = -dy / length * dist
        ny =  dx / length * dist
        return (QgsPointXY(p1.x()+nx, p1.y()+ny),
                QgsPointXY(p2.x()+nx, p2.y()+ny))

    @staticmethod
    def _line_isect(a1, a2, b1, b2):
        d1x = a2.x()-a1.x();  d1y = a2.y()-a1.y()
        d2x = b2.x()-b1.x();  d2y = b2.y()-b1.y()
        denom = d1x*d2y - d1y*d2x
        if abs(denom) < 1e-10:
            return QgsPointXY((a2.x()+b1.x())/2, (a2.y()+b1.y())/2)
        t = ((b1.x()-a1.x())*d2y - (b1.y()-a1.y())*d2x) / denom
        return QgsPointXY(a1.x()+t*d1x, a1.y()+t*d1y)

    @staticmethod
    def _is_closed_ring(pts):
        return (len(pts) >= 4
                and abs(pts[0].x() - pts[-1].x()) < 1e-9
                and abs(pts[0].y() - pts[-1].y()) < 1e-9)

    def _offset_polyline(self, pts, dist):
        n = len(pts)
        if n < 2:
            return None
        if self._is_closed_ring(pts):
            ring = pts[:-1]
            m = len(ring)
            if m < 3:
                return None
            segs = [self._offset_seg(ring[i], ring[(i + 1) % m], dist)
                    for i in range(m)]
            result = [
                self._line_isect(segs[i][0], segs[i][1],
                                 segs[(i + 1) % m][0], segs[(i + 1) % m][1])
                for i in range(m)
            ]
            result.append(result[0])
            return result
        segs = [self._offset_seg(pts[i], pts[i+1], dist) for i in range(n-1)]
        result = [segs[0][0]]
        for i in range(len(segs)-1):
            pt = self._line_isect(segs[i][0], segs[i][1], segs[i+1][0], segs[i+1][1])
            result.append(pt)
        result.append(segs[-1][1])
        return result

    def _cursor_side(self, pts, map_pt):
        if len(pts) < 2:
            return 1
        cg     = QgsGeometry.fromPointXY(map_pt)
        min_d  = float('inf')
        best_i = 0
        for i in range(len(pts)-1):
            seg = QgsGeometry.fromPolylineXY([pts[i], pts[i+1]])
            d = seg.distance(cg)
            if d < min_d:
                min_d, best_i = d, i
        a = pts[best_i]
        b = pts[best_i+1]
        cross = (b.x()-a.x())*(map_pt.y()-a.y()) - (b.y()-a.y())*(map_pt.x()-a.x())
        return 1 if cross >= 0 else -1

    def _build_offset_geom(self, geom, dist, map_pt):
        if is_circle(geom):
            center, radius = circle_params(geom)
            cursor_dist = math.hypot(map_pt.x() - center.x(), map_pt.y() - center.y())
            new_r = radius + dist if cursor_dist >= radius else radius - dist
            if new_r <= 0:
                return None
            return build_circle_geom(center, new_r)

        if geom.isMultipart():
            return None
        pts = geom.asPolyline()
        if not pts:
            return None
        side    = self._cursor_side(pts, map_pt)
        off_pts = self._offset_polyline(pts, dist * side)
        if off_pts is None or len(off_pts) < 2:
            return None
        return QgsGeometry.fromPolylineXY(off_pts)

    # --- Preview ---

    def _update_preview(self, map_pt):
        if self._state == _ST_SELECT:
            lyr, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._hover_band.setToGeometry(feat.geometry(), lyr)
                self._hover_band.setVisible(True)
            else:
                self._hover_band.setVisible(False)
            self._preview_band.setVisible(False)
            self._perp_band.setVisible(False)
        elif self._state == _ST_PICK_DIST and self._sel_geom is not None:
            self._hover_band.setVisible(False)
            dist = self._dist_to_sel(map_pt)
            if dist > 1e-10:
                closest = self._sel_geom.nearestPoint(QgsGeometry.fromPointXY(map_pt))
                if not closest.isEmpty():
                    perp_line = QgsGeometry.fromPolylineXY(
                        [QgsPointXY(closest.asPoint().x(), closest.asPoint().y()), map_pt]
                    )
                    self._perp_band.setToGeometry(perp_line, None)
                    self._perp_band.setVisible(True)
                else:
                    self._perp_band.setVisible(False)
                off_geom = self._build_offset_geom(self._sel_geom, dist, map_pt)
                if off_geom:
                    self._preview_band.setToGeometry(off_geom, self._sel_layer)
                    self._preview_band.setVisible(True)
                    self.promptChanged.emit(f"Offset distance <{dist:.3f}m>:")
                    dyn = getattr(self._ctx, 'dyn_widget', None)
                    if dyn:
                        dyn.set_live_value(dist)
                    return
            self._preview_band.setVisible(False)
            self._perp_band.setVisible(False)

    # --- Selection ---

    def _select_feature(self, lyr, feat):
        self._sel_layer = lyr
        self._sel_fid   = feat.id()
        self._sel_geom  = QgsGeometry(feat.geometry())
        self._sel_band.setToGeometry(self._sel_geom, lyr)
        self._sel_band.setVisible(True)
        self._hover_band.setVisible(False)
        self._state = _ST_PICK_DIST
        self._log(f"  Selected '{lyr.name()}'  —  move cursor to set distance, or click",
                  "#88ccff")
        self.promptChanged.emit("Offset distance:")

    def _deselect(self):
        self._sel_layer = None
        self._sel_fid   = None
        self._sel_geom  = None
        self._sel_band.setVisible(False)
        self._preview_band.setVisible(False)
        self._perp_band.setVisible(False)
        self._state = _ST_SELECT
        self._log("  Click a line to select for offsetting")
        self.promptChanged.emit("Click a line to offset:")

    # --- Apply ---

    def _apply_offset(self, dist, map_pt):  # noqa: C901
        if self._sel_layer is None or self._sel_geom is None:
            return
        off_geom = self._build_offset_geom(self._sel_geom, dist, map_pt)
        if off_geom is None:
            self._log("  Could not compute offset — multipart lines not supported", "#ffaaaa")
            return

        lyr = self._sel_layer
        nf  = QgsFeature(lyr.fields())
        nf.setGeometry(off_geom)
        if is_circle(self._sel_geom):
            src_feat = lyr.getFeature(self._sel_fid)
            if src_feat.isValid():
                nf.setAttributes(src_feat.attributes())
            new_center, new_radius = circle_params(off_geom)
            set_circle_attrs_on_feature(nf, new_center, new_radius)
        else:
            for fname, val in polyline_attrs(off_geom).items():
                idx = lyr.fields().indexOf(fname)
                if idx >= 0:
                    nf.setAttribute(idx, val)
        if not lyr.isEditable():
            lyr.startEditing()
        hist = getattr(self._ctx, 'action_history', None)
        if hist:
            hist.begin_group()
        lyr.addFeature(nf)
        if hist:
            hist.record_step(lyr.id())
        if hist:
            hist.end_group()
        lyr.triggerRepaint()
        self._log(f"  Offset {dist:.4f}  →  '{lyr.name()}'", "#88ff88")

    def _dispatch(self, sem):
        if self._state == _ST_PICK_DIST and self._sel_geom is not None:
            map_pt = self._last_map_pt
            if map_pt is None:
                return
            if sem.type == EventType.VALUE_ENTERED and sem.value is not None:
                dist = abs(float(sem.value))
                if dist < 1e-10:
                    self._log("  Distance must be greater than zero", "#ffaaaa")
                    return
                self._apply_offset(dist, map_pt)
            elif sem.type == EventType.COORDINATE_ENTERED and sem.point:
                dist = abs(float(sem.point.x()))
                if dist < 1e-10:
                    self._log("  Distance must be greater than zero", "#ffaaaa")
                    return
                self._apply_offset(dist, map_pt)

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        sel = getattr(self._ctx, 'selection_model', None)
        if sel and not sel.is_empty():
            for lid, fid in sel:
                layer = QgsProject.instance().mapLayer(lid)
                if not isinstance(layer, QgsVectorLayer):
                    continue
                if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.GeometryType.LineGeometry:
                    continue
                feat = layer.getFeature(fid)
                if feat.isValid() and not feat.geometry().isEmpty():
                    self._select_feature(layer, feat)
                    break
        if self._state != _ST_SELECT:
            self._log("OFFSET", "#aaddff")
            self._last_input_mode   = "value"
            self._last_input_prompt = "Offset distance:"
            self.inputModeChanged.emit("value", "Offset distance:")
        else:
            self._log("OFFSET  ──  click a line, then move cursor or type distance",
                      "#aaddff")
            self._last_input_mode   = "no_value"
            self._last_input_prompt = "Click a line to offset:"
            self.inputModeChanged.emit("no_value", "Click a line to offset:")

    def deactivate(self):
        self._clear_snap_marker()
        self._preview_band.setVisible(False)
        self._hover_band.setVisible(False)
        self._sel_band.setVisible(False)
        self._perp_band.setVisible(False)
        self.inputModeChanged.emit("", "")
        self._state     = _ST_SELECT
        self._sel_layer = None
        self._sel_geom  = None
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self._snapped(self.toMapCoordinates(event.pos()))
        self._last_map_pt = map_pt
        self._update_preview(map_pt)

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            if self._state == _ST_PICK_DIST:
                self._deselect()
            else:
                self._go_home()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        map_pt = self._snapped(self.toMapCoordinates(event.pos()))
        self._last_map_pt = map_pt

        if self._state == _ST_SELECT:
            lyr, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._select_feature(lyr, feat)
            else:
                self._log("  No line found near click")
        elif self._state == _ST_PICK_DIST and self._sel_geom is not None:
            dist = self._dist_to_sel(map_pt)
            if dist < 1e-10:
                self._log("  Click further from the line to set a non-zero offset")
                return
            self._apply_offset(dist, map_pt)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._state == _ST_PICK_DIST:
                self._deselect()
            else:
                self._go_home()
