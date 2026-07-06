"""
AutoCAD-style MOVE tool.

Workflow
────────
1. Click features to build a selection set.
   Shift+click removes a feature from the set.
   Enter / Space / RMB (with selection) → confirm and proceed.

2. Click base point  → snaps to nearest vertex.

3. Move cursor       → live dashed preview of ALL selected features.
   Click destination → all features translated by (dest − base); tool exits.

   Esc             → cancel / exit tool at any phase.
   RMB in phases 2-3 → cancel back to idle (keeps tool open for re-select).

Shortcut: if a feature is already highlighted in the vertex selector when
this tool activates (typing 'm' while standing on a parcel), step 1 is
skipped and the tool opens directly at step 2.
"""

from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsGeometry,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from .dynamic_input import DynamicInput


_C_HIGHLIGHT = QColor(0, 200, 80, 220)
_C_HL_FILL   = QColor(0, 200, 80, 20)
_C_PREVIEW   = QColor(255, 130, 0, 220)
_C_PREV_FILL = QColor(255, 130, 0, 30)

_ST_SELECT = 0   # accumulate features; Enter/RMB confirms
_ST_BASE   = 1   # click base point
_ST_PLACE  = 2   # click destination, live preview

_HIT_PX = 10

_HINT = {
    _ST_SELECT: "Click features  (Shift+click = deselect  |  Enter = confirm)",
    _ST_BASE:   "Click base point",
    _ST_PLACE:  "Click destination",
}

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


