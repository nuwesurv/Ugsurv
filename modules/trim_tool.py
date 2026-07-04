"""
AutoCAD-style TRIM tool.

Two-phase workflow:
  Phase 1 – Select cutting edges
      Click lines to use as cutting boundaries (highlighted cyan).
      Enter / Space / RMB  → advance to trim phase.
      (Enter/RMB with nothing selected → treat ALL lines as cutting edges,
       matching AutoCAD's 'Select objects: <Enter>' shortcut.)

  Phase 2 – Trim (multi-select then confirm)
      Hover   : orange dashed preview shows the segment under the cursor.
      Click   : segment is marked with a persistent red band (no deletion yet).
                Click the same segment again to deselect it.
                Click as many segments as you want.
      Enter / Space / RMB → trim ALL marked segments at once, commit, exit.
      Esc     → cancel without trimming anything.
"""

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsFeature,
)
from qgis.PyQt.QtGui import QColor


_C_EDGE     = QColor(  0, 210, 210, 220)   # cyan   – selected cutting edge
_C_PREVIEW  = QColor(255, 140,   0, 220)   # orange – hover: segment under cursor
_C_SELECTED = QColor(220,   0,   0, 255)   # red    – segment marked for trim
_C_HOVER    = QColor(255, 200,   0, 180)   # yellow – hover over line in SELECT phase

_ST_SELECT = 0
_ST_TRIM   = 1

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

_HINT = {
    _ST_SELECT: "Click cutting edges",
    _ST_TRIM:   "Click segment to trim",
}


