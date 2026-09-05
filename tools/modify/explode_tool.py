# -*- coding: utf-8 -*-
"""
AutoCAD-style EXPLODE tool.

Click a feature to explode it:
  • Multipart geometry  → split into individual single-part features (all types)
  • Single polyline     → split into individual 2-vertex line segments
  • Single point/polygon → not explodable; message shown

Attributes are copied to every resulting feature.
Repeat for more features.  Enter / RMB / Esc → exit.
"""

import contextlib

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType
from ...core import style as _style
from ...core.circle_utils import is_circle

_C_HOVER = _style.RB_HOVER

_HIT_PX = 10

class ExplodeTool(QgsMapTool):
    """EXPLODE — break multipart features or polylines into individual parts."""

    inputModeChanged = pyqtSignal(str, str)
    promptChanged    = pyqtSignal(str)

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._hover_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.LineGeometry)
        self._hover_band.setColor(_C_HOVER)
        self._hover_band.setWidth(_style.RB_WIDTH)
        self._hover_band.setLineStyle(_style.RB_LINE_STYLE)
        self._hover_band.setVisible(False)

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

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

    def _apply_explode(self, map_pt):
        result = self._find_feature_near(map_pt)
        if result is None:
            self._log("  No feature found near click")
            return

        lyr, feat = result
        geom = feat.geometry()
        if geom.isEmpty():
            self._log("  Empty geometry — nothing to explode")
            return
        if is_circle(geom):
            self._log("  Circles cannot be exploded", "#ffaaaa")
            return

        gt = QgsWkbTypes.geometryType(geom.wkbType())
        if not lyr.isEditable():
            lyr.startEditing()

        hist = getattr(self._ctx, 'action_history', None)

        if geom.isMultipart():
            if gt == QgsWkbTypes.GeometryType.PointGeometry:
                parts = [QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y()))
                         for p in geom.asMultiPoint()]
            elif gt == QgsWkbTypes.GeometryType.LineGeometry:
                parts = [QgsGeometry.fromPolylineXY(p) for p in geom.asMultiPolyline()]
            elif gt == QgsWkbTypes.GeometryType.PolygonGeometry:
                parts = [QgsGeometry.fromPolygonXY(p) for p in geom.asMultiPolygon()]
            else:
                self._log("  Unknown geometry type — cannot explode")
                return

            if len(parts) < 2:
                self._log("  Only one part found — nothing to explode")
                return

            if hist:
                hist.begin_group()
            lyr.changeGeometry(feat.id(), parts[0])
            if hist:
                hist.record_step(lyr.id())
            for p in parts[1:]:
                nf = QgsFeature(lyr.fields())
                nf.setAttributes(feat.attributes())
                nf.setGeometry(p)
                lyr.addFeature(nf)
                if hist:
                    hist.record_step(lyr.id())
            if hist:
                hist.end_group()
            lyr.triggerRepaint()
            self._log(
                f"  Exploded multipart '{lyr.name()}' fid {feat.id()}"
                f"  → {len(parts)} single-part features", "#88ff88"
            )

        elif gt == QgsWkbTypes.GeometryType.LineGeometry:
            pts = geom.asPolyline()
            if len(pts) < 2:
                self._log("  Line has fewer than 2 vertices")
                return
            if len(pts) == 2:
                self._log("  Line already has exactly 2 vertices — nothing to explode")
                return

            segments = [
                QgsGeometry.fromPolylineXY([pts[i], pts[i + 1]])
                for i in range(len(pts) - 1)
            ]
            if hist:
                hist.begin_group()
            lyr.changeGeometry(feat.id(), segments[0])
            if hist:
                hist.record_step(lyr.id())
            for seg in segments[1:]:
                nf = QgsFeature(lyr.fields())
                nf.setAttributes(feat.attributes())
                nf.setGeometry(seg)
                lyr.addFeature(nf)
                if hist:
                    hist.record_step(lyr.id())
            if hist:
                hist.end_group()
            lyr.triggerRepaint()
            self._log(
                f"  Exploded polyline '{lyr.name()}' fid {feat.id()}"
                f"  → {len(segments)} segments", "#88ff88"
            )

        else:
            type_name = QgsWkbTypes.displayString(geom.wkbType())
            self._log(f"  {type_name} — not explodable (only multipart or polyline features)")

    def _dispatch(self, sem):
        pass

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        self._log(
            "EXPLODE  ──  click a feature to break it apart"
            "  (multipart → parts | polyline → segments)", "#aaddff"
        )
        self._last_input_mode   = "no_value"
        self._last_input_prompt = "Click a feature to explode:"
        self.inputModeChanged.emit("no_value", "Click a feature to explode:")

    def deactivate(self):
        self._hover_band.setVisible(False)
        self.inputModeChanged.emit("", "")
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

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._go_home()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._apply_explode(self.toMapCoordinates(event.pos()))
        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._go_home()
