"""
AutoCAD-style ROTATE tool.

Workflow
────────
1. Click any feature   → highlighted green        (_ST_SELECT → _ST_BASE)
2. Click base point    → rotation centre           (_ST_BASE   → _ST_ANGLE)
3. Move cursor         → live rotated preview; angle shown in DynamicInput
4. Click              → apply rotation at cursor angle
   Type angle + Enter  → apply that angle precisely
   Escape / RMB        → cancel

Angle convention: counter-clockwise from east (0° = east, 90° = north),
matching standard mathematical convention.  The live preview makes the
direction immediately obvious regardless of convention.
"""

import math

from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsGeometry,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from .dynamic_input import DynamicInput


_C_HIGHLIGHT = QColor(0, 200, 80, 220)
_C_HL_FILL   = QColor(0, 200, 80, 20)
_C_PREVIEW   = QColor(255, 130, 0, 220)
_C_PREV_FILL = QColor(255, 130, 0, 30)

_ST_SELECT = 0
_ST_BASE   = 1
_ST_ANGLE  = 2

_HIT_PX = 10

_HINT = {
    _ST_SELECT: "Click a feature to rotate",
    _ST_BASE:   "Click rotation centre",
    _ST_ANGLE:  "Click to set angle  or  type degrees",
}

_HINT_STYLE = (
    "QLabel {"
    "  background-color: rgba(20, 20, 20, 210);"
    "  color: #f0f0f0;"
    "  border: 1px solid rgba(255, 255, 255, 80);"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "  font-size: 9pt;"
    "}"
)


def _rotate_pt(p, cx, cy, cos_a, sin_a):
    dx = p.x() - cx
    dy = p.y() - cy
    return QgsPointXY(cx + dx*cos_a - dy*sin_a,
                      cy + dx*sin_a + dy*cos_a)


def _rotate_geom(geom, cx, cy, angle_deg):
    """Rotate geometry CCW by angle_deg around (cx, cy). Handles all geom types."""
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    gt = QgsWkbTypes.geometryType(geom.wkbType())

    if gt == QgsWkbTypes.PointGeometry:
        if geom.isMultipart():
            pts = [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in geom.asMultiPoint()]
            return QgsGeometry.fromMultiPointXY(pts)
        p = geom.asPoint()
        return QgsGeometry.fromPointXY(
            _rotate_pt(QgsPointXY(p.x(), p.y()), cx, cy, cos_a, sin_a)
        )

    elif gt == QgsWkbTypes.LineGeometry:
        if geom.isMultipart():
            parts = [
                [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in part]
                for part in geom.asMultiPolyline()
            ]
            return QgsGeometry.fromMultiPolylineXY(parts)
        pts = [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in geom.asPolyline()]
        return QgsGeometry.fromPolylineXY(pts)

    elif gt == QgsWkbTypes.PolygonGeometry:
        if geom.isMultipart():
            parts = [
                [[_rotate_pt(p, cx, cy, cos_a, sin_a) for p in ring] for ring in part]
                for part in geom.asMultiPolygon()
            ]
            return QgsGeometry.fromMultiPolygonXY(parts)
        rings = [
            [_rotate_pt(p, cx, cy, cos_a, sin_a) for p in ring]
            for ring in geom.asPolygon()
        ]
        return QgsGeometry.fromPolygonXY(rings)

    return QgsGeometry(geom)


