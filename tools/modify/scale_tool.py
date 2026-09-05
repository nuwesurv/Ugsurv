# -*- coding: utf-8 -*-
"""
AutoCAD-style SCALE tool.

Workflow
────────
1. Click features to select.  Enter/RMB → confirm.
2. Click scale origin.
3. Click to set factor by cursor distance  or  type factor + Enter.
"""

import contextlib
import math

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import (
    QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType, SnapType
from ...core import style as _style
from ...core.circle_utils import (
    is_circle, circle_params, build_circle_geom, update_circle_attrs,
)

_C_HIGHLIGHT = _style.RB_SOURCE
_C_HL_FILL   = _style.RB_SOURCE_FILL
_C_PREVIEW   = _style.RB_PREVIEW
_C_PREV_FILL = _style.RB_PREVIEW_FILL

_ST_SELECT = 0
_ST_BASE   = 1
_ST_SCALE  = 2

_HIT_PX = 10

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


def _scale_geom(geom, cx, cy, factor):
    """Scale geometry around (cx, cy) by factor. Handles all geometry types."""
    if is_circle(geom):
        center, radius = circle_params(geom)
        new_cx = cx + (center.x() - cx) * factor
        new_cy = cy + (center.y() - cy) * factor
        return build_circle_geom(QgsPointXY(new_cx, new_cy), radius * abs(factor))

    gt = QgsWkbTypes.geometryType(geom.wkbType())

    def _sp(p):
        return QgsPointXY(cx + (p.x() - cx) * factor,
                          cy + (p.y() - cy) * factor)

    if gt == QgsWkbTypes.GeometryType.PointGeometry:
        if geom.isMultipart():
            return QgsGeometry.fromMultiPointXY([_sp(p) for p in geom.asMultiPoint()])
        p = geom.asPoint()
        return QgsGeometry.fromPointXY(_sp(QgsPointXY(p.x(), p.y())))
    elif gt == QgsWkbTypes.GeometryType.LineGeometry:
        if geom.isMultipart():
            parts = [[_sp(p) for p in part] for part in geom.asMultiPolyline()]
            return QgsGeometry.fromMultiPolylineXY(parts)
        return QgsGeometry.fromPolylineXY([_sp(p) for p in geom.asPolyline()])
    elif gt == QgsWkbTypes.GeometryType.PolygonGeometry:
        if geom.isMultipart():
            parts = [[[_sp(p) for p in ring] for ring in poly]
                     for poly in geom.asMultiPolygon()]
            return QgsGeometry.fromMultiPolygonXY(parts)
        rings = [[_sp(p) for p in ring] for ring in geom.asPolygon()]
        return QgsGeometry.fromPolygonXY(rings)
    return QgsGeometry(geom)


class ScaleTool(QgsMapTool):
    """Scale features — multi-select → origin → factor."""

    inputModeChanged = pyqtSignal(str, str)
    promptChanged    = pyqtSignal(str)

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state        = _ST_SELECT
        self._sel_features = []
        self._sel_bands    = []
        self._prev_bands   = []
        self._base_pt: QgsPointXY | None = None
        self._ref_dist     = 1.0
        self._last_factor  = 1.0
        self._snap_marker  = None

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

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

    def _find_feature_near(self, map_pt):
        tol     = self._hit_tol()
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        rect    = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                               map_pt.x()+tol, map_pt.y()+tol)
        best, best_d = None, tol
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial():
                continue
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                d = geom.distance(pt_geom)
                if d < best_d:
                    best_d = d
                    best = (lyr, feat.id(), QgsGeometry(geom))
        return best

    def _make_band(self, geom_type, color, fill_color, width=_style.RB_WIDTH, dashed=False):
        band = QgsRubberBand(self._canvas, geom_type)
        band.setColor(color)
        band.setFillColor(fill_color)
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

    def _sel_key(self, layer, fid):
        return (id(layer), fid)

    def _existing_keys(self):
        return [self._sel_key(lyr, f) for lyr, f, _ in self._sel_features]

    def _add_to_selection(self, layer, fid, geom):
        if self._sel_key(layer, fid) in self._existing_keys():
            return False
        self._sel_features.append((layer, fid, QgsGeometry(geom)))
        gt = QgsWkbTypes.geometryType(geom.wkbType())
        hl = self._make_band(gt, _C_HIGHLIGHT, _C_HL_FILL, width=2)
        hl.setToGeometry(geom, layer)
        self._sel_bands.append(hl)
        prev = self._make_band(gt, _C_PREVIEW, _C_PREV_FILL, width=2, dashed=True)
        prev.setVisible(False)
        self._prev_bands.append(prev)
        return True

    def _remove_from_selection(self, layer, fid):
        key  = self._sel_key(layer, fid)
        keys = self._existing_keys()
        if key not in keys:
            return False
        idx = keys.index(key)
        self._sel_features.pop(idx)
        self._rm(self._sel_bands.pop(idx))
        self._rm(self._prev_bands.pop(idx))
        return True

    def _clear_selection(self):
        for b in self._sel_bands:
            self._rm(b)
        for b in self._prev_bands:
            self._rm(b)
        self._sel_features = []
        self._sel_bands    = []
        self._prev_bands   = []

    def _enter_base(self):
        for b in self._prev_bands:
            b.setVisible(False)
        self._state = _ST_BASE
        n = len(self._sel_features)
        self._log(f"  {n} feature(s) selected  →  click scale origin", "#88ccff")
        self.inputModeChanged.emit("no_value", "Click scale origin:")

    def _enter_scale(self, base_pt: QgsPointXY):
        self._base_pt = base_pt
        self._state   = _ST_SCALE
        dists = []
        bbox = QgsRectangle()
        for layer, fid, geom in self._sel_features:
            c = geom.centroid().asPoint()
            d = math.hypot(c.x() - base_pt.x(), c.y() - base_pt.y())
            if d > 0:
                dists.append(d)
            bbox.combineExtentWith(geom.boundingBox())
        centroid_dist = sum(dists) / len(dists) if dists else 0.0
        # When origin is clicked near the centroid, centroid_dist ≈ 0 and
        # factor = cursor_dist / ref_dist would fly.  Use the bounding-box
        # half-diagonal as a floor: it is proportional to feature size, so
        # snapping the cursor to a point 2× that distance gives factor = 2.0.
        bbox_half = math.hypot(bbox.width(), bbox.height()) / 2.0
        self._ref_dist = max(centroid_dist, bbox_half) or 1.0
        for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
            band.setToGeometry(geom, layer)
            band.setVisible(True)
        self._log("  Click or type scale factor", "#88ccff")
        self.inputModeChanged.emit("value", "Scale factor:")

    def _update_preview(self, map_pt: QgsPointXY):
        if self._state != _ST_SCALE or not self._base_pt:
            return
        dist = math.hypot(map_pt.x() - self._base_pt.x(),
                          map_pt.y() - self._base_pt.y())
        factor = dist / self._ref_dist if self._ref_dist > 0 else 1.0
        if factor <= 0:
            return
        self._last_factor = factor
        cx, cy = self._base_pt.x(), self._base_pt.y()
        for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
            scaled = _scale_geom(geom, cx, cy, factor)
            band.setToGeometry(scaled, layer)
        self.promptChanged.emit(f"Scale factor <{factor:.3f}>:")
        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn:
            dyn.set_live_value(factor)

    def _apply_scale(self, factor: float):
        if not self._base_pt or factor <= 0:
            return
        cx, cy = self._base_pt.x(), self._base_pt.y()
        modified = set()
        for layer, fid, geom in self._sel_features:
            new_geom = _scale_geom(geom, cx, cy, factor)
            if not layer.isEditable():
                layer.startEditing()
            layer.changeGeometry(fid, new_geom)
            if is_circle(geom):
                new_center, new_radius = circle_params(new_geom)
                update_circle_attrs(layer, fid, new_center, new_radius)
            modified.add(layer)
        for lyr in modified:
            lyr.triggerRepaint()
        n = len(self._sel_features)
        self._log(f"  Scaled {n} feature(s)  ×{factor:.4f}", "#88ff88")
        self._go_home()

    def _reset(self):
        self._clear_selection()
        self._state   = _ST_SELECT
        self._base_pt = None
        self.inputModeChanged.emit("no_value", "Select features to scale:")

    def _dispatch(self, sem):
        if self._state == _ST_SCALE:
            if sem.type == EventType.VALUE_ENTERED and sem.value is not None:
                self._apply_scale(float(sem.value))
            elif sem.type == EventType.COORDINATE_ENTERED and sem.point and self._base_pt:
                dist = math.hypot(sem.point.x() - self._base_pt.x(),
                                  sem.point.y() - self._base_pt.y())
                factor = dist / self._ref_dist if self._ref_dist > 0 else 1.0
                if factor > 0:
                    self._apply_scale(factor)

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        sel = getattr(self._ctx, 'selection_model', None)
        if sel and not sel.is_empty():
            for lid, fid in sel:
                layer = QgsProject.instance().mapLayer(lid)
                if not isinstance(layer, QgsVectorLayer):
                    continue
                feat = layer.getFeature(fid)
                if feat.isValid() and not feat.geometry().isEmpty():
                    self._add_to_selection(layer, fid, feat.geometry())
        if self._sel_features:
            self._log("SCALE", "#aaddff")
            self._last_input_mode   = "no_value"
            self._last_input_prompt = "Click scale origin:"
            self.inputModeChanged.emit("no_value", "Click scale origin:")
            self._enter_base()
        else:
            self._log("SCALE  ──  select features to scale", "#aaddff")
            self._last_input_mode   = "no_value"
            self._last_input_prompt = "Select features to scale:"
            self.inputModeChanged.emit("no_value", "Select features to scale:")

    def deactivate(self):
        self._clear_snap_marker()
        self._clear_selection()
        self.inputModeChanged.emit("", "")
        self._state   = _ST_SELECT
        self._base_pt = None
        super().deactivate()

    def canvasMoveEvent(self, event):
        self._update_preview(self._snapped(self.toMapCoordinates(event.pos())))

    def canvasPressEvent(self, event):
        map_pt = self._snapped(self.toMapCoordinates(event.pos()))
        shift  = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if event.button() == Qt.MouseButton.RightButton:
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._go_home()
            elif self._state == _ST_SCALE:
                self._apply_scale(self._last_factor)
            else:
                self._reset()
                self._log("  Scale cancelled", "#ffaaaa")
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self._state == _ST_SELECT:
            result = self._find_feature_near(map_pt)
            if result:
                layer, fid, geom = result
                if shift:
                    if self._remove_from_selection(layer, fid):
                        self._log(f"  Deselected  ({len(self._sel_features)} selected)")
                else:
                    if self._add_to_selection(layer, fid, geom):
                        self._log(f"  Selected '{layer.name()}' fid {fid}"
                                  f"  ({len(self._sel_features)} selected)")
            else:
                self._log("  No feature found near click")

        elif self._state == _ST_BASE:
            self._enter_scale(map_pt)

        elif self._state == _ST_SCALE:
            dist = math.hypot(map_pt.x() - self._base_pt.x(),
                              map_pt.y() - self._base_pt.y())
            factor = dist / self._ref_dist if self._ref_dist > 0 else 1.0
            if factor > 0:
                self._apply_scale(factor)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("  Scale cancelled", "#ffaaaa")
            else:
                self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._go_home()
            elif self._state == _ST_SCALE:
                self._apply_scale(self._last_factor)
