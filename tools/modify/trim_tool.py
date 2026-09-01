# -*- coding: utf-8 -*-
"""
AutoCAD-style TRIM tool.

Two-phase workflow:
  Phase 1 – Select cutting edges
      Click lines (highlighted cyan).  Enter/RMB → advance.
      (Enter/RMB with nothing selected → treat ALL lines as cutting edges.)

  Phase 2 – Trim
      Hover: orange dashed preview shows segment under cursor.
      Click: marks segment red.  Click again to deselect.
      Enter/RMB → trim ALL marked segments at once, exit.
      Esc → cancel.
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

_C_EDGE     = _style.RB_EDGE
_C_PREVIEW  = _style.RB_PREVIEW
_C_SELECTED = _style.RB_DESTROY
_C_HOVER    = _style.RB_HOVER

_ST_SELECT = 0
_ST_TRIM   = 1

_HIT_PX = 10

_HINT_STYLE = (
    "QLabel{background:rgba(20,20,20,210);color:#f0f0f0;"
    "border:1px solid rgba(255,255,255,80);border-radius:4px;"
    "padding:3px 8px;font-size:9pt;}"
)

_HINT = {
    _ST_SELECT: "Click cutting edges",
    _ST_TRIM:   "Click segment to mark for trim",
}


class TrimTool(QgsMapTool):
    """AutoCAD-style TRIM tool — multi-select segments then confirm all at once."""

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state           = _ST_SELECT
        self._cutting_edges   = []
        self._cutting_bands   = []
        self._modified_layers = set()

        self._preview_band = self._make_band(_C_PREVIEW, width=_style.RB_WIDTH, dashed=True)
        self._preview_band.setVisible(False)
        self._hover_band   = self._make_band(_C_HOVER,   width=_style.RB_WIDTH, dashed=True)
        self._hover_band.setVisible(False)

        self._pending_trims  = []
        self._selected_bands = []

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

    def _intersection_dists(self, line_geom, cutting_geoms):
        dists = set()
        for cg in cutting_geoms:
            inter = line_geom.intersection(cg)
            if inter is None or inter.isEmpty():
                continue
            gt = QgsWkbTypes.geometryType(inter.wkbType())
            pts = []
            if gt == QgsWkbTypes.GeometryType.PointGeometry:
                if inter.isMultipart():
                    pts = [QgsPointXY(p.x(), p.y()) for p in inter.asMultiPoint()]
                else:
                    p = inter.asPoint()
                    pts = [QgsPointXY(p.x(), p.y())]
            elif gt == QgsWkbTypes.GeometryType.LineGeometry:
                if inter.isMultipart():
                    for part in inter.asMultiPolyline():
                        if part:
                            pts += [part[0], part[-1]]
                else:
                    pl = inter.asPolyline()
                    if pl:
                        pts += [pl[0], pl[-1]]
            for pt in pts:
                d = line_geom.lineLocatePoint(QgsGeometry.fromPointXY(pt))
                dists.add(round(d, 8))
        return sorted(dists)

    def _sub_line(self, geom, d_from, d_to):
        if d_to - d_from < 1e-10:
            return None
        pts = []
        s = geom.interpolate(d_from)
        if not s.isEmpty():
            p = s.asPoint()
            pts.append(QgsPointXY(p.x(), p.y()))
        verts = geom.asPolyline()
        cum   = 0.0
        for i, v in enumerate(verts):
            if i > 0:
                cum += verts[i - 1].distance(v)
            if d_from < cum < d_to:
                pts.append(v)
        e = geom.interpolate(d_to)
        if not e.isEmpty():
            p = e.asPoint()
            pts.append(QgsPointXY(p.x(), p.y()))
        if len(pts) >= 2:
            return QgsGeometry.fromPolylineXY(pts)
        return None

    def _trim_interval(self, line_geom, cutting_geoms, click_pt):
        dists = self._intersection_dists(line_geom, cutting_geoms)
        if not dists:
            return None, None
        total      = line_geom.length()
        click_dist = line_geom.lineLocatePoint(QgsGeometry.fromPointXY(click_pt))
        boundaries = [0.0] + dists + [total]
        trim_idx   = len(boundaries) - 2
        for i in range(len(boundaries) - 1):
            if boundaries[i] <= click_dist <= boundaries[i + 1]:
                trim_idx = i
                break
        return trim_idx, boundaries

    def _cutting_geoms_for(self, exclude_layer=None, exclude_fid=None):
        geoms = []
        for lyr, fid in self._cutting_edges:
            if lyr is exclude_layer and fid == exclude_fid:
                continue
            f = lyr.getFeature(fid)
            if f.isValid() and not f.geometry().isEmpty():
                geoms.append(f.geometry())
        return geoms

    def _pending_key(self, layer, fid, trim_idx):
        return (id(layer), fid, trim_idx)

    def _update_cutting_edge(self, layer, feat, shift=False):
        key  = (id(layer), feat.id())
        keys = [(id(lyr), fid) for lyr, fid in self._cutting_edges]
        if shift:
            if key in keys:
                idx = keys.index(key)
                self._cutting_edges.pop(idx)
                self._rm(self._cutting_bands.pop(idx))
                self._log(f"  Deselected: '{layer.name()}' fid {feat.id()}"
                          f"  ({len(self._cutting_edges)} selected)")
        else:
            if key not in keys:
                self._cutting_edges.append((layer, feat.id()))
                band = self._make_band(_C_EDGE, width=_style.RB_WIDTH)
                band.setToGeometry(feat.geometry(), layer)
                self._cutting_bands.append(band)
                self._log(f"  Cutting edge: '{layer.name()}' fid {feat.id()}"
                          f"  ({len(self._cutting_edges)} selected)")
            else:
                self._log(f"  Already selected")

    def _update_mark(self, layer, feat, click_pt, shift=False):
        line_geom = feat.geometry()
        if line_geom.isEmpty() or line_geom.isMultipart():
            self._log("  Multipart geometry — trim not supported (use single-part lines)")
            return

        cutting = self._cutting_geoms_for(exclude_layer=layer, exclude_fid=feat.id())
        if not cutting:
            self._log("  No usable cutting edges for this line")
            return

        trim_idx, boundaries = self._trim_interval(line_geom, cutting, click_pt)
        if trim_idx is None:
            self._log("  No intersection with cutting edges found on this line")
            return

        key = self._pending_key(layer, feat.id(), trim_idx)
        existing_keys = [
            self._pending_key(lyr, fid, ti)
            for lyr, fid, _, _, ti in self._pending_trims
        ]

        if shift:
            if key in existing_keys:
                idx = existing_keys.index(key)
                self._pending_trims.pop(idx)
                self._rm(self._selected_bands.pop(idx))
                self._log(f"  Deselected segment  ({len(self._pending_trims)} marked)")
        else:
            if key in existing_keys:
                self._log(f"  Segment already marked")
            else:
                d_a, d_b = boundaries[trim_idx], boundaries[trim_idx + 1]
                sub = self._sub_line(line_geom, d_a, d_b)
                if sub is None:
                    return
                self._pending_trims.append((layer, feat.id(), line_geom, boundaries, trim_idx))
                band = self._make_band(_C_SELECTED, width=_style.RB_WIDTH_THICK)
                band.setToGeometry(sub, layer)
                self._selected_bands.append(band)
                n = len(self._pending_trims)
                self._log(f"  Marked  {d_b - d_a:.3f} u  on '{layer.name()}'  ({n} segment(s) selected)")

        self._preview_band.setVisible(False)

    def _confirm_all_trims(self):
        if not self._pending_trims:
            return

        groups = {}
        for entry in self._pending_trims:
            layer, fid = entry[0], entry[1]
            key = (id(layer), fid)
            groups.setdefault(key, []).append(entry)

        total_segments = 0
        for (layer_id, fid), entries in groups.items():
            layer     = entries[0][0]
            orig_geom = entries[0][2]
            feat      = layer.getFeature(fid)
            if not feat.isValid():
                continue

            all_bounds_set = set()
            remove_set     = set()
            for _, _, _, boundaries, trim_idx in entries:
                for b in boundaries:
                    all_bounds_set.add(round(b, 8))
                d_a = round(boundaries[trim_idx],     8)
                d_b = round(boundaries[trim_idx + 1], 8)
                remove_set.add((d_a, d_b))

            all_bounds = sorted(all_bounds_set)
            remaining  = []
            for i in range(len(all_bounds) - 1):
                interval = (round(all_bounds[i], 8), round(all_bounds[i + 1], 8))
                if interval in remove_set:
                    continue
                sub = self._sub_line(orig_geom, all_bounds[i], all_bounds[i + 1])
                if sub is not None:
                    remaining.append(sub)

            if not layer.isEditable():
                layer.startEditing()
            self._modified_layers.add(layer)

            if remaining:
                layer.changeGeometry(fid, remaining[0])
                for extra in remaining[1:]:
                    new_feat = QgsFeature(layer.fields())
                    new_feat.setGeometry(extra)
                    new_feat.setAttributes(feat.attributes())
                    layer.addFeature(new_feat)
            else:
                layer.deleteFeature(fid)

            layer.triggerRepaint()
            total_segments += len(entries)

        for band in self._selected_bands:
            self._rm(band)
        self._selected_bands = []
        self._pending_trims  = []
        self._log(f"  Trimmed {total_segments} segment(s)", "#88ff88")

    def _clear_pending(self):
        for band in self._selected_bands:
            self._rm(band)
        self._selected_bands = []
        self._pending_trims  = []

    def _update_preview(self, map_pt):
        layer, feat = self._find_line_near(map_pt)

        if feat is None:
            self._hover_band.setVisible(False)
            self._preview_band.setVisible(False)
            return

        line_geom = feat.geometry()
        if line_geom.isMultipart():
            self._hover_band.setVisible(False)
            self._preview_band.setVisible(False)
            return

        if self._state == _ST_SELECT:
            self._hover_band.setToGeometry(line_geom, layer)
            self._hover_band.setVisible(True)
            self._preview_band.setVisible(False)
            return

        self._hover_band.setVisible(False)
        cutting  = self._cutting_geoms_for(exclude_layer=layer, exclude_fid=feat.id())
        trim_idx, boundaries = self._trim_interval(line_geom, cutting, map_pt)
        if trim_idx is None:
            self._preview_band.setVisible(False)
            return
        sub = self._sub_line(line_geom, boundaries[trim_idx], boundaries[trim_idx + 1])
        if sub:
            self._preview_band.setToGeometry(sub, layer)
            self._preview_band.setVisible(True)
        else:
            self._preview_band.setVisible(False)

    def _advance_to_trim(self):
        if not self._cutting_edges:
            for lyr in self._line_layers():
                for feat in lyr.getFeatures():
                    if feat.geometry().isEmpty():
                        continue
                    self._cutting_edges.append((lyr, feat.id()))
                    band = self._make_band(_C_EDGE, width=_style.RB_WIDTH)
                    band.setToGeometry(feat.geometry(), lyr)
                    self._cutting_bands.append(band)
            self._log(f"  All lines as cutting edges ({len(self._cutting_edges)} features)")
        else:
            self._log(f"  {len(self._cutting_edges)} cutting edge(s) confirmed")

        self._state = _ST_TRIM
        self._log("  Click segments to mark for removal", "#88ccff")

    def _finish(self):
        self._confirm_all_trims()
        self._go_home()

    def _dispatch(self, sem):
        pass  # trim uses Enter key via keyPressEvent only

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        self._log("TRIM  ──  click cutting edges", "#aaddff")

    def deactivate(self):
        for band in self._cutting_bands:
            self._rm(band)
        self._cutting_bands = []
        self._cutting_edges = []
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
                self._advance_to_trim()
            else:
                self._finish()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if self._state == _ST_SELECT:
            layer, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._update_cutting_edge(layer, feat, shift=shift)
            else:
                self._log("  No line found near click")
        elif self._state == _ST_TRIM:
            layer, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._update_mark(layer, feat, map_pt, shift=shift)
            else:
                self._log("  No line found near click")

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._state == _ST_SELECT:
                self._advance_to_trim()
            else:
                self._finish()