class TrimTool(QgsMapTool):
    """AutoCAD-style TRIM tool — multi-select segments then confirm all at once."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None   # injected by UgsurvMaptool.set_tool()

        self._state           = _ST_SELECT
        self._cutting_edges   = []   # list of (layer, fid)
        self._cutting_bands   = []   # QgsRubberBand per cutting edge
        self._modified_layers = set()

        # Hover feedback
        self._preview_band = self._make_band(_C_PREVIEW, width=3, dashed=True)
        self._preview_band.setVisible(False)
        self._hover_band   = self._make_band(_C_HOVER,   width=3, dashed=True)
        self._hover_band.setVisible(False)

        # Multi-selection: parallel lists of pending trims and their red bands
        # Each entry: (layer, fid, orig_geom, boundaries, trim_idx)
        self._pending_trims = []
        self._selected_bands = []

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

    # ------------------------------------------------------------------
    # Internal helpers
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
        """Return (layer, QgsFeature) for the nearest line within hit tolerance."""
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

    # ------------------------------------------------------------------
    # Geometry: intersection distances + sub-line extraction
    # ------------------------------------------------------------------

    def _intersection_dists(self, line_geom, cutting_geoms):
        """Return sorted distances along line_geom where cutting_geoms cross it."""
        dists = set()
        for cg in cutting_geoms:
            inter = line_geom.intersection(cg)
            if inter is None or inter.isEmpty():
                continue
            gt = QgsWkbTypes.geometryType(inter.wkbType())
            pts = []
            if gt == QgsWkbTypes.PointGeometry:
                if inter.isMultipart():
                    pts = [QgsPointXY(p.x(), p.y()) for p in inter.asMultiPoint()]
                else:
                    p = inter.asPoint()
                    pts = [QgsPointXY(p.x(), p.y())]
            elif gt == QgsWkbTypes.LineGeometry:
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
        """Extract sub-linestring from geom between distances d_from and d_to."""
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
        """
        Return (trim_idx, boundaries) for the interval containing click_pt,
        or (None, None) if no intersection with cutting edges exists.
        """
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

    # ------------------------------------------------------------------
    # Cutting-edge management
    # ------------------------------------------------------------------

    def _toggle_cutting_edge(self, layer, feat):
        key  = (id(layer), feat.id())
        keys = [(id(l), fid) for l, fid in self._cutting_edges]
        if key in keys:
            idx = keys.index(key)
            self._cutting_edges.pop(idx)
            self._rm(self._cutting_bands.pop(idx))
            self._log(f"\nDeselected: '{layer.name()}' fid {feat.id()}")
        else:
            self._cutting_edges.append((layer, feat.id()))
            band = self._make_band(_C_EDGE, width=3)
            band.setToGeometry(feat.geometry(), layer)
            self._cutting_bands.append(band)
            self._log(f"\nCutting edge: '{layer.name()}' fid {feat.id()}")

    def _cutting_geoms_for(self, exclude_layer=None, exclude_fid=None):
        geoms = []
        for lyr, fid in self._cutting_edges:
            if lyr is exclude_layer and fid == exclude_fid:
                continue
            f = lyr.getFeature(fid)
            if f.isValid() and not f.geometry().isEmpty():
                geoms.append(f.geometry())
        return geoms

    # ------------------------------------------------------------------
    # Multi-selection: mark / deselect / confirm all
    # ------------------------------------------------------------------

    def _pending_key(self, layer, fid, trim_idx):
        return (id(layer), fid, trim_idx)

    def _toggle_mark(self, layer, feat, click_pt):
        """
        Mark the segment under click_pt for trimming (adds a red band).
        If that exact segment is already marked, deselect it instead.
        """
        line_geom = feat.geometry()
        if line_geom.isEmpty() or line_geom.isMultipart():
            self._log("\nMultipart geometry — trim not supported (use single-part lines)")
            return

        cutting = self._cutting_geoms_for(exclude_layer=layer, exclude_fid=feat.id())
        if not cutting:
            self._log("\nNo usable cutting edges for this line")
            return

        trim_idx, boundaries = self._trim_interval(line_geom, cutting, click_pt)
        if trim_idx is None:
            self._log("\nNo intersection with cutting edges found on this line")
            return

        key = self._pending_key(layer, feat.id(), trim_idx)
        existing_keys = [
            self._pending_key(l, fid, ti)
            for l, fid, _, _, ti in self._pending_trims
        ]

        if key in existing_keys:
            # Toggle off — remove this mark
            idx = existing_keys.index(key)
            self._pending_trims.pop(idx)
            self._rm(self._selected_bands.pop(idx))
            n = len(self._pending_trims)
            self._log(f"\nDeselected segment  ({n} marked)")
        else:
            # Add new mark
            d_a, d_b = boundaries[trim_idx], boundaries[trim_idx + 1]
            sub = self._sub_line(line_geom, d_a, d_b)
            if sub is None:
                return
            self._pending_trims.append((layer, feat.id(), line_geom, boundaries, trim_idx))
            band = self._make_band(_C_SELECTED, width=4)
            band.setToGeometry(sub, layer)
            self._selected_bands.append(band)
            n = len(self._pending_trims)
            self._log(
                f"\nMarked  {d_b - d_a:.3f} units  on '{layer.name()}'"
                f"  ({n} segment{'s' if n > 1 else ''} selected)"
            )

        self._preview_band.setVisible(False)

    def _confirm_all_trims(self):
        """Execute every pending marked trim, then clear the selection.

        Trims on the same feature are merged into a single geometry operation
        so that multiple selected segments on one line are all removed at once.
        """
        if not self._pending_trims:
            return

        # Group by (layer object id, feature id) so same-feature trims are merged.
        groups = {}
        for entry in self._pending_trims:
            layer, fid = entry[0], entry[1]
            key = (id(layer), fid)
            groups.setdefault(key, []).append(entry)

        total_segments = 0
        for (layer_id, fid), entries in groups.items():
            layer     = entries[0][0]
            orig_geom = entries[0][2]   # geometry captured at first-click for this feature
            feat      = layer.getFeature(fid)
            if not feat.isValid():
                continue

            # Build the union of all boundary sets and the set of intervals to remove.
            all_bounds_set = set()
            remove_set     = set()
            for _, _, _, boundaries, trim_idx in entries:
                for b in boundaries:
                    all_bounds_set.add(round(b, 8))
                d_a = round(boundaries[trim_idx],     8)
                d_b = round(boundaries[trim_idx + 1], 8)
                remove_set.add((d_a, d_b))

            all_bounds = sorted(all_bounds_set)

            # Keep every interval NOT in the remove set.
            remaining = []
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

        self._log(f"\nTrimmed {total_segments} segment(s)")

    def _clear_selection(self):
        """Discard all pending marks without trimming."""
        for band in self._selected_bands:
            self._rm(band)
        self._selected_bands = []
        self._pending_trims  = []

    # ------------------------------------------------------------------
    # Hover preview
    # ------------------------------------------------------------------

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

        # Trim phase — show the segment under the cursor in orange
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

    # ------------------------------------------------------------------
    # Phase transitions
    # ------------------------------------------------------------------

    def _advance_to_trim(self):
        if not self._cutting_edges:
            for lyr in self._line_layers():
                for feat in lyr.getFeatures():
                    if feat.geometry().isEmpty():
                        continue
                    self._cutting_edges.append((lyr, feat.id()))
                    band = self._make_band(_C_EDGE, width=2)
                    band.setToGeometry(feat.geometry(), lyr)
                    self._cutting_bands.append(band)
            self._log(f"\nAll lines as cutting edges ({len(self._cutting_edges)} features)")
        else:
            self._log(f"\n{len(self._cutting_edges)} cutting edge(s) confirmed")

        self._state = _ST_TRIM
        self._log(
            "\nTRIM: click segments to mark for removal (click again to deselect)"
            "\n  Enter / RMB → trim all marked  |  Esc → cancel\n"
        )

    def _finish(self):
        self._confirm_all_trims()
        for lyr in self._modified_layers:
            try:
                lyr.commitChanges()
            except Exception:
                pass
        if self._modified_layers:
            self._log(f"\nCommitted changes to {len(self._modified_layers)} layer(s)")
        self.deactivate()

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._log(
            "\nTRIM  ──  Select cutting edges:"
            "\n  Click boundary lines  (click again to deselect)"
            "\n  Enter / RMB with no selection  →  use ALL lines as cutting edges"
            "\n  Enter / RMB after selection    →  start trimming"
            "\n  Esc → cancel\n"
        )

    def deactivate(self):
        for band in self._cutting_bands:
            self._rm(band)
        self._cutting_bands = []
        self._cutting_edges = []

        self._clear_selection()
        self._rm(self._preview_band)
        self._rm(self._hover_band)

        self._state = _ST_SELECT
        self._modified_layers.clear()
        self._hint.hide()

        if self._maptool:
            self._maptool.clear_tool()

        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        self._update_preview(self.toMapCoordinates(event.pos()))
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())

        if event.button() == Qt.RightButton:
            if self._state == _ST_SELECT:
                self._advance_to_trim()
            else:
                self._finish()
            return

        if event.button() != Qt.LeftButton:
            return

        if self._state == _ST_SELECT:
            layer, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._toggle_cutting_edge(layer, feat)
            else:
                self._log("\nNo line found near click — click directly on a line")

        elif self._state == _ST_TRIM:
            layer, feat = self._find_line_near(map_pt)
            if feat is not None:
                self._toggle_mark(layer, feat, map_pt)
            else:
                self._log("\nNo line found near click")

        self.canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self._state == _ST_SELECT:
                self._advance_to_trim()
            else:
                self._finish()
