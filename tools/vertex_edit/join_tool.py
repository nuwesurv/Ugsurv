# -*- coding: utf-8 -*-
"""
AutoCAD-style JOIN tool.

Click polylines to add them to the join set (highlighted green).
Click a highlighted feature again to deselect it.
Enter / RMB → validate all selected lines share endpoints, then join
              them into one LineString and delete the originals.
Esc → cancel.

Contact-point rule
──────────────────
Every consecutive pair in the final chain must share an endpoint within
_TOUCH_TOL map units.  Any selection where no valid chain is found is
rejected with a log message — no geometry is modified.

Chaining uses backtracking over all orderings and per-segment reversals
(feasible for the typical 2–6 features used in practice).
"""

import contextlib
from itertools import permutations

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)
from ...core.events import EventType
from ...core import style as _style
from ..layer_utils import polyline_attrs

_C_SEL   = _style.RB_SOURCE
_C_HOVER = _style.RB_HOVER

_HIT_PX    = 10
_TOUCH_TOL = 1e-3

_HINT_STYLE = (
    "QLabel{background:rgba(20,20,20,210);color:#f0f0f0;"
    "border:1px solid rgba(255,255,255,80);border-radius:4px;"
    "padding:3px 8px;font-size:9pt;}"
)


class JoinTool(QgsMapTool):
    """Click polylines to build a join set; Enter chains them into one."""

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._selected  = []
        self._sel_bands = {}
        self._hover_band = None
        self._hover_key  = None

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _show_hint(self, screen_pos, text):
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

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    def _make_band(self, geom, color, width=_style.RB_WIDTH):
        gt = (QgsWkbTypes.geometryType(geom.wkbType())
              if not geom.isEmpty() else QgsWkbTypes.GeometryType.LineGeometry)
        band = QgsRubberBand(self._canvas, gt)
        band.setColor(color)
        band.setWidth(width)
        band.setLineStyle(_style.RB_LINE_STYLE)
        return band

    def _rm_band(self, band):
        if band is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(band)

    def _clear_hover(self):
        self._rm_band(self._hover_band)
        self._hover_band = None
        self._hover_key  = None

    def _clear_all(self):
        self._clear_hover()
        for band in self._sel_bands.values():
            self._rm_band(band)
        self._sel_bands.clear()
        self._selected.clear()

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

    def _find_line_near(self, map_pt):
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
                if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.GeometryType.LineGeometry:
                    continue
                d = geom.distance(pt_geom)
                if d < best_d:
                    best_d = d
                    best = (lyr, feat.id())
        return best

    def _is_selected(self, layer, fid):
        return (id(layer), fid) in self._sel_bands

    def _select(self, layer, fid):
        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if geom.isEmpty():
            return
        band = self._make_band(geom, _C_SEL, width=_style.RB_WIDTH)
        band.setToGeometry(geom, layer)
        self._sel_bands[(id(layer), fid)] = band
        self._selected.append((layer, fid))
        self._log(f"  Selected fid {fid} of '{layer.name()}'  [{len(self._selected)} total]")

    def _deselect(self, layer, fid):
        self._rm_band(self._sel_bands.pop((id(layer), fid), None))
        self._selected = [(lyr, f) for lyr, f in self._selected
                          if not (id(lyr) == id(layer) and f == fid)]
        self._log(f"  Deselected fid {fid}  [{len(self._selected)} total]")

    def _toggle(self, layer, fid):
        if self._is_selected(layer, fid):
            self._deselect(layer, fid)
        else:
            self._select(layer, fid)

    def _geom_points(self, geom):
        pts = []
        it  = geom.vertices()
        while it.hasNext():
            v = it.next()
            pts.append(QgsPointXY(v.x(), v.y()))
        return pts

    def _touch(self, p1, p2):
        return p1.distance(p2) <= _TOUCH_TOL

    def _try_order(self, segments, order):
        first_pts = segments[order[0]][2]
        for start_reversed in (False, True):
            chain = list(reversed(first_pts)) if start_reversed else list(first_pts)
            ok = True
            for k in range(1, len(order)):
                pts  = segments[order[k]][2]
                tail = chain[-1]
                if self._touch(tail, pts[0]):
                    chain.extend(pts[1:])
                elif self._touch(tail, pts[-1]):
                    chain.extend(list(reversed(pts))[1:])
                else:
                    ok = False
                    break
            if ok:
                return chain
        return None

    def _chain_or_reject(self, segments):
        for order in permutations(range(len(segments))):
            result = self._try_order(segments, order)
            if result is not None:
                return result, None
        return None, "polylines not touching — endpoints must share a vertex"

    def _join_and_commit(self):
        if len(self._selected) < 2:
            self._log("  Select at least 2 polylines first", "#ffaaaa")
            return

        segments = []
        for lyr, fid in self._selected:
            feat = lyr.getFeature(fid)
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            pts = self._geom_points(geom)
            if len(pts) >= 2:
                segments.append((lyr, fid, pts))

        if len(segments) < 2:
            self._log("  Not enough valid geometries", "#ffaaaa")
            return

        chained, err = self._chain_or_reject(segments)
        if err:
            self._log(f"  JOIN failed: {err}", "#ffaaaa")
            return

        new_geom   = QgsGeometry.fromPolylineXY(chained)
        attrs      = polyline_attrs(new_geom)
        length     = attrs.get("length",    0.0)
        is_closed  = attrs.get("closed",    False)
        area_sqm   = attrs.get("area_sqm",  0.0)
        area_acres = attrs.get("area_acres", 0.0)

        target = segments[0][0]
        if not target.isEditable():
            target.startEditing()

        new_feat = QgsFeature(target.fields())
        new_feat.setGeometry(new_geom)
        for fname, val in attrs.items():
            idx = target.fields().indexOf(fname)
            if idx >= 0:
                new_feat.setAttribute(idx, val)
        target.addFeature(new_feat)

        for lyr, fid, _ in segments:
            if not lyr.isEditable():
                lyr.startEditing()
            lyr.deleteFeature(fid)

        seen = set()
        for lyr, _, _ in segments:
            lid = id(lyr)
            if lid not in seen:
                lyr.updateExtents()
                lyr.triggerRepaint()
                seen.add(lid)

        n = len(segments)
        msg = (f"  Joined {n} polylines → 1 feature  "
               f"({len(chained)} vertices, length: {length:.3f})")
        if is_closed:
            msg += f"  area: {area_sqm:.3f} sqm ({area_acres:.4f} acres)"
        self._log(msg, "#88ff88")

        self._clear_all()
        self._log("  JOIN: click polylines to select")

    def _dispatch(self, sem):
        pass

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        self._log("JOIN  ──  click polylines to select", "#aaddff")

    def deactivate(self):
        self._clear_all()
        self._hint.hide()
        super().deactivate()

    def canvasMoveEvent(self, event):
        raw_pt = self.toMapCoordinates(event.pos())
        hit    = self._find_line_near(raw_pt)

        if hit:
            lyr, fid = hit
            key = (id(lyr), fid)
            if not self._is_selected(lyr, fid):
                if self._hover_key != key:
                    self._clear_hover()
                    feat = lyr.getFeature(fid)
                    geom = feat.geometry()
                    if not geom.isEmpty():
                        band = self._make_band(geom, _C_HOVER, width=_style.RB_WIDTH)
                        band.setToGeometry(geom, lyr)
                        self._hover_band = band
                        self._hover_key  = key
            else:
                self._clear_hover()
        else:
            self._clear_hover()

        n = len(self._selected)
        if n >= 2:
            self._show_hint(event.pos(), f"{n} selected")
        elif n == 1:
            self._show_hint(event.pos(), "Select 1 more polyline")
        else:
            self._show_hint(event.pos(), "Click polylines to select")

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._join_and_commit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        raw_pt = self.toMapCoordinates(event.pos())
        hit    = self._find_line_near(raw_pt)
        if hit:
            self._clear_hover()
            self._toggle(*hit)
        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._join_and_commit()