class MoveTool(QgsMapTool):
    """Move features — AutoCAD-style multi-select → base point → destination."""

    def __init__(self, canvas, terminal_dock, preselect=None):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None
        self._preselect    = preselect

        self._state        = _ST_SELECT
        self._sel_features = []   # list of (layer, fid, geom_copy)
        self._sel_bands    = []   # highlight band per selected feature
        self._prev_bands   = []   # preview band per selected feature
        self._base_pt      = None
        self._snap_pt      = None
        self._snap_ind     = None
        self._cc_cross     = None   # circle-center snap icon: "+" marker

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        self._dinput = DynamicInput(canvas, terminal_dock, [
            {"key": "dx", "label": "ΔX"},
            {"key": "dy", "label": "ΔY"},
        ])
        self._dinput.on_cancel = self._cancel_dinput

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_band(self, geom_type, color, fill_color, width=2, dashed=False):
        band = QgsRubberBand(self.canvas, geom_type)
        band.setColor(color)
        band.setFillColor(fill_color)
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
                    best = (lyr, feat.id(), QgsGeometry(geom))
        return best

    def _circle_center_from_geom(self, geom):
        """Return center QgsPointXY of a 5-point circle geometry, or None."""
        pts, it = [], geom.vertices()
        while it.hasNext() and len(pts) < 3:
            p = it.next()
            pts.append(QgsPointXY(p.x(), p.y()))
        if len(pts) < 3:
            return None
        return QgsPointXY((pts[0].x() + pts[2].x()) / 2, (pts[0].y() + pts[2].y()) / 2)

    def _find_circle_center_near(self, map_pt):
        """Return (layer, fid, center_pt) of the nearest _circles center within tolerance."""
        tol  = self._hit_tol()
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        best_lyr, best_fid, best_center, best_dist = None, None, None, tol
        for lyr in self._vector_layers():
            if lyr.name() != "_circles":
                continue
            for feat in lyr.getFeatures(rect):
                center = self._circle_center_from_geom(feat.geometry())
                if center is None:
                    continue
                dist = map_pt.distance(center)
                if dist < best_dist:
                    best_dist, best_lyr, best_fid, best_center = dist, lyr, feat.id(), center
        if best_lyr is None:
            return None
        return (best_lyr, best_fid, best_center)

    def _snap(self, screen_pos):
        map_pt = self.toMapCoordinates(screen_pos)
        # Circle centers are not real vertices — check them before native snap
        center_hit = self._find_circle_center_near(map_pt)
        if center_hit:
            _, _, center = center_hit
            if self._snap_ind:
                self._snap_ind.setMatch(QgsPointLocator.Match())  # hide native indicator
            if self._cc_cross:
                self._cc_cross.setCenter(center)
                self._cc_cross.setVisible(True)
            return center
        # No circle center — hide custom marker, use native snap
        if self._cc_cross:
            self._cc_cross.setVisible(False)
        match = self.canvas.snappingUtils().snapToMap(screen_pos)
        if match.isValid():
            if self._snap_ind:
                self._snap_ind.setMatch(match)
            return match.point()
        if self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        return map_pt

    def _show_hint(self, screen_pos):
        text = _HINT.get(self._state, "")
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        cw, ch = self.canvas.width(), self.canvas.height()
        hw, hh = self._hint.width(), self._hint.height()
        if pos.x() + hw > cw:
            pos.setX(screen_pos.x() - hw - 4)
        if pos.y() + hh > ch:
            pos.setY(screen_pos.y() - hh - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    # ------------------------------------------------------------------
    # Selection management
    # ------------------------------------------------------------------

    def _sel_key(self, layer, fid):
        return (id(layer), fid)

    def _existing_keys(self):
        return [self._sel_key(l, f) for l, f, _ in self._sel_features]

    def _add_to_selection(self, layer, fid, geom):
        if self._sel_key(layer, fid) in self._existing_keys():
            return False
        self._sel_features.append((layer, fid, QgsGeometry(geom)))
        gt = QgsWkbTypes.geometryType(geom.wkbType())
        hl = self._make_band(gt, _C_HIGHLIGHT, _C_HL_FILL, width=2)
        hl.setToGeometry(geom, layer)
        self._sel_bands.append(hl)
        prev = self._make_band(gt, _C_PREVIEW, _C_PREV_FILL, width=2, dashed=True)
        prev.setVisible(False)
        self._prev_bands.append(prev)
        return True

    def _remove_from_selection(self, layer, fid):
        key  = self._sel_key(layer, fid)
        keys = self._existing_keys()
        if key not in keys:
            return False
        idx = keys.index(key)
        self._sel_features.pop(idx)
        self._rm(self._sel_bands.pop(idx))
        self._rm(self._prev_bands.pop(idx))
        return True

    def _clear_selection(self):
        for b in self._sel_bands:
            self._rm(b)
        for b in self._prev_bands:
            self._rm(b)
        self._sel_features = []
        self._sel_bands    = []
        self._prev_bands   = []

    # ------------------------------------------------------------------
    # DynamicInput callbacks
    # ------------------------------------------------------------------

    def _on_displacement_committed(self, values: dict):
        self.terminal_dock.clear_input_handler()
        self._apply_displacement(values["dx"], values["dy"])

    def _on_displacement_terminal(self, text: str):
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        parts = text.strip().replace(',', ' ').split()
        if len(parts) == 1:
            self._apply_displacement(parts[0], "0")
        elif len(parts) >= 2:
            self._apply_displacement(parts[0], parts[1])
        else:
            self._log("\nInvalid input — use: dx  or  dx,dy")

    def _apply_displacement(self, dx_text: str, dy_text: str):
        try:
            dx = float(dx_text)
            dy = float(dy_text)
        except ValueError:
            self._log("\nInvalid displacement — enter dx [Tab] dy")
            return
        dest = QgsPointXY(self._base_pt.x() + dx, self._base_pt.y() + dy)
        self._apply_move(dest)

    def _cancel_dinput(self):
        # Esc in DynamicInput: keep selection, go back to base-point click
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        for b in self._prev_bands:
            b.setVisible(False)
        if self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        self._state   = _ST_BASE
        self._base_pt = None
        self._snap_pt = None
        n = len(self._sel_features)
        self._log(f"\n{n} feature(s) still selected  →  click base point")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _enter_base(self):
        for b in self._prev_bands:
            b.setVisible(False)
        self._state = _ST_BASE
        n = len(self._sel_features)
        self._log(
            f"\n{n} feature(s) selected"
            "  →  click base point  |  Esc to cancel"
        )

    def _enter_place(self, base_pt):
        self._base_pt = base_pt
        self._state   = _ST_PLACE
        for b in self._prev_bands:
            b.setVisible(True)
        self._log("\nClick destination  or  type dx,dy + Enter  |  Esc to cancel")
        cp = self.canvas.getCoordinateTransform().transform(base_pt)
        self._dinput.on_commit = self._on_displacement_committed
        self.terminal_dock.request_input("dx,dy: ", self._on_displacement_terminal)
        self._dinput.show(cp.x(), cp.y())

    def _update_preview(self, snap_pt):
        if self._state == _ST_PLACE and self._base_pt:
            dx = snap_pt.x() - self._base_pt.x()
            dy = snap_pt.y() - self._base_pt.y()
            for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
                moved = QgsGeometry(geom)
                moved.translate(dx, dy)
                band.setToGeometry(moved, layer)

    def _commit(self, screen_pos):
        snap_pt = self._snap(screen_pos)
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._apply_move(snap_pt)

    def _apply_move(self, dest_pt: QgsPointXY):
        dx = dest_pt.x() - self._base_pt.x()
        dy = dest_pt.y() - self._base_pt.y()
        modified = set()
        for layer, fid, geom in self._sel_features:
            new_geom = QgsGeometry(geom)
            new_geom.translate(dx, dy)
            if not layer.isEditable():
                layer.startEditing()
            layer.changeGeometry(fid, new_geom)
            modified.add(layer)
        for lyr in modified:
            lyr.triggerRepaint()
        n = len(self._sel_features)
        self._log(f"\nMoved {n} feature(s)  Δ({dx:.3f}, {dy:.3f})")
        self.deactivate()

    def _reset(self):
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._clear_selection()
        if self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        self._state   = _ST_SELECT
        self._base_pt = None
        self._snap_pt = None

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def _make_cc_markers(self):
        m = QgsVertexMarker(self.canvas)
        m.setColor(QColor(66, 135, 245))
        m.setIconType(QgsVertexMarker.ICON_CROSS)
        m.setIconSize(14)
        m.setPenWidth(2)
        m.setVisible(False)
        self._cc_cross = m

    def _rm_cc_markers(self):
        if self._cc_cross is not None:
            try:
                self.canvas.scene().removeItem(self._cc_cross)
            except Exception:
                pass
        self._cc_cross = None

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()
        self._snap_ind = QgsSnapIndicator(self.canvas)
        self._make_cc_markers()

        if self._preselect:
            items = self._preselect
            self._preselect = None
            if isinstance(items, list):
                for layer, fid in items:
                    feat = layer.getFeature(fid)
                    if feat.isValid() and not feat.geometry().isEmpty():
                        self._add_to_selection(layer, fid, feat.geometry())
            else:
                layer, fid = items
                feat = layer.getFeature(fid)
                if feat.isValid() and not feat.geometry().isEmpty():
                    self._add_to_selection(layer, fid, feat.geometry())
            if self._sel_features:
                self._enter_base()
                return

        self._log(
            "\nMOVE  ──  click features to select"
            "  (Shift+click to deselect)"
            "\n  Enter / Space / RMB → confirm selection, then click base point, then destination"
            "\n  Esc → cancel\n"
        )

    def deactivate(self):
        self._dinput.destroy()
        self.terminal_dock.clear_input_handler()
        self._clear_selection()
        self._snap_ind = None
        self._rm_cc_markers()
        self._hint.hide()
        self._state   = _ST_SELECT
        self._base_pt = None
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        if self._state in (_ST_BASE, _ST_PLACE):
            snap_pt = self._snap(event.pos())
            self._snap_pt = snap_pt
            self._update_preview(snap_pt)
            if self._state == _ST_PLACE and self._base_pt:
                dx = snap_pt.x() - self._base_pt.x()
                dy = snap_pt.y() - self._base_pt.y()
                cp = self.canvas.getCoordinateTransform().transform(snap_pt)
                self._dinput.update(cp.x(), cp.y(), {
                    "dx": f"{dx:.3f}",
                    "dy": f"{dy:.3f}",
                })
        else:
            if self._snap_ind:
                self._snap_ind.setMatch(QgsPointLocator.Match())
            if self._cc_cross:
                self._cc_cross.setVisible(False)
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        shift  = bool(event.modifiers() & Qt.ShiftModifier)

        if event.button() == Qt.RightButton:
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._hint.hide()
                    self.deactivate()
            else:
                self._reset()
                self._log("\nMove cancelled")
            return

        if event.button() != Qt.LeftButton:
            return

        if self._state == _ST_SELECT:
            result = self._find_feature_near(map_pt)
            if result:
                layer, fid, geom = result
                if shift:
                    if self._remove_from_selection(layer, fid):
                        self._log(f"\nDeselected  ({len(self._sel_features)} selected)")
                    else:
                        self._log("\nNot in selection")
                else:
                    if self._add_to_selection(layer, fid, geom):
                        self._log(
                            f"\nSelected '{layer.name()}' fid {fid}"
                            f"  ({len(self._sel_features)} selected)"
                        )
                    else:
                        self._log(
                            f"\nAlready selected"
                            f"  ({len(self._sel_features)} selected)"
                            "  — Shift+click to deselect"
                        )
            else:
                self._log("\nNo feature found near click")

        elif self._state == _ST_BASE:
            self._enter_place(self._snap(event.pos()))

        elif self._state == _ST_PLACE:
            self._commit(event.pos())

        self.terminal_dock.command.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("\nMove cancelled")
            else:
                self._hint.hide()
                self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._hint.hide()
                    self.deactivate()

    def mouseDoubleClickEvent(self, event):
        self.terminal_dock.command.setFocus()
