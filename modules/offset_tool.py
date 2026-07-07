"""
AutoCAD-style OFFSET tool.

Workflow
────────
1. Hover over a line → highlights yellow.  Click → selects it.
2. Move cursor away  → dashed orange preview appears on the cursor's side;
   dynamic input shows the live perpendicular distance.
   Click              → create offset at that distance.
   Type distance in dynamic input + Enter → create offset at typed distance
                                            on the cursor's current side.
   RMB                → deselect / go back to step 1.
   Escape             → exit.
   Repeat step 2 to keep creating parallel copies of the same line.
"""

import math

from .layer_utils import polyline_attrs
from .dynamic_input import DynamicInput
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor


_C_PREVIEW  = QColor(255, 140,  0, 200)
_C_HOVER    = QColor(255, 200,  0, 160)
_C_SELECTED = QColor( 50, 210,  80, 220)

_HIT_PX = 10

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

_ST_SELECT    = 0   # hover / click a line to select it
_ST_PICK_DIST = 1   # line selected; cursor distance sets offset; click applies

_HINT = {
    _ST_SELECT:    "Click a line to select  (Esc = exit)",
    _ST_PICK_DIST: "Click to offset  |  type distance + Enter  (RMB = reselect)",
}


class OffsetTool(QgsMapTool):
    """AutoCAD-style OFFSET — select a line, then click or type the distance."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None

        self._state       = _ST_SELECT
        self._sel_layer   = None
        self._sel_feat    = None
        self._sel_geom    = None   # QgsGeometry copy of selected feature
        self._last_map_pt = None   # last cursor map position (for DI commit side)

        self._hover_band   = self._make_band(_C_HOVER,    width=2)
        self._hover_band.setVisible(False)
        self._sel_band     = self._make_band(_C_SELECTED, width=3)
        self._sel_band.setVisible(False)
        self._preview_band = self._make_band(_C_PREVIEW,  width=2, dashed=True)
        self._preview_band.setVisible(False)

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        self._dinput = DynamicInput(canvas, terminal_dock, [
            {"key": "dist", "label": "Offset distance"},
        ])
        self._dinput.on_commit = self._on_dinput_commit
        self._dinput.on_cancel = self._on_dinput_cancel

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    def _make_band(self, color, width=2, dashed=False):
        band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        band.setColor(color)
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

    def _line_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.isSpatial()
            and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry
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
        """Perpendicular (shortest) distance from map_pt to selected feature."""
        if self._sel_geom is None:
            return 0.0
        return self._sel_geom.distance(QgsGeometry.fromPointXY(map_pt))

    # ------------------------------------------------------------------
    # Offset geometry maths
    # ------------------------------------------------------------------

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

    def _offset_polyline(self, pts, dist):
        n = len(pts)
        if n < 2:
            return None
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
        """Compute signed offset toward the cursor's side of the feature."""
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

    # ------------------------------------------------------------------
    # Preview update
    # ------------------------------------------------------------------

    def _update_preview(self, map_pt):
        if self._state == _ST_SELECT:
            lyr, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._hover_band.setToGeometry(feat.geometry(), lyr)
                self._hover_band.setVisible(True)
            else:
                self._hover_band.setVisible(False)
            self._preview_band.setVisible(False)

        elif self._state == _ST_PICK_DIST and self._sel_geom is not None:
            self._hover_band.setVisible(False)
            dist = self._dist_to_sel(map_pt)
            if dist > 1e-10:
                off_geom = self._build_offset_geom(self._sel_geom, dist, map_pt)
                if off_geom:
                    self._preview_band.setToGeometry(off_geom, self._sel_layer)
                    self._preview_band.setVisible(True)
                    return
            self._preview_band.setVisible(False)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _select_feature(self, lyr, feat):
        self._sel_layer = lyr
        self._sel_feat  = feat
        self._sel_geom  = QgsGeometry(feat.geometry())
        self._sel_band.setToGeometry(self._sel_geom, lyr)
        self._sel_band.setVisible(True)
        self._hover_band.setVisible(False)
        self._state = _ST_PICK_DIST
        self._log(
            f"\nSelected '{lyr.name()}'"
            "\n  Move cursor to set distance  |  click to apply"
            "\n  Or type a distance in the input box + Enter"
            "\n  RMB → reselect  |  Esc → exit\n"
        )
        centroid = self._sel_geom.centroid().asPoint()
        cp = self.canvas.getCoordinateTransform().transform(centroid)
        self._dinput.show(cp.x(), cp.y())
        self.terminal_dock.request_input("Distance: ", self._on_terminal_dist)

    def _deselect(self):
        self._sel_layer = None
        self._sel_feat  = None
        self._sel_geom  = None
        self._sel_band.setVisible(False)
        self._preview_band.setVisible(False)
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._state = _ST_SELECT
        self._log("\nClick a line to select for offsetting\n")

    # ------------------------------------------------------------------
    # Distance input callbacks
    # ------------------------------------------------------------------

    def _on_dinput_commit(self, values: dict):
        """Called when user presses Enter/Space inside the dynamic input box."""
        self.terminal_dock.clear_input_handler()
        text = values.get("dist", "").strip()
        if not text:
            return
        try:
            dist = abs(float(text))
        except ValueError:
            self._log(f"\nInvalid distance '{text}' — enter a positive number")
            self._rewire()
            return
        if dist < 1e-10:
            self._log("\nDistance must be greater than zero")
            self._rewire()
            return
        map_pt = self._last_map_pt
        if map_pt is None or self._sel_geom is None:
            return
        self._apply_offset(dist, map_pt)

    def _on_dinput_cancel(self):
        """Called when user presses Escape inside the dynamic input box."""
        self.terminal_dock.clear_input_handler()
        self._deselect()

    def _on_terminal_dist(self, text: str):
        """Fallback: user typed distance in terminal command line and pressed Enter."""
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        text = text.strip()
        if not text:
            self._deselect()
            return
        try:
            dist = abs(float(text))
        except ValueError:
            self._log(f"\nInvalid distance '{text}' — enter a positive number")
            self._rewire()
            return
        if dist < 1e-10:
            self._log("\nDistance must be greater than zero")
            self._rewire()
            return
        map_pt = self._last_map_pt
        if map_pt is None or self._sel_geom is None:
            self._deselect()
            return
        self._apply_offset(dist, map_pt)

    def _rewire(self):
        """Re-show DI and re-register terminal handler after a bad input."""
        if self._last_map_pt:
            cp = self.canvas.getCoordinateTransform().transform(self._last_map_pt)
            self._dinput.show(cp.x(), cp.y())
        self.terminal_dock.request_input("Distance: ", self._on_terminal_dist)

    # ------------------------------------------------------------------
    # Apply offset
    # ------------------------------------------------------------------

    def _apply_offset(self, dist, map_pt):
        if self._sel_layer is None or self._sel_geom is None:
            return
        off_geom = self._build_offset_geom(self._sel_geom, dist, map_pt)
        if off_geom is None:
            self._log("\nCould not compute offset — multipart lines not supported")
            self._rewire()
            return

        lyr      = self._sel_layer
        src_feat = self._sel_feat
        new_feat = QgsFeature(lyr.fields())
        new_feat.setAttributes(src_feat.attributes())
        new_feat.setGeometry(off_geom)
        for fname, val in polyline_attrs(off_geom).items():
            idx = lyr.fields().indexOf(fname)
            if idx >= 0:
                new_feat.setAttribute(idx, val)

        if not lyr.isEditable():
            lyr.startEditing()
        lyr.addFeature(new_feat)
        lyr.triggerRepaint()
        self._log(f"\nOffset  {dist:.4f}  →  '{lyr.name()}'")

        # Stay on the same feature — re-wire for the next offset
        self._rewire()

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._log(
            "\nOFFSET  ──  click a line to select it"
            "\n  Move cursor to set distance, click to apply"
            "\n  Or type the exact distance in the input box + Enter"
            "\n  RMB → reselect  |  Esc → exit\n"
        )

    def deactivate(self):
        self._dinput.destroy()
        self.terminal_dock.clear_input_handler()
        self._rm(self._preview_band)
        self._rm(self._hover_band)
        self._rm(self._sel_band)
        self._hint.hide()
        self._state = _ST_SELECT
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        self._last_map_pt = map_pt
        self._update_preview(map_pt)
        self._show_hint(event.pos())

        if self._state == _ST_PICK_DIST and self._sel_geom is not None:
            dist = self._dist_to_sel(map_pt)
            cp   = self.canvas.getCoordinateTransform().transform(map_pt)
            self._dinput.update(cp.x(), cp.y(), {"dist": f"{dist:.4f}"})

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            if self._state == _ST_PICK_DIST:
                self._deselect()
            else:
                self.deactivate()
            return
        if event.button() != Qt.LeftButton:
            return

        map_pt = self.toMapCoordinates(event.pos())
        self._last_map_pt = map_pt

        if self._state == _ST_SELECT:
            lyr, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._select_feature(lyr, feat)
            else:
                self._log("\nNo line found near click")

        elif self._state == _ST_PICK_DIST and self._sel_geom is not None:
            dist = self._dist_to_sel(map_pt)
            if dist < 1e-10:
                self._log("\nClick further from the line to set a non-zero offset")
                return
            self._dinput.hide()
            self.terminal_dock.clear_input_handler()
            self._apply_offset(dist, map_pt)

        self.canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            if self._state == _ST_PICK_DIST:
                self._deselect()
            else:
                self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self._state == _ST_SELECT:
                self.deactivate()
