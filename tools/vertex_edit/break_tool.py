# -*- coding: utf-8 -*-
"""
AutoCAD-style BREAK tool.

Workflow
────────
1. Hover over a line  → yellow highlight; red dot shows exact break point
2. Click              → line is split into two features at the nearest point
   Both halves are kept — nothing is deleted.
   Repeat for more breaks on other lines.
   Enter / RMB / Esc  → exit.
"""

import contextlib

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType
from ...core import style as _style

_C_HOVER  = _style.RB_HOVER
_C_BREAK  = _style.RB_DESTROY

_HIT_PX = 10

_HINT_STYLE = (
    "QLabel{background:rgba(20,20,20,210);color:#f0f0f0;"
    "border:1px solid rgba(255,255,255,80);border-radius:4px;"
    "padding:3px 8px;font-size:9pt;}"
)


class BreakTool(QgsMapTool):
    """BREAK — split a line into two at a clicked point without removing anything."""

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

        self._pt_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PointGeometry)
        self._pt_band.setColor(_C_BREAK)
        self._pt_band.setIconSize(8)
        self._pt_band.setVisible(False)

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

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

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    def _show_hint(self, screen_pos):
        self._hint.setText("Click line to break")
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self._canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self._canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

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

    def _apply_break(self, map_pt):
        lyr, feat = self._find_line_near(map_pt)
        if feat is None:
            self._log("  No line found near click")
            return

        geom = feat.geometry()
        if geom.isEmpty() or geom.isMultipart():
            self._log("  Multipart geometry — break not supported (use single-part lines)")
            return

        pt_geom = QgsGeometry.fromPointXY(map_pt)
        break_d = geom.lineLocatePoint(pt_geom)
        total   = geom.length()

        margin = 1e-6 * total
        if break_d < margin or break_d > total - margin:
            self._log("  Break point too close to an endpoint — nothing to split")
            return

        seg_a = self._sub_line(geom, 0.0, break_d)
        seg_b = self._sub_line(geom, break_d, total)

        if seg_a is None or seg_b is None:
            self._log("  Could not compute sub-lines")
            return

        if not lyr.isEditable():
            lyr.startEditing()

        lyr.changeGeometry(feat.id(), seg_a)

        new_feat = QgsFeature(lyr.fields())
        new_feat.setAttributes(feat.attributes())
        new_feat.setGeometry(seg_b)
        lyr.addFeature(new_feat)

        lyr.triggerRepaint()
        brk = geom.interpolate(break_d).asPoint()
        self._log(
            f"  Broke '{lyr.name()}' fid {feat.id()}"
            f"  at ({brk.x():.3f}, {brk.y():.3f})",
            "#88ff88"
        )

    def _dispatch(self, sem):
        pass

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        self._log("BREAK  ──  click any line to split it  (both halves kept)", "#aaddff")

    def deactivate(self):
        self._rm(self._hover_band)
        self._rm(self._pt_band)
        self._hint.hide()
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        lyr, feat = self._find_line_near(map_pt)
        if feat and not feat.geometry().isMultipart():
            self._hover_band.setToGeometry(feat.geometry(), lyr)
            self._hover_band.setVisible(True)
            d    = feat.geometry().lineLocatePoint(QgsGeometry.fromPointXY(map_pt))
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
        if event.button() == Qt.MouseButton.RightButton:
            map_pt = self.toMapCoordinates(event.pos())
            lyr, feat = self._find_line_near(map_pt)
            if feat and not feat.geometry().isMultipart():
                self._apply_break(map_pt)
            else:
                self._go_home()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._apply_break(self.toMapCoordinates(event.pos()))
        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._go_home()
