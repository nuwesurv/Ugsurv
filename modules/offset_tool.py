"""
AutoCAD-style OFFSET tool.

Workflow
────────
1. Type offset distance in the terminal prompt.
2. Hover over a line  → dashed orange preview appears on the cursor's side.
3. Click              → new parallel line created in the same layer.
   Repeat clicks to keep offsetting at the same distance.
   Enter / RMB        → exit.
   Escape             → exit.

The cursor side determines the offset direction automatically — no need to
type +/– signs.  Distance is always treated as a positive magnitude.
"""

import math

from .layer_utils import polyline_attrs
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


_C_PREVIEW = QColor(255, 140,  0, 200)
_C_HOVER   = QColor(255, 200,  0, 160)

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

_ST_DIST   = 0
_ST_SELECT = 1

_HINT = {
    _ST_DIST:   "Type offset distance in terminal",
    _ST_SELECT: "Click line to offset  (Enter / RMB = exit)",
}


class OffsetTool(QgsMapTool):
    """AutoCAD-style OFFSET — parallel copy of a line at a fixed distance."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None

        self._state = _ST_DIST
        self._dist  = 0.0

        self._preview_band = self._make_band(_C_PREVIEW, width=2, dashed=True)
        self._preview_band.setVisible(False)
        self._hover_band   = self._make_band(_C_HOVER,   width=2)
        self._hover_band.setVisible(False)

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

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

    # ------------------------------------------------------------------
    # Offset geometry maths
    # ------------------------------------------------------------------

    @staticmethod
    def _offset_seg(p1, p2, dist):
        """Offset segment p1→p2 perpendicularly by dist (left = positive)."""
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
        """Intersection of infinite lines a1–a2 and b1–b2 (miter join)."""
        d1x = a2.x()-a1.x();  d1y = a2.y()-a1.y()
        d2x = b2.x()-b1.x();  d2y = b2.y()-b1.y()
        denom = d1x*d2y - d1y*d2x
        if abs(denom) < 1e-10:
            return QgsPointXY((a2.x()+b1.x())/2, (a2.y()+b1.y())/2)
        t = ((b1.x()-a1.x())*d2y - (b1.y()-a1.y())*d2x) / denom
        return QgsPointXY(a1.x()+t*d1x, a1.y()+t*d1y)

    def _offset_polyline(self, pts, dist):
        """Compute offset points for a QgsPointXY list using miter joins."""
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
        """Return +1 if map_pt is to the left of the nearest segment, else -1."""
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

    def _build_offset_geom(self, feat_geom, map_pt):
        """Compute signed offset geometry toward the cursor side."""
        if feat_geom.isMultipart():
            return None
        pts = feat_geom.asPolyline()
        if not pts:
            return None
        side    = self._cursor_side(pts, map_pt)
        off_pts = self._offset_polyline(pts, self._dist * side)
        if off_pts is None or len(off_pts) < 2:
            return None
        return QgsGeometry.fromPolylineXY(off_pts)

    # ------------------------------------------------------------------
    # Hover preview
    # ------------------------------------------------------------------

    def _update_preview(self, map_pt):
        if self._state != _ST_SELECT:
            self._preview_band.setVisible(False)
            self._hover_band.setVisible(False)
            return

        lyr, feat = self._find_line_near(map_pt)
        if feat is None:
            self._hover_band.setVisible(False)
            self._preview_band.setVisible(False)
            return

        self._hover_band.setToGeometry(feat.geometry(), lyr)
        self._hover_band.setVisible(True)

        off_geom = self._build_offset_geom(feat.geometry(), map_pt)
        if off_geom:
            self._preview_band.setToGeometry(off_geom, lyr)
            self._preview_band.setVisible(True)
        else:
            self._preview_band.setVisible(False)

    # ------------------------------------------------------------------
    # Distance input
    # ------------------------------------------------------------------

    def _request_distance(self):
        self._state = _ST_DIST
        self.terminal_dock.request_input("Offset distance: ", self._on_distance_entered)

    def _on_distance_entered(self, text: str):
        text = text.strip()
        if not text:
            self.deactivate()
            return
        try:
            dist = abs(float(text))
        except ValueError:
            self._log(f"\nInvalid distance '{text}' — enter a positive number")
            self.terminal_dock.request_input("Offset distance: ", self._on_distance_entered)
            return
        if dist < 1e-10:
            self._log("\nDistance must be greater than zero")
            self.terminal_dock.request_input("Offset distance: ", self._on_distance_entered)
            return
        self._dist  = dist
        self._state = _ST_SELECT
        self._log(
            f"\nOffset distance = {dist:.4f}"
            "\n  Hover over a line and click to create a parallel copy"
            "\n  Enter / RMB → exit\n"
        )

    # ------------------------------------------------------------------
    # Apply offset
    # ------------------------------------------------------------------

    def _apply_offset(self, map_pt):
        lyr, feat = self._find_line_near(map_pt)
        if feat is None:
            self._log("\nNo line found near click")
            return

        off_geom = self._build_offset_geom(feat.geometry(), map_pt)
        if off_geom is None:
            self._log("\nCould not compute offset — multipart lines not supported")
            return

        src_feat = lyr.getFeature(feat.id())
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
        self._log(f"\nOffset  {self._dist:.4f}  →  '{lyr.name()}'")

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._log(
            "\nOFFSET  ──  enter distance in terminal, then click lines to offset"
            "\n  Cursor side sets direction  |  Enter / RMB → exit\n"
        )
        self._request_distance()

    def deactivate(self):
        self.terminal_dock.clear_input_handler()
        self._rm(self._preview_band)
        self._rm(self._hover_band)
        self._hint.hide()
        self._state = _ST_DIST
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        self._update_preview(self.toMapCoordinates(event.pos()))
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            return
        if event.button() != Qt.LeftButton:
            return
        if self._state == _ST_SELECT:
            self._apply_offset(self.toMapCoordinates(event.pos()))
        self.canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self._state == _ST_SELECT:
                self.deactivate()
