"""
AutoCAD-style EXPLODE tool.

Click a feature to explode it:
  • Multipart geometry  → split into individual single-part features (all types)
  • Single polyline     → split into individual 2-vertex line segments
  • Single point/polygon → not explodable; a message is shown

Attributes are copied to every resulting feature.
Repeat for more features.  Enter / RMB / Esc → exit.
"""

from qgis.gui import QgsMapTool, QgsRubberBand
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


_C_HOVER = QColor(255, 200, 0, 180)

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


class ExplodeTool(QgsMapTool):
    """EXPLODE — break multipart features or polylines into individual parts."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None

        self._hover_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self._hover_band.setColor(_C_HOVER)
        self._hover_band.setWidth(3)
        self._hover_band.setVisible(False)

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
                    best   = (lyr, feat)
        return best

    def _rm(self, item):
        if item is not None:
            try:
                self.canvas.scene().removeItem(item)
            except Exception:
                pass

    def _show_hint(self, screen_pos):
        self._hint.setText("Click feature to explode  (Enter / RMB = exit)")
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self.canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self.canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    # ------------------------------------------------------------------

    def _apply_explode(self, map_pt):
        result = self._find_feature_near(map_pt)
        if result is None:
            self._log("\nNo feature found near click")
            return

        lyr, feat = result
        geom = feat.geometry()

        if geom.isEmpty():
            self._log("\nEmpty geometry — nothing to explode")
            return

        gt = QgsWkbTypes.geometryType(geom.wkbType())

        if not lyr.isEditable():
            lyr.startEditing()

        if geom.isMultipart():
            if gt == QgsWkbTypes.PointGeometry:
                parts = [QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y()))
                         for p in geom.asMultiPoint()]
            elif gt == QgsWkbTypes.LineGeometry:
                parts = [QgsGeometry.fromPolylineXY(p) for p in geom.asMultiPolyline()]
            elif gt == QgsWkbTypes.PolygonGeometry:
                parts = [QgsGeometry.fromPolygonXY(p) for p in geom.asMultiPolygon()]
            else:
                self._log("\nUnknown geometry type — cannot explode")
                return

            if len(parts) < 2:
                self._log("\nOnly one part found — nothing to explode")
                return

            lyr.changeGeometry(feat.id(), parts[0])
            for p in parts[1:]:
                nf = QgsFeature(lyr.fields())
                nf.setAttributes(feat.attributes())
                nf.setGeometry(p)
                lyr.addFeature(nf)

            lyr.triggerRepaint()
            self._log(
                f"\nExploded multipart '{lyr.name()}' fid {feat.id()}"
                f"  into {len(parts)} single-part features"
            )

        elif gt == QgsWkbTypes.LineGeometry:
            pts = geom.asPolyline()
            if len(pts) < 2:
                self._log("\nLine has fewer than 2 vertices")
                return
            if len(pts) == 2:
                self._log("\nLine already has exactly 2 vertices — nothing to explode")
                return

            segments = [
                QgsGeometry.fromPolylineXY([pts[i], pts[i + 1]])
                for i in range(len(pts) - 1)
            ]

            lyr.changeGeometry(feat.id(), segments[0])
            for seg in segments[1:]:
                nf = QgsFeature(lyr.fields())
                nf.setAttributes(feat.attributes())
                nf.setGeometry(seg)
                lyr.addFeature(nf)

            lyr.triggerRepaint()
            self._log(
                f"\nExploded polyline '{lyr.name()}' fid {feat.id()}"
                f"  into {len(segments)} segments"
            )

        else:
            type_name = QgsWkbTypes.displayString(geom.wkbType())
            self._log(f"\n{type_name} — not explodable (only multipart or polyline features)")

    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._log(
            "\nEXPLODE  ──  click a feature to break it apart"
            "\n  Multipart → single parts  |  Polyline → individual segments"
            "\n  Enter / RMB / Esc → exit\n"
        )

    def deactivate(self):
        self._rm(self._hover_band)
        self._hint.hide()
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        result = self._find_feature_near(map_pt)
        if result:
            lyr, feat = result
            gt = QgsWkbTypes.geometryType(feat.geometry().wkbType())
            self._hover_band.reset(gt)
            self._hover_band.setToGeometry(feat.geometry(), lyr)
            self._hover_band.setVisible(True)
        else:
            self._hover_band.setVisible(False)
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            return
        if event.button() != Qt.LeftButton:
            return
        self._apply_explode(self.toMapCoordinates(event.pos()))
        self.canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.deactivate()
