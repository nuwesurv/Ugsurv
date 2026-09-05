# -*- coding: utf-8 -*-
"""
AutoCAD-style ROTATE tool.

Workflow
────────
1. Click features to select.  Enter/RMB → confirm.
2. Click rotation centre.
3. Click to set angle from cursor  or  type degrees + Enter.

Angle: CCW from east (0°=east, 90°=north).
"""

import contextlib
import math

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import (
    QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType
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
_ST_ANGLE  = 2

_HIT_PX = 10


def _rotate_pt(p, cx, cy, cos_a, sin_a):
    dx = p.x() - cx
    dy = p.y() - cy
    return QgsPointXY(cx + dx*cos_a - dy*sin_a,
                      cy + dx*sin_a + dy*cos_a)


def _rotate_geom(geom, cx, cy, angle_deg):
    """Rotate geometry CCW by angle_deg around (cx, cy). Handles all geom types."""
    if is_circle(geom):
        # Rotating a circle = moving its center; radius stays the same.
        center, radius = circle_params(geom)
        a = math.radians(angle_deg)
        cos_a, sin_a = math.cos(a), math.sin(a)
        dx, dy = center.x() - cx, center.y() - cy
        new_cx = cx + dx * cos_a - dy * sin_a
        new_cy = cy + dx * sin_a + dy * cos_a
        return build_circle_geom(QgsPointXY(new_cx, new_cy), radius)

    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    gt = QgsWkbTypes.geometryType(geom.wkbType())

    if gt == QgsWkbTypes.GeometryType.PointGeometry:
        if geom.isMultipart():
            pts = [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in geom.asMultiPoint()]
            return QgsGeometry.fromMultiPointXY(pts)
        p = geom.asPoint()
        return QgsGeometry.fromPointXY(
            _rotate_pt(QgsPointXY(p.x(), p.y()), cx, cy, cos_a, sin_a)
        )
    elif gt == QgsWkbTypes.GeometryType.LineGeometry:
        if geom.isMultipart():
            parts = [
                [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in part]
                for part in geom.asMultiPolyline()
            ]
            return QgsGeometry.fromMultiPolylineXY(parts)
        pts = [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in geom.asPolyline()]
        return QgsGeometry.fromPolylineXY(pts)
    elif gt == QgsWkbTypes.GeometryType.PolygonGeometry:
        if geom.isMultipart():
            parts = [
                [[_rotate_pt(p, cx, cy, cos_a, sin_a) for p in ring]
                 for ring in poly]
                for poly in geom.asMultiPolygon()
            ]
            return QgsGeometry.fromMultiPolygonXY(parts)
        rings = [
            [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in ring]
            for ring in geom.asPolygon()
        ]
        return QgsGeometry.fromPolygonXY(rings)
    return QgsGeometry(geom)


class RotateTool(QgsMapTool):
    """Rotate features — multi-select → rotation centre → angle."""

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
        self._last_angle   = 0.0

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

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
        self._log(f"  {n} feature(s) selected  →  click rotation centre", "#88ccff")
        self.promptChanged.emit("Click rotation centre:")

    def _enter_angle(self, base_pt: QgsPointXY):
        self._base_pt = base_pt
        self._state   = _ST_ANGLE
        for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
            band.setToGeometry(geom, layer)
            band.setVisible(True)
        self._log("  Click or type angle in degrees (CCW from east)", "#88ccff")
        self.promptChanged.emit("Click or type angle in degrees:")

    def _update_preview(self, map_pt: QgsPointXY):
        if self._state != _ST_ANGLE or not self._base_pt:
            return
        angle_deg = math.degrees(
            math.atan2(map_pt.y() - self._base_pt.y(),
                       map_pt.x() - self._base_pt.x())
        )
        cx, cy = self._base_pt.x(), self._base_pt.y()
        for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
            rotated = _rotate_geom(geom, cx, cy, angle_deg)
            band.setToGeometry(rotated, layer)
        self._last_angle = angle_deg
        self.promptChanged.emit(f"Angle <{angle_deg:.2f}°>:")
        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn:
            dyn.set_live_value(angle_deg)

    def _apply_rotation(self, angle_deg: float):
        if not self._base_pt:
            return
        cx, cy = self._base_pt.x(), self._base_pt.y()
        modified = set()
        for layer, fid, geom in self._sel_features:
            new_geom = _rotate_geom(geom, cx, cy, angle_deg)
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
        self._log(f"  Rotated {n} feature(s)  {angle_deg:.2f}°", "#88ff88")
        self._go_home()

    def _reset(self):
        self._clear_selection()
        self._state   = _ST_SELECT
        self._base_pt = None

    def _dispatch(self, sem):
        if self._state == _ST_ANGLE:
            if sem.type == EventType.VALUE_ENTERED and sem.value is not None:
                self._apply_rotation(float(sem.value))
            elif sem.type == EventType.COORDINATE_ENTERED and sem.point and self._base_pt:
                angle_deg = math.degrees(
                    math.atan2(sem.point.y() - self._base_pt.y(),
                               sem.point.x() - self._base_pt.x())
                )
                self._apply_rotation(angle_deg)

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
            self._log("ROTATE", "#aaddff")
            self._last_input_mode   = "value"
            self._last_input_prompt = "Click rotation centre:"
            self.inputModeChanged.emit("value", "Click rotation centre:")
            self._enter_base()
        else:
            self._log("ROTATE  ──  select features to rotate", "#aaddff")
            self._last_input_mode   = "value"
            self._last_input_prompt = "Select features to rotate:"
            self.inputModeChanged.emit("value", "Select features to rotate:")

    def deactivate(self):
        self._clear_selection()
        self.inputModeChanged.emit("", "")
        self._state   = _ST_SELECT
        self._base_pt = None
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        self._update_preview(map_pt)

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        shift  = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if event.button() == Qt.MouseButton.RightButton:
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._go_home()
            elif self._state == _ST_ANGLE:
                self._apply_rotation(self._last_angle)
            else:
                self._reset()
                self._log("  Rotate cancelled", "#ffaaaa")
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
            self._enter_angle(map_pt)

        elif self._state == _ST_ANGLE:
            angle_deg = math.degrees(
                math.atan2(map_pt.y() - self._base_pt.y(),
                           map_pt.x() - self._base_pt.x())
            )
            self._apply_rotation(angle_deg)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("  Rotate cancelled", "#ffaaaa")
            else:
                self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._go_home()
            elif self._state == _ST_ANGLE:
                self._apply_rotation(self._last_angle)
