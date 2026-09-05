# -*- coding: utf-8 -*-
"""
AutoCAD-style TRIM tool.

Workflow
────────
Phase 1  (_ST_SELECT)
    Click line features to designate them as cutting edges (cyan highlight).
    Shift+click removes a cutting edge.
    Enter / RMB with no edge selected  → every line on the map becomes a cutting edge.
    Enter / RMB with edges selected    → those edges are confirmed.

Phase 2  (_ST_TRIM)
    Hover near any line.  The preview rubber band shows the segment under the
    cursor between the nearest pair of cutting-edge intersections.
    Left-click → mark that segment for removal (red highlight).
    Shift+left-click → unmark a previously marked segment.
    Enter / RMB → trim all marked segments at once, then exit.
    Esc → exit without trimming.
"""

import contextlib

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType, SnapType
from ...core import style as _style

_SNAP_ICONS = {
    SnapType.VERTEX:        (_style.SNAP_ICON['endpoint'],     _style._CC_COLOR),
    SnapType.POINT:         (_style.SNAP_ICON['point'],        _style._CC_COLOR),
    SnapType.MIDPOINT:      (_style.SNAP_ICON['midpoint'],     _style._CC_COLOR),
    SnapType.CENTER:        (_style.SNAP_ICON['center'],       _style._CC_COLOR),
    SnapType.INTERSECTION:  (_style.SNAP_ICON['intersection'], _style._CC_COLOR),
    SnapType.PERPENDICULAR: (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.EXTENSION:     (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.NEAREST:       (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.SELF:          (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.GRID:          (QgsVertexMarker.ICON_CROSS,       _style.SNAP_GRID_COLOR),
}

# ── state constants ────────────────────────────────────────────────────────────
_ST_SELECT = 0   # selecting cutting edges
_ST_TRIM   = 1   # marking segments for removal

_HIT_PX = 10


class TrimTool(QgsMapTool):

    inputModeChanged = pyqtSignal(str, str)
    promptChanged    = pyqtSignal(str)

    # ── construction ───────────────────────────────────────────────────────────

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state = _ST_SELECT

        # Phase 1 — cutting edges
        self._cutting_edges = []   # [(QgsVectorLayer, fid), ...]
        self._cutting_bands = []   # parallel QgsRubberBand list

        # Rubber bands
        self._hover_band   = self._make_band(_style.RB_HOVER,    dashed=False)
        self._preview_band = self._make_band(_style.RB_PREVIEW,  dashed=True)
        self._hover_band.setVisible(False)
        self._preview_band.setVisible(False)

        # Phase 2 — segments marked for removal
        self._pending_trims  = []   # [(layer, fid, orig_geom, boundaries, trim_idx), ...]
        self._selected_bands = []   # parallel QgsRubberBand list

        self._modified_layers = set()
        self._snap_marker = None

    # ── logging ────────────────────────────────────────────────────────────────

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    # ── rubber-band helpers ────────────────────────────────────────────────────

    def _make_band(self, color, dashed=False):
        band = QgsRubberBand(self._canvas, QgsWkbTypes.GeometryType.LineGeometry)
        band.setColor(color)
        band.setWidth(_style.RB_WIDTH)
        band.setLineStyle(_style.RB_LINE_STYLE)
        return band

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    # ── snap marker ────────────────────────────────────────────────────────────

    def _snapped(self, raw_pt):
        engine = getattr(self._ctx, 'snap_engine', None)
        if engine:
            result = engine.resolve(raw_pt, self._canvas)
            if result:
                self._show_snap_marker(result.point, result.snap_type)
                return result.point
        self._hide_snap_marker()
        return raw_pt

    def _show_snap_marker(self, pt, snap_type):
        icon, color = _SNAP_ICONS.get(snap_type, (QgsVertexMarker.ICON_BOX, _style._CC_COLOR))
        if self._snap_marker is None:
            self._snap_marker = QgsVertexMarker(self._canvas)
            self._snap_marker.setIconSize(_style.SNAP_ICON_SIZE)
            self._snap_marker.setPenWidth(_style.SNAP_PEN_WIDTH)
        self._snap_marker.setIconType(icon)
        self._snap_marker.setColor(color)
        self._snap_marker.setCenter(pt)
        self._snap_marker.show()

    def _hide_snap_marker(self):
        if self._snap_marker:
            self._snap_marker.hide()

    def _clear_snap_marker(self):
        if self._snap_marker is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(self._snap_marker)
            self._snap_marker = None

    # ── geometry helpers ───────────────────────────────────────────────────────

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
        """Return (layer, feat) for the closest line within hit tolerance, or (None, None)."""
        tol  = self._hit_tol()
        rect = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                            map_pt.x()+tol, map_pt.y()+tol)
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        best_layer, best_feat, best_d = None, None, float('inf')
        for lyr in self._line_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                d = feat.geometry().distance(pt_geom)
                if d < best_d:
                    best_d, best_layer, best_feat = d, lyr, feat
        return (best_layer, best_feat) if best_d <= tol else (None, None)

    # ── Phase 1: cutting edge management ──────────────────────────────────────

    def _add_cutting_edge(self, layer, feat):
        key  = (id(layer), feat.id())
        keys = [(id(l), f) for l, f in self._cutting_edges]
        if key in keys:
            self._log("  Already a cutting edge")
            return
        self._cutting_edges.append((layer, feat.id()))
        band = self._make_band(_style.RB_EDGE)
        band.setToGeometry(feat.geometry(), layer)
        self._cutting_bands.append(band)
        self._log(f"  Cutting edge: '{layer.name()}' fid {feat.id()}"
                  f"  ({len(self._cutting_edges)} selected)")

    def _remove_cutting_edge(self, layer, feat):
        key  = (id(layer), feat.id())
        keys = [(id(l), f) for l, f in self._cutting_edges]
        if key not in keys:
            return
        idx = keys.index(key)
        self._cutting_edges.pop(idx)
        self._rm(self._cutting_bands.pop(idx))
        self._log(f"  Removed cutting edge  ({len(self._cutting_edges)} remaining)")

    def _clear_cutting_edges(self):
        for band in self._cutting_bands:
            self._rm(band)
        self._cutting_bands = []
        self._cutting_edges = []

    def _advance_to_trim(self):
        """Confirm cutting edges and enter Phase 2."""
        if not self._cutting_edges:
            for lyr in self._line_layers():
                for feat in lyr.getFeatures():
                    if feat.geometry().isEmpty():
                        continue
                    self._cutting_edges.append((lyr, feat.id()))
                    band = self._make_band(_style.RB_EDGE)
                    band.setToGeometry(feat.geometry(), lyr)
                    self._cutting_bands.append(band)
            self._log(f"  All lines as cutting edges  ({len(self._cutting_edges)} features)")
        else:
            self._log(f"  {len(self._cutting_edges)} cutting edge(s) confirmed")

        self._state = _ST_TRIM
        self._log("  Click segments to mark for removal", "#88ccff")
        self.promptChanged.emit("Click segment to trim:")

    # ── Phase 2: trim logic ────────────────────────────────────────────────────

    def _cutting_geoms_for(self, exclude_layer=None, exclude_fid=None):
        geoms = []
        for lyr, fid in self._cutting_edges:
            if lyr is exclude_layer and fid == exclude_fid:
                continue
            f = lyr.getFeature(fid)
            if f.isValid() and not f.geometry().isEmpty():
                geoms.append(f.geometry())
        return geoms

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
        return QgsGeometry.fromPolylineXY(pts) if len(pts) >= 2 else None

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

    def _pending_key(self, layer, fid, trim_idx):
        return (id(layer), fid, trim_idx)

    def _mark_segment(self, layer, feat, click_pt, shift=False):
        line_geom = feat.geometry()
        if line_geom.isEmpty() or line_geom.isMultipart():
            self._log("  Multipart geometry — trim not supported")
            return

        cutting = self._cutting_geoms_for(exclude_layer=layer, exclude_fid=feat.id())
        if not cutting:
            self._log("  No usable cutting edges for this line")
            return

        trim_idx, boundaries = self._trim_interval(line_geom, cutting, click_pt)
        if trim_idx is None:
            self._log("  No intersection with cutting edges found")
            return

        key = self._pending_key(layer, feat.id(), trim_idx)
        existing = [
            self._pending_key(lyr, fid, ti)
            for lyr, fid, _, _, ti in self._pending_trims
        ]

        if shift:
            if key in existing:
                idx = existing.index(key)
                self._pending_trims.pop(idx)
                self._rm(self._selected_bands.pop(idx))
                self._log(f"  Unmarked segment  ({len(self._pending_trims)} marked)")
        else:
            if key in existing:
                self._log("  Segment already marked")
            else:
                d_a, d_b = boundaries[trim_idx], boundaries[trim_idx + 1]
                sub = self._sub_line(line_geom, d_a, d_b)
                if sub is None:
                    return
                self._pending_trims.append((layer, feat.id(), line_geom, boundaries, trim_idx))
                band = self._make_band(_style.RB_DESTROY)
                band.setWidth(_style.RB_WIDTH_THICK)
                band.setToGeometry(sub, layer)
                self._selected_bands.append(band)
                n = len(self._pending_trims)
                self._log(f"  Marked  {d_b - d_a:.3f} m  on '{layer.name()}'  ({n} segment(s))")

        self._preview_band.setVisible(False)

    def _clear_pending(self):
        for band in self._selected_bands:
            self._rm(band)
        self._selected_bands = []
        self._pending_trims  = []

    def _confirm_all_trims(self):
        if not self._pending_trims:
            return

        groups = {}
        for entry in self._pending_trims:
            key = (id(entry[0]), entry[1])
            groups.setdefault(key, []).append(entry)

        total = 0
        for (_, fid), entries in groups.items():
            layer     = entries[0][0]
            orig_geom = entries[0][2]
            feat      = layer.getFeature(fid)
            if not feat.isValid():
                continue

            all_bounds = set()
            remove_set = set()
            for _, _, _, boundaries, trim_idx in entries:
                for b in boundaries:
                    all_bounds.add(round(b, 8))
                d_a = round(boundaries[trim_idx],     8)
                d_b = round(boundaries[trim_idx + 1], 8)
                remove_set.add((d_a, d_b))

            all_bounds = sorted(all_bounds)
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
            total += len(entries)

        self._log(f"  Trimmed {total} segment(s)", "#88ff88")

    # ── preview ────────────────────────────────────────────────────────────────

    def _update_preview(self, map_pt):
        layer, feat = self._find_line_near(map_pt)

        if feat is None or feat.geometry().isMultipart():
            self._hover_band.setVisible(False)
            self._preview_band.setVisible(False)
            return

        line_geom = feat.geometry()

        if self._state == _ST_SELECT:
            self._hover_band.setToGeometry(line_geom, layer)
            self._hover_band.setVisible(True)
            self._preview_band.setVisible(False)
            return

        self._hover_band.setVisible(False)
        cutting = self._cutting_geoms_for(exclude_layer=layer, exclude_fid=feat.id())
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

    # ── typed-input dispatch ───────────────────────────────────────────────────

    def _dispatch(self, sem):
        pass   # trim has no typed-distance input

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    def _finish(self):
        """Commit all pending trims, clear visuals, return to home tool."""
        self._confirm_all_trims()
        self._clear_pending()
        self._clear_cutting_edges()
        self._preview_band.setVisible(False)
        self._hover_band.setVisible(False)
        self._go_home()

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        sel = getattr(self._ctx, 'selection_model', None)
        if sel and not sel.is_empty():
            for lid, fid in sel:
                layer = QgsProject.instance().mapLayer(lid)
                if not isinstance(layer, QgsVectorLayer):
                    continue
                if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.GeometryType.LineGeometry:
                    continue
                feat = layer.getFeature(fid)
                if feat.isValid() and not feat.geometry().isEmpty():
                    self._cutting_edges.append((layer, feat.id()))
                    band = self._make_band(_style.RB_EDGE)
                    band.setToGeometry(feat.geometry(), layer)
                    self._cutting_bands.append(band)
        if self._cutting_edges:
            self._log("TRIM", "#aaddff")
            self._last_input_mode   = "no_value"
            self._last_input_prompt = "Click segment to trim:"
            self.inputModeChanged.emit("no_value", "Click segment to trim:")
            self._advance_to_trim()
        else:
            self._log("TRIM  ──  click cutting edges  (Enter = skip, use all)", "#aaddff")
            self._last_input_mode   = "no_value"
            self._last_input_prompt = "Click cutting edges:"
            self.inputModeChanged.emit("no_value", "Click cutting edges:")

    def deactivate(self):
        self._clear_cutting_edges()
        self._clear_pending()
        self._clear_snap_marker()
        self._rm(self._preview_band)
        self._rm(self._hover_band)
        self._state = _ST_SELECT
        self._modified_layers.clear()
        self.inputModeChanged.emit("", "")
        super().deactivate()

    # ── Qt event handlers ──────────────────────────────────────────────────────

    def canvasMoveEvent(self, event):
        self._update_preview(self._snapped(self.toMapCoordinates(event.pos())))

    def canvasPressEvent(self, event):
        map_pt = self._snapped(self.toMapCoordinates(event.pos()))
        btn    = event.button()

        if btn == Qt.MouseButton.RightButton:
            if self._state == _ST_SELECT:
                self._advance_to_trim()
            else:
                self._finish()
            return

        if btn != Qt.MouseButton.LeftButton:
            return

        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if self._state == _ST_SELECT:
            layer, feat = self._find_line_near(map_pt)
            if feat is None:
                self._log("  No line found near click")
                return
            if shift:
                self._remove_cutting_edge(layer, feat)
            else:
                self._add_cutting_edge(layer, feat)

        elif self._state == _ST_TRIM:
            layer, feat = self._find_line_near(map_pt)
            if feat is None:
                self._log("  No line found near click")
                return
            self._mark_segment(layer, feat, map_pt, shift=shift)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._finish()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._state == _ST_SELECT:
                self._advance_to_trim()
            else:
                self._finish()