class RotateTool(QgsMapTool):
    """Rotate entire features — AutoCAD-style select → centre → angle."""

    def __init__(self, canvas, terminal_dock, preselect=None):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None
        self._preselect    = preselect

        self._state        = _ST_SELECT
        self._sel_features = []   # list of (layer, fid, geom_copy)
        self._sel_bands    = []   # highlight band per feature
        self._prev_bands   = []   # preview band per feature
        self._base_pt      = None
        self._snap_pt      = None
        self._snap_ind     = None

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        self._dinput = DynamicInput(canvas, terminal_dock, [
            {"key": "angle", "label": "Angle (°)"},
        ])
        self._dinput.on_cancel = self._cancel_dinput

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_band(self, geom_type, color, fill_color, width=2, dashed=False):
        band = QgsRubberBand(self.canvas, geom_type)
        band.setColor(color)
        band.setFillColor(fill_color)
        band.setWidth(width)
        if dashed:
            band.setLineStyle(Qt.DashLine)
        return band

    def _rm(self, item):
        if item is not None:
            try:
                self.canvas.scene().removeItem(item)
            except Exception:
                pass

    def _log(self, msg):
        self.terminal_dock.commandOutputText += msg
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    def _hit_tol(self):
        return _HIT_PX * self.canvas.mapUnitsPerPixel()

    def _vector_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial()
        ]

    def _find_feature_near(self, map_pt):
        tol     = self._hit_tol()
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        rect    = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                               map_pt.x()+tol, map_pt.y()+tol)
        best, best_d = None, tol
        for lyr in self._vector_layers():
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                d = geom.distance(pt_geom)
                if d < best_d:
                    best_d = d
                    best = (lyr, feat.id(), QgsGeometry(geom))
        return best

    def _snap(self, screen_pos):
        match = self.canvas.snappingUtils().snapToMap(screen_pos)
        if match.isValid():
            if self._snap_ind:
                self._snap_ind.setMatch(match)
            return match.point()
        if self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        return self.toMapCoordinates(screen_pos)

    def _show_hint(self, screen_pos):
        text = _HINT.get(self._state, "")
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self.canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self.canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    def _clear_bands(self):
        for b in self._sel_bands:
            self._rm(b)
        for b in self._prev_bands:
            self._rm(b)
        self._sel_bands  = []
        self._prev_bands = []

    def _reset(self):
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._clear_bands()
        if self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        self._state        = _ST_SELECT
        self._sel_features = []
        self._base_pt      = None
        self._snap_pt      = None

    # ------------------------------------------------------------------
    # DynamicInput callbacks
    # ------------------------------------------------------------------

    def _on_angle_committed(self, values: dict):
        self.terminal_dock.clear_input_handler()
        self._apply_angle_text(values["angle"])

    def _on_angle_terminal(self, text: str):
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._apply_angle_text(text.strip())

    def _apply_angle_text(self, text: str):
        if not text:
            self._reset()
            self._log("\nRotate cancelled")
            return
        try:
            angle = float(text)
        except ValueError:
            self._log(f"\nInvalid angle '{text}' — enter degrees (e.g. 45 or -90)")
            return
        self._apply_rotate(angle)

    def _cancel_dinput(self):
        self._reset()
        self._log("\nRotate cancelled")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _enter_base(self, layer, fid, geom):
        """Called when a single feature is clicked in _ST_SELECT."""
        self._clear_bands()
        self._sel_features = [(layer, fid, QgsGeometry(geom))]
        gt = QgsWkbTypes.geometryType(geom.wkbType())
        hl = self._make_band(gt, _C_HIGHLIGHT, _C_HL_FILL, width=2)
        hl.setToGeometry(geom, layer)
        self._sel_bands.append(hl)
        prev = self._make_band(gt, _C_PREVIEW, _C_PREV_FILL, width=2, dashed=True)
        prev.setVisible(False)
        self._prev_bands.append(prev)
        self._state = _ST_BASE
        self._log(
            f"\nSelected '{layer.name()}' fid {fid}"
            f"  →  click rotation centre  |  Esc / RMB to cancel"
        )

    def _load_preselect(self, items):
        """Load a list of (layer, fid) tuples as the selection, enter _ST_BASE."""
        self._clear_bands()
        self._sel_features = []
        for layer, fid in items:
            feat = layer.getFeature(fid)
            if not feat.isValid() or feat.geometry().isEmpty():
                continue
            geom = QgsGeometry(feat.geometry())
            self._sel_features.append((layer, fid, geom))
            gt = QgsWkbTypes.geometryType(geom.wkbType())
            hl = self._make_band(gt, _C_HIGHLIGHT, _C_HL_FILL, width=2)
            hl.setToGeometry(geom, layer)
            self._sel_bands.append(hl)
            prev = self._make_band(gt, _C_PREVIEW, _C_PREV_FILL, width=2, dashed=True)
            prev.setVisible(False)
            self._prev_bands.append(prev)
        if self._sel_features:
            self._state = _ST_BASE
            n = len(self._sel_features)
            self._log(
                f"\n{n} feature(s) selected"
                f"  →  click rotation centre  |  Esc / RMB to cancel"
            )

    def _enter_angle(self, base_pt):
        self._base_pt = base_pt
        self._state   = _ST_ANGLE
        for b in self._prev_bands:
            b.setVisible(True)
        self._log("\nClick to set angle  or  type degrees + Enter  |  Esc / RMB to cancel")
        cp = self.canvas.getCoordinateTransform().transform(base_pt)
        self._dinput.on_commit = self._on_angle_committed
        self.terminal_dock.request_input("Angle°: ", self._on_angle_terminal)
        self._dinput.show(cp.x(), cp.y())

    def _cursor_angle(self, snap_pt):
        if not self._base_pt:
            return 0.0
        dx = snap_pt.x() - self._base_pt.x()
        dy = snap_pt.y() - self._base_pt.y()
        return math.degrees(math.atan2(dy, dx))

    def _update_preview(self, snap_pt):
        if self._state == _ST_ANGLE and self._base_pt:
            angle  = self._cursor_angle(snap_pt)
            cx, cy = self._base_pt.x(), self._base_pt.y()
            for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
                rotated = _rotate_geom(geom, cx, cy, angle)
                band.setToGeometry(rotated, layer)

    def _commit(self, screen_pos):
        snap_pt = self._snap(screen_pos)
        angle   = self._cursor_angle(snap_pt)
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._apply_rotate(angle)

    def _apply_rotate(self, angle_deg: float):
        cx, cy   = self._base_pt.x(), self._base_pt.y()
        modified = set()
        for layer, fid, geom in self._sel_features:
            new_geom = _rotate_geom(geom, cx, cy, angle_deg)
            if not layer.isEditable():
                layer.startEditing()
            layer.changeGeometry(fid, new_geom)
            modified.add(layer)
        for lyr in modified:
            lyr.triggerRepaint()
        n = len(self._sel_features)
        self._log(f"\nRotated {n} feature(s)  {angle_deg:.2f}°  around ({cx:.3f}, {cy:.3f})")
        self.deactivate()

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()
        self._snap_ind = QgsSnapIndicator(self.canvas)

        if self._preselect:
            items = self._preselect
            self._preselect = None
            if isinstance(items, list):
                self._load_preselect(items)
            else:
                layer, fid = items
                feat = layer.getFeature(fid)
                if feat.isValid() and not feat.geometry().isEmpty():
                    self._enter_base(layer, fid, QgsGeometry(feat.geometry()))
            if self._sel_features:
                return

        self._log(
            "\nROTATE  ──  click a feature, then rotation centre, then set angle"
            "\n  Click to rotate visually  |  type angle (°) + Enter for precision"
            "\n  Esc / RMB → cancel\n"
        )

    def deactivate(self):
        self._dinput.destroy()
        self.terminal_dock.clear_input_handler()
        self._clear_bands()
        self._sel_features = []
        self._snap_ind = None
        self._hint.hide()
        self._state = _ST_SELECT
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        if self._state in (_ST_BASE, _ST_ANGLE):
            snap_pt = self._snap(event.pos())
            self._snap_pt = snap_pt
            self._update_preview(snap_pt)
            if self._state == _ST_ANGLE and self._base_pt:
                angle = self._cursor_angle(snap_pt)
                cp    = self.canvas.getCoordinateTransform().transform(snap_pt)
                self._dinput.update(cp.x(), cp.y(), {"angle": f"{angle:.2f}"})
        elif self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())

        if event.button() == Qt.RightButton:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("\nRotate cancelled")
            else:
                self._hint.hide()
                self.deactivate()
            return

        if event.button() != Qt.LeftButton:
            return

        if self._state == _ST_SELECT:
            result = self._find_feature_near(map_pt)
            if result:
                self._enter_base(*result)
            else:
                self._log("\nNo feature found near click")

        elif self._state == _ST_BASE:
            snap_pt = self._snap(event.pos())
            self._enter_angle(snap_pt)

        elif self._state == _ST_ANGLE:
            self._commit(event.pos())

        self.terminal_dock.command.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("\nRotate cancelled")
            else:
                self._hint.hide()
                self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._hint.hide()
            self.deactivate()

    def mouseDoubleClickEvent(self, event):
        self.terminal_dock.command.setFocus()
