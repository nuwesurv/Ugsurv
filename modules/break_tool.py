"""
AutoCAD-style BREAK tool.

Workflow
────────
1. Hover over a line  → yellow highlight; red dot shows exact break point
2. Click              → line is split into two features at the nearest point on the line
   Both halves are kept — nothing is deleted.
   Repeat for more breaks on other lines.
   Enter / RMB / Esc  → exit.
"""

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from . import snap_utils
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor
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


_C_HOVER   = QColor(255, 200,  0, 180)
_C_BREAK   = QColor(220,   0,  0, 255)

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


class BreakTool(QgsMapTool):
    """BREAK — split a line into two at a clicked point without removing anything."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None
        self._snap_marker  = None

        self._hover_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self._hover_band.setColor(_C_HOVER)
        self._hover_band.setWidth(3)
        self._hover_band.setVisible(False)

        self._pt_band = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self._pt_band.setColor(_C_BREAK)
        self._pt_band.setIconSize(8)
        self._pt_band.setVisible(False)

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

    # ------------------------------------------------------------------

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

    def _rm(self, item):
        if item is not None:
            try:
                self.canvas.scene().removeItem(item)
            except Exception:
                pass

    def _show_hint(self, screen_pos):
        self._hint.setText("Click line to break  (Enter / RMB = exit)")
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self.canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self.canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    def _snap(self, screen_pos):
        map_pt = self.toMapCoordinates(screen_pos)
        pt, icon = snap_utils.snap_point(self.canvas, map_pt)
        if icon is not None and self._snap_marker:
            self._snap_marker.setCenter(pt)
            self._snap_marker.setIconType(icon)
            self._snap_marker.setVisible(True)
        elif self._snap_marker:
            self._snap_marker.setVisible(False)
        return pt

    # ------------------------------------------------------------------
    # Geometry helpers

    def _sub_line(self, geom, d_from, d_to):
        if d_to - d_from < 1e-10:
            return None
        pts = []
        s = geom.interpolate(d_from)
        if not s.isEmpty():
            p = s.asPoint()
            pts.append(QgsPointXY(p.x(), p.y()))
        verts = geom.asPolyline()
        cum = 0.0
        for i, v in enumerate(verts):
            if i > 0:
                cum += verts[i-1].distance(v)
            if d_from < cum < d_to:
                pts.append(v)
        e = geom.interpolate(d_to)
        if not e.isEmpty():
            p = e.asPoint()
            pts.append(QgsPointXY(p.x(), p.y()))
        if len(pts) >= 2:
            return QgsGeometry.fromPolylineXY(pts)
        return None

    def _apply_break(self, screen_pos):
        snap_pt = self._snap(screen_pos)
        map_pt  = self.toMapCoordinates(screen_pos)

        lyr, feat = self._find_line_near(map_pt)
        if feat is None:
            self._log("\nNo line found near click")
            return

        geom = feat.geometry()
        if geom.isEmpty() or geom.isMultipart():
            self._log("\nMultipart geometry — break not supported (use single-part lines)")
            return

        # Project snap point onto the line to get the exact break distance
        pt_geom = QgsGeometry.fromPointXY(snap_pt)
        break_d = geom.lineLocatePoint(pt_geom)
        total   = geom.length()

        margin = 1e-6 * total
        if break_d < margin or break_d > total - margin:
            self._log("\nBreak point too close to an endpoint — nothing to split")
            return

        seg_a = self._sub_line(geom, 0.0, break_d)
        seg_b = self._sub_line(geom, break_d, total)

        if seg_a is None or seg_b is None:
            self._log("\nCould not compute sub-lines")
            return

        if not lyr.isEditable():
            lyr.startEditing()

        lyr.changeGeometry(feat.id(), seg_a)

        new_feat = QgsFeature(lyr.fields())
        new_feat.setAttributes(feat.attributes())
        new_feat.setGeometry(seg_b)
        lyr.addFeature(new_feat)

        lyr.triggerRepaint()
        brk_pt = geom.interpolate(break_d).asPoint()
        self._log(
            f"\nBroke '{lyr.name()}' fid {feat.id()}"
            f"  at ({brk_pt.x():.3f}, {brk_pt.y():.3f})"
        )

    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        snap_utils.init_snap()
        self._snap_marker = QgsVertexMarker(self.canvas)
        self._snap_marker.setColor(QColor(66, 135, 245))
        self._snap_marker.setIconSize(10)
        self._snap_marker.setPenWidth(2)
        self._snap_marker.setVisible(False)
        self._log(
            "\nBREAK  ──  click any line to split it at that point"
            "\n  Both halves are kept  |  Enter / RMB / Esc → exit\n"
        )

    def deactivate(self):
        self._rm(self._hover_band)
        self._rm(self._pt_band)
        if self._snap_marker:
            self.canvas.scene().removeItem(self._snap_marker)
            self._snap_marker = None
        self._hint.hide()
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        snap_pt = self._snap(event.pos())
        lyr, feat = self._find_line_near(map_pt)
        if feat and not feat.geometry().isMultipart():
            self._hover_band.setToGeometry(feat.geometry(), lyr)
            self._hover_band.setVisible(True)
            d = feat.geometry().lineLocatePoint(QgsGeometry.fromPointXY(snap_pt))
            near = feat.geometry().interpolate(d)
            if not near.isEmpty():
                self._pt_band.setToGeometry(near, lyr)
                self._pt_band.setVisible(True)
            else:
                self._pt_band.setVisible(False)
        else:
            self._hover_band.setVisible(False)
            self._pt_band.setVisible(False)
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            return
        if event.button() != Qt.LeftButton:
            return
        self._apply_break(event.pos())
        self.canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.deactivate()
