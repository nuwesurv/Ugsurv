"""
AutoCAD-style JOIN tool.

Click polylines to add them to the join set (highlighted green).
Click a highlighted feature again to deselect it.
Enter / RMB → validate that all selected lines share endpoints, then join
              them into one single LineString and delete the originals.
Esc → cancel.

Contact-point rule
──────────────────
Every consecutive pair of segments in the final chain must share an endpoint
within _TOUCH_TOL map units.  Any selection where a valid chain cannot be
found (i.e. some polyline doesn't touch its neighbour) is rejected with
"polylines not touching" — no geometry is modified.

Chaining uses backtracking over all orderings and per-segment reversals
(feasible for the typical 2–6 features used in practice).
"""

from itertools import permutations

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
from . import snap_utils

_C_SEL   = QColor(  0, 200,  80, 220)   # green  – selected feature
_C_HOVER = QColor(255, 200,   0, 200)   # yellow – hovered, not yet selected

_HIT_PX   = 10
_TOUCH_TOL = 1e-3   # map units; endpoints within this distance are considered coincident


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


class JoinTool(QgsMapTool):
    """Click polylines to build a join set; Enter chains them into one."""

    def __init__(self, canvas, terminal_dock, preselect=None):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None   # injected by UgsurvMaptool.set_tool()
        self._preselect    = preselect

        self._selected  = []    # ordered list of (layer, fid)
        self._sel_bands = {}    # (id(layer), fid) → QgsRubberBand (green)
        self._hover_band = None
        self._hover_key  = None

        snap_utils.init_snap()

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

    # ------------------------------------------------------------------
    # Logging / hints
    # ------------------------------------------------------------------

    def _log(self, msg):
        self.terminal_dock.commandOutputText += msg
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    def _show_hint(self, screen_pos, text):
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

    # ------------------------------------------------------------------
    # Rubber-band helpers
    # ------------------------------------------------------------------

    def _make_band(self, geom, color, width=3):
        gt = (QgsWkbTypes.geometryType(geom.wkbType())
              if not geom.isEmpty() else QgsWkbTypes.LineGeometry)
        band = QgsRubberBand(self.canvas, gt)
        band.setColor(color)
        band.setWidth(width)
        return band

    def _rm_band(self, band):
        if band is not None:
            try:
                self.canvas.scene().removeItem(band)
            except Exception:
                pass

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

    # ------------------------------------------------------------------
    # Spatial lookup
    # ------------------------------------------------------------------

    def _find_line_near(self, map_pt):
        tol     = _HIT_PX * self.canvas.mapUnitsPerPixel()
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        rect    = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        best, best_d = None, tol
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial():
                continue
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
                    continue
                d = geom.distance(pt_geom)
                if d < best_d:
                    best_d = d
                    best = (lyr, feat.id())
        return best

    # ------------------------------------------------------------------
    # Selection toggle
    # ------------------------------------------------------------------

    def _is_selected(self, layer, fid):
        return (id(layer), fid) in self._sel_bands

    def _select(self, layer, fid):
        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if geom.isEmpty():
            return
        band = self._make_band(geom, _C_SEL, width=3)
        band.setToGeometry(geom, layer)
        self._sel_bands[(id(layer), fid)] = band
        self._selected.append((layer, fid))
        self._log(f"\nSelected feature {fid} of '{layer.name()}'  [{len(self._selected)} total]")

    def _deselect(self, layer, fid):
        self._rm_band(self._sel_bands.pop((id(layer), fid), None))
        self._selected = [(l, f) for l, f in self._selected
                          if not (id(l) == id(layer) and f == fid)]
        self._log(f"\nDeselected feature {fid}  [{len(self._selected)} total]")

    def _toggle(self, layer, fid):
        if self._is_selected(layer, fid):
            self._deselect(layer, fid)
        else:
            self._select(layer, fid)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _geom_points(self, geom):
        """Return all vertices of a LineGeometry as a list of QgsPointXY."""
        pts = []
        it  = geom.vertices()
        while it.hasNext():
            v = it.next()
            pts.append(QgsPointXY(v.x(), v.y()))
        return pts

    def _touch(self, p1, p2):
        """True when two points are within _TOUCH_TOL of each other."""
        return p1.distance(p2) <= _TOUCH_TOL

    # ------------------------------------------------------------------
    # Chaining with contact-point validation
    # ------------------------------------------------------------------

    def _try_order(self, segments, order):
        """
        Attempt to chain segments in `order` (a permutation of indices).
        Each segment may be used forward or reversed.
        Returns merged [QgsPointXY, ...] if every consecutive pair shares
        an endpoint within _TOUCH_TOL, otherwise returns None.
        """
        first_pts = segments[order[0]][2]

        for start_reversed in (False, True):
            chain = list(reversed(first_pts)) if start_reversed else list(first_pts)
            ok = True

            for k in range(1, len(order)):
                pts  = segments[order[k]][2]
                tail = chain[-1]

                if self._touch(tail, pts[0]):
                    # Forward: drop the shared endpoint to avoid a duplicate
                    chain.extend(pts[1:])
                elif self._touch(tail, pts[-1]):
                    # Reversed: same
                    chain.extend(list(reversed(pts))[1:])
                else:
                    ok = False
                    break

            if ok:
                return chain

        return None

    def _chain_or_reject(self, segments):
        """
        Try every ordering and per-segment reversal to find a valid chain
        where every consecutive pair shares an endpoint within _TOUCH_TOL.

        Returns (pts_list, None) on success or (None, error_message) on failure.
        Practical limit: works comfortably up to ~7 segments (5040 permutations).
        """
        for order in permutations(range(len(segments))):
            result = self._try_order(segments, order)
            if result is not None:
                return result, None

        return None, "polylines not touching"

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def _join_and_commit(self):
        if len(self._selected) < 2:
            self._log("\nJOIN: select at least 2 polylines first")
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
            self._log("\nJOIN: not enough valid geometries")
            return

        chained, err = self._chain_or_reject(segments)
        if err:
            self._log(f"\nJOIN: {err}")
            return   # leave selection intact so user can adjust

        new_geom = QgsGeometry.fromPolylineXY(chained)
        attrs    = polyline_attrs(new_geom)

        # Write result to the first selected feature's layer
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

        # Delete originals (may span multiple layers)
        for lyr, fid, _ in segments:
            if not lyr.isEditable():
                lyr.startEditing()
            lyr.deleteFeature(fid)

        # Repaint each affected layer once
        seen = set()
        for lyr, _, _ in segments:
            lid = id(lyr)
            if lid not in seen:
                lyr.updateExtents()
                lyr.triggerRepaint()
                seen.add(lid)

        n = len(segments)
        self._log(
            f"\nJoined {n} polylines → 1 feature  "
            f"({len(chained)} vertices, length: {length:.3f})"
            + (f", area: {area_sqm:.3f} sqm ({area_acres:.4f} acres)" if is_closed else "")
        )

        self._clear_all()
        self._log("\nJOIN: click polylines to select, Enter to join\n")

    # ------------------------------------------------------------------
    # QgsMapTool overrides
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()

        if self._preselect:
            items = self._preselect
            self._preselect = None
            for layer, fid in items:
                feat = layer.getFeature(fid)
                geom = feat.geometry()
                if (geom.isEmpty()
                        or QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry):
                    continue
                if not self._is_selected(layer, fid):
                    self._select(layer, fid)
            if len(self._selected) >= 2:
                self._join_and_commit()
                return
            elif len(self._selected) == 1:
                self._log("\nJOIN: 1 line preloaded — click more polylines, then Enter to join\n")
                return

        self._log("\nJOIN: click polylines to select, Enter to join\n")

    def deactivate(self):
        self._clear_all()
        self._hint.hide()
        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
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
                        band = self._make_band(geom, _C_HOVER, width=2)
                        band.setToGeometry(geom, lyr)
                        self._hover_band = band
                        self._hover_key  = key
            else:
                self._clear_hover()
        else:
            self._clear_hover()

        n = len(self._selected)
        if n >= 2:
            self._show_hint(event.pos(), f"{n} selected — Enter to join")
        elif n == 1:
            self._show_hint(event.pos(), "Select 1 more polyline, then Enter")
        else:
            self._show_hint(event.pos(), "Click polylines to select")

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._join_and_commit()
            return
        if event.button() != Qt.LeftButton:
            return
        raw_pt = self.toMapCoordinates(event.pos())
        hit    = self._find_line_near(raw_pt)
        if hit:
            self._clear_hover()
            self._toggle(*hit)
        self.canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self._join_and_commit()
