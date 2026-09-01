# -*- coding: utf-8 -*-
"""
AutoCAD-style EXTEND tool.

Two-phase workflow:
  Phase 1 – Select boundary edges (cyan).  Enter/RMB → advance.
  Phase 2 – Click line ends to mark for extension (blue).
             Enter/RMB → apply all.  Esc → cancel.
"""

import contextlib
import math

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType
from ...core import style as _style

_C_EDGE     = _style.RB_EDGE
_C_PREVIEW  = _style.RB_PREVIEW
_C_SELECTED = _style.RB_EXTEND
_C_HOVER    = _style.RB_HOVER

_ST_SELECT = 0
_ST_EXTEND = 1

_HIT_PX = 10
_FAR    = 1e8

_HINT_STYLE = (
    "QLabel{background:rgba(20,20,20,210);color:#f0f0f0;"
    "border:1px solid rgba(255,255,255,80);border-radius:4px;"
    "padding:3px 8px;font-size:9pt;}"
)

_HINT = {
    _ST_SELECT: "Click boundary edges",
    _ST_EXTEND: "Click line end to mark for extension",
}


class ExtendTool(QgsMapTool):
    """AutoCAD-style EXTEND tool — multi-select extensions then confirm all at once."""

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state           = _ST_SELECT
        self._boundary_edges  = []
        self._boundary_bands  = []
        self._modified_layers = set()

        self._preview_band = self._make_band(_C_PREVIEW, width=_style.RB_WIDTH, dashed=True)
        self._preview_band.setVisible(False)
        self._hover_band   = self._make_band(_C_HOVER,   width=_style.RB_WIDTH, dashed=True)
        self._hover_band.setVisible(False)

        self._pending       = []
        self._pending_bands = []

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _show_hint(self, screen_pos):
        text = _HINT.get(self._state, "")
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self._canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self._canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

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

    def _line_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.isSpatial()
            and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.GeometryType.LineGeometry
        ]

    def _find_line_near(self, map_pt):
        tol        = self._hit_tol()
        rect       = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                                  map_pt.x()+tol, map_pt.y()+tol)
        click_geom = QgsGeometry.fromPointXY(map_pt)
        best_layer, best_feat, best_d = None, None, float('inf')
        for lyr in self._line_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                d = feat.geometry().distance(click_geom)
                if d < best_d:
                    best_d     = d
                    best_layer = lyr
                    best_feat  = feat
        if best_d <= tol:
            return best_layer, best_feat
        return None, None

    def _boundary_geoms_for(self, exclude_layer=None, exclude_fid=None):
        geoms = []
        for lyr, fid in self._boundary_edges:
            if lyr is exclude_layer and fid == exclude_fid:
                continue
            f = lyr.getFeature(fid)
            if f.isValid() and not f.geometry().isEmpty():
                geoms.append(f.geometry())
        return geoms

    def _compute_extension(self, line_geom, map_pt, boundary_geoms):
        pts = line_geom.asPolyline()
        if len(pts) < 2:
            return None, None, None

        d_start = map_pt.distance(pts[0])
        d_end   = map_pt.distance(pts[-1])

        if d_start <= d_end:
            ep_idx, ep, adj = 0, pts[0], pts[1]
        else:
            ep_idx, ep, adj = -1, pts[-1], pts[-2]

        dx = ep.x() - adj.x()
        dy = ep.y() - adj.y()
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < 1e-10:
            return None, None, None
        dx /= dist
        dy /= dist

        near_pt = QgsPointXY(ep.x() + dx * 1e-6, ep.y() + dy * 1e-6)
        far_pt  = QgsPointXY(ep.x() + dx * _FAR,  ep.y() + dy * _FAR)
        ray     = QgsGeometry.fromPolylineXY([near_pt, far_pt])

        best_pt = None
        best_d  = float('inf')

        for bg in boundary_geoms:
            inter = ray.intersection(bg)
            if inter is None or inter.isEmpty():
                continue
            gt = QgsWkbTypes.geometryType(inter.wkbType())
            candidates = []
            if gt == QgsWkbTypes.GeometryType.PointGeometry:
                if inter.isMultipart():
                    candidates = [QgsPointXY(p.x(), p.y()) for p in inter.asMultiPoint()]
                else:
                    p = inter.asPoint()
                    candidates = [QgsPointXY(p.x(), p.y())]
            elif gt == QgsWkbTypes.GeometryType.LineGeometry:
                if inter.isMultipart():
                    for part in inter.asMultiPolyline():
                        if part:
                            candidates += [part[0], part[-1]]
                else:
                    pl = inter.asPolyline()
                    if pl:
                        candidates += [pl[0], pl[-1]]
            for pt in candidates:
                d = ep.distance(pt)
                if d > 1e-6 and d < best_d:
                    best_d = d
                    best_pt = pt

        return ep_idx, ep, best_pt

    def _update_boundary(self, layer, feat, shift=False):
        key  = (id(layer), feat.id())
        keys = [(id(lyr), fid) for lyr, fid in self._boundary_edges]
        if shift:
            if key in keys:
                idx = keys.index(key)
                self._boundary_edges.pop(idx)
                self._rm(self._boundary_bands.pop(idx))
                self._log(f"  Deselected: '{layer.name()}' fid {feat.id()}"
                          f"  ({len(self._boundary_edges)} selected)")
        else:
            if key not in keys:
                self._boundary_edges.append((layer, feat.id()))
                band = self._make_band(_C_EDGE, width=_style.RB_WIDTH)
                band.setToGeometry(feat.geometry(), layer)
                self._boundary_bands.append(band)
                self._log(f"  Boundary edge: '{layer.name()}' fid {feat.id()}"
                          f"  ({len(self._boundary_edges)} selected)")
            else:
                self._log("  Already selected")

    def _pending_key(self, layer, fid, ep_idx):
        return (id(layer), fid, ep_idx)

    def _update_extend(self, layer, feat, map_pt, shift=False):
        line_geom = feat.geometry()
        if line_geom.isEmpty() or line_geom.isMultipart():
            self._log("  Multipart geometry — extend not supported")
            return

        boundaries = self._boundary_geoms_for(exclude_layer=layer, exclude_fid=feat.id())
        if not boundaries:
            self._log("  No usable boundary edges for this line")
            return

        ep_idx, ep, ext_pt = self._compute_extension(line_geom, map_pt, boundaries)
        if ext_pt is None:
            self._log("  No boundary intersection found in extension direction")
            return

        key      = self._pending_key(layer, feat.id(), ep_idx)
        existing = [self._pending_key(lyr, fid, ei) for lyr, fid, ei, _, _ in self._pending]

        if shift:
            if key in existing:
                idx = existing.index(key)
                self._pending.pop(idx)
                self._rm(self._pending_bands.pop(idx))
                self._log(f"  Deselected extension  ({len(self._pending)} marked)")
        else:
            if key in existing:
                self._log("  Extension already marked")
            else:
                ext_geom = QgsGeometry.fromPolylineXY([ep, ext_pt])
                self._pending.append((layer, feat.id(), ep_idx, ep, ext_pt))
                band = self._make_band(_C_SELECTED, width=_style.RB_WIDTH, dashed=True)
                band.setToGeometry(ext_geom, layer)
                self._pending_bands.append(band)
                n = len(self._pending)
                self._log(f"  Marked extension  {ep.distance(ext_pt):.3f} u"
                          f"  on '{layer.name()}'  ({n} end(s) selected)")

        self._preview_band.setVisible(False)

    def _confirm_all_extends(self):
        if not self._pending:
            return

        groups = {}
        for entry in self._pending:
            layer, fid = entry[0], entry[1]
            groups.setdefault((id(layer), fid), []).append(entry)

        total = 0
        for (_, fid), entries in groups.items():
            layer = entries[0][0]
            feat  = layer.getFeature(fid)
            if not feat.isValid():
                continue
            pts = list(feat.geometry().asPolyline())

            end_exts   = [e for e in entries if e[2] == -1]
            start_exts = [e for e in entries if e[2] ==  0]
            for _, _, _, _, ext_pt in end_exts:
                pts = pts + [ext_pt]
            for _, _, _, _, ext_pt in start_exts:
                pts = [ext_pt] + pts

            new_geom = QgsGeometry.fromPolylineXY(pts)
            if not layer.isEditable():
                layer.startEditing()
            layer.changeGeometry(fid, new_geom)
            layer.triggerRepaint()
            self._modified_layers.add(layer)
            total += len(entries)

        for band in self._pending_bands:
            self._rm(band)
        self._pending_bands = []
        self._pending       = []
        self._log(f"  Extended {total} line end(s)", "#88ff88")

    def _clear_pending(self):
        for band in self._pending_bands:
            self._rm(band)
        self._pending_bands = []
        self._pending       = []

    def _update_preview(self, map_pt):
        if self._state == _ST_SELECT:
            layer, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._hover_band.setToGeometry(feat.geometry(), layer)
                self._hover_band.setVisible(True)
            else:
                self._hover_band.setVisible(False)
            self._preview_band.setVisible(False)
            return

        self._hover_band.setVisible(False)
        layer, feat = self._find_line_near(map_pt)
        if feat is None or feat.geometry().isMultipart():
            self._preview_band.setVisible(False)
            return

        boundaries = self._boundary_geoms_for(exclude_layer=layer, exclude_fid=feat.id())
        _, ep, ext_pt = self._compute_extension(feat.geometry(), map_pt, boundaries)
        if ep is not None and ext_pt is not None:
            self._preview_band.setToGeometry(
                QgsGeometry.fromPolylineXY([ep, ext_pt]), layer
            )
            self._preview_band.setVisible(True)
        else:
            self._preview_band.setVisible(False)

    def _advance_to_extend(self):
        if not self._boundary_edges:
            for lyr in self._line_layers():
                for feat in lyr.getFeatures():
                    if feat.geometry().isEmpty():
                        continue
                    self._boundary_edges.append((lyr, feat.id()))
                    band = self._make_band(_C_EDGE, width=_style.RB_WIDTH)
                    band.setToGeometry(feat.geometry(), lyr)
                    self._boundary_bands.append(band)
            self._log(f"  All lines as boundaries ({len(self._boundary_edges)} features)")
        else:
            self._log(f"  {len(self._boundary_edges)} boundary edge(s) confirmed")

        self._state = _ST_EXTEND
        self._log("  Click line ends to mark", "#88ccff")

    def _finish(self):
        self._confirm_all_extends()
        self._go_home()

    def _dispatch(self, sem):
        pass

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        self._log("EXTEND  ──  click boundary edges", "#aaddff")

    def deactivate(self):
        for band in self._boundary_bands:
            self._rm(band)
        self._boundary_bands = []
        self._boundary_edges = []
        self._clear_pending()
        self._rm(self._preview_band)
        self._rm(self._hover_band)
        self._state = _ST_SELECT
        self._modified_layers.clear()
        self._hint.hide()
        super().deactivate()

    def canvasMoveEvent(self, event):
        self._update_preview(self.toMapCoordinates(event.pos()))
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())

        if event.button() == Qt.MouseButton.RightButton:
            if self._state == _ST_SELECT:
                self._advance_to_extend()
            else:
                self._finish()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if self._state == _ST_SELECT:
            layer, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._update_boundary(layer, feat, shift=shift)
            else:
                self._log("  No line found near click")
        elif self._state == _ST_EXTEND:
            layer, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._update_extend(layer, feat, map_pt, shift=shift)
            else:
                self._log("  No line found near click")

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._state == _ST_SELECT:
                self._advance_to_extend()
            else:
                self._finish()
