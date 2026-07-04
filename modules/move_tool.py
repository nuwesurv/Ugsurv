"""
AutoCAD-style MOVE tool.

Workflow
────────
1. Click any feature         → highlighted green                  (_ST_SELECT → _ST_BASE)
2. Click base point          → snaps to nearest vertex            (_ST_BASE   → _ST_PLACE)
3. Move cursor               → live orange dashed preview + snap indicator
4. Click destination         → feature translated by (dest − base); resets to idle
   Right-click / Escape      → cancel back to idle
   Enter / Space             → exit tool

Shortcut: if a feature is already highlighted in the vertex selector when
this tool activates (typing 'm' while standing on a parcel), step 1 is
skipped and the tool opens directly at step 2.
"""

from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator
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
import math


_C_HIGHLIGHT = QColor(0, 200, 80, 220)
_C_HL_FILL   = QColor(0, 200, 80, 20)
_C_PREVIEW   = QColor(255, 130, 0, 220)
_C_PREV_FILL = QColor(255, 130, 0, 30)

_ST_SELECT = 0   # waiting for feature click
_ST_BASE   = 1   # feature highlighted, waiting for base point
_ST_PLACE  = 2   # base set, live preview, waiting for destination

_HIT_PX = 10

_HINT = {
    _ST_SELECT: "Click a feature to select",
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
    """Move entire features — AutoCAD-style select → base → destination."""

    def __init__(self, canvas, terminal_dock, preselect=None):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None
        self._preselect    = preselect

        self._state     = _ST_SELECT
        self._sel_layer = None
        self._sel_fid   = None
        self._sel_geom  = None
        self._base_pt   = None
        self._snap_pt   = None

        self._hl_band      = None
        self._preview_band = None
        self._snap_ind     = None   # QgsSnapIndicator — created fresh on activate

        # floating hint label parented to the canvas widget
        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        # floating dynamic input: dx + dy displacement after base point is set
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
        """Return (layer, fid, geom_copy) for the nearest feature within hit tolerance."""
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

    def _snap(self, screen_pos):
        """Return snapped QgsPointXY using the project's snapping utils."""
        match = self.canvas.snappingUtils().snapToMap(screen_pos)
        if match.isValid():
            if self._snap_ind:
                self._snap_ind.setMatch(match)
            return match.point()
        if self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        return self.toMapCoordinates(screen_pos)

    def _show_hint(self, screen_pos):
        """Position and display the state-appropriate hint near the cursor."""
        text = _HINT.get(self._state, "")
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        # nudge back inside canvas if it would overflow
        cw = self.canvas.width()
        ch = self.canvas.height()
        hw = self._hint.width()
        hh = self._hint.height()
        if pos.x() + hw > cw:
            pos.setX(screen_pos.x() - hw - 4)
        if pos.y() + hh > ch:
            pos.setY(screen_pos.y() - hh - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    def _clear_bands(self):
        self._rm(self._hl_band)
        self._hl_band = None
        self._rm(self._preview_band)
        self._preview_band = None

    def _reset(self):
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._clear_bands()
        if self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        self._state     = _ST_SELECT
        self._sel_layer = None
        self._sel_fid   = None
        self._sel_geom  = None
        self._base_pt   = None
        self._snap_pt   = None

    # ------------------------------------------------------------------
    # DynamicInput callbacks
    # ------------------------------------------------------------------

    def _on_displacement_committed(self, values: dict):
        """Called when user presses Enter / Space in the floating dx/dy widget."""
        self.terminal_dock.clear_input_handler()
        self._apply_displacement(values["dx"], values["dy"])

    def _on_displacement_terminal(self, text: str):
        """Called when user types in the terminal and presses Enter."""
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
            self._log(f"\nInvalid displacement — enter dx [Tab] dy")
            return
        dest = QgsPointXY(self._base_pt.x() + dx, self._base_pt.y() + dy)
        self._apply_move(dest)

    def _cancel_dinput(self):
        self._reset()
        self._log("\nMove cancelled")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _enter_base(self, layer, fid, geom):
        self._clear_bands()
        self._sel_layer = layer
        self._sel_fid   = fid
        self._sel_geom  = geom
        self._state     = _ST_BASE

        gt = QgsWkbTypes.geometryType(geom.wkbType())
        self._hl_band = self._make_band(gt, _C_HIGHLIGHT, _C_HL_FILL, width=2)
        self._hl_band.setToGeometry(geom, layer)
        self._preview_band = self._make_band(gt, _C_PREVIEW, _C_PREV_FILL, width=2, dashed=True)
        self._preview_band.setVisible(False)

        self._log(
            f"\nSelected '{layer.name()}' fid {fid}"
            f"  →  click base point  |  Esc / RMB to cancel"
        )

    def _enter_place(self, base_pt):
        self._base_pt = base_pt
        self._state   = _ST_PLACE
        self._preview_band.setVisible(True)
        self._log("\nClick destination  or  type dx,dy + Enter  |  Esc / RMB to cancel")
        cp = self.canvas.getCoordinateTransform().transform(base_pt)
        self._dinput.on_commit = self._on_displacement_committed
        self.terminal_dock.request_input("dx,dy: ", self._on_displacement_terminal)
        self._dinput.show(cp.x(), cp.y())

    def _update_preview(self, snap_pt):
        if self._state == _ST_PLACE:
            dx = snap_pt.x() - self._base_pt.x()
            dy = snap_pt.y() - self._base_pt.y()
            moved = QgsGeometry(self._sel_geom)
            moved.translate(dx, dy)
            self._preview_band.setToGeometry(moved, self._sel_layer)

    def _commit(self, screen_pos):
        snap_pt = self._snap(screen_pos)
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._apply_move(snap_pt)

    def _apply_move(self, dest_pt: QgsPointXY):
        dx = dest_pt.x() - self._base_pt.x()
        dy = dest_pt.y() - self._base_pt.y()
        lyr, fid = self._sel_layer, self._sel_fid
        new_geom = QgsGeometry(self._sel_geom)
        new_geom.translate(dx, dy)
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.changeGeometry(fid, new_geom)
        lyr.triggerRepaint()
        self._log(f"\nMoved  Δ({dx:.3f}, {dy:.3f})  →  '{lyr.name()}' fid {fid}")
        self._reset()

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()

        self._snap_ind = QgsSnapIndicator(self.canvas)

        if self._preselect:
            layer, fid = self._preselect
            self._preselect = None
            feat = layer.getFeature(fid)
            if feat.isValid() and not feat.geometry().isEmpty():
                self._enter_base(layer, fid, QgsGeometry(feat.geometry()))
                return

        self._log(
            "\nMOVE  ──  click a feature to select it, then click base point, then destination"
            "\n  Esc / RMB → cancel  |  Enter → exit\n"
        )

    def deactivate(self):
        self._dinput.destroy()
        self.terminal_dock.clear_input_handler()
        self._clear_bands()
        self._snap_ind = None   # QgsSnapIndicator cleans up its own canvas item
        self._hint.hide()
        self._state = _ST_SELECT
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
        elif self._snap_ind:
            self._snap_ind.setMatch(QgsPointLocator.Match())
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())

        if event.button() == Qt.RightButton:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("\nMove cancelled")
            else:
                self._hint.hide()
                self.deactivate()
            return

        if event.button() != Qt.LeftButton:
            return

        if self._state == _ST_SELECT:
            result = self._find_feature_near(map_pt)
            if result:
                self._enter_base(*result)
            else:
                self._log("\nNo feature found near click")

        elif self._state == _ST_BASE:
            snap_pt = self._snap(event.pos())
            self._enter_place(snap_pt)

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
            self._hint.hide()
            self.deactivate()

    def mouseDoubleClickEvent(self, event):
        self.terminal_dock.command.setFocus()
