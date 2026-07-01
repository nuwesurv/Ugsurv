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

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)


_C_HIGHLIGHT = QColor(0, 200, 80, 220)
_C_HL_FILL   = QColor(0, 200, 80, 20)
_C_PREVIEW   = QColor(255, 130, 0, 220)
_C_PREV_FILL = QColor(255, 130, 0, 30)
_C_SNAP      = QColor(255, 210, 0, 240)    # yellow – snap indicator box

_ST_SELECT = 0   # waiting for feature click
_ST_BASE   = 1   # feature highlighted, waiting for base point
_ST_PLACE  = 2   # base set, live preview, waiting for destination

_HIT_PX = 10


class MoveTool(QgsMapTool):
    """Move entire features — AutoCAD-style select → base → destination."""

    def __init__(self, canvas, terminal_dock, preselect=None):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None
        self._preselect    = preselect   # (layer, fid) injected before activate()

        self._state     = _ST_SELECT
        self._sel_layer = None
        self._sel_fid   = None
        self._sel_geom  = None   # QgsGeometry snapshot
        self._base_pt   = None   # snapped base point
        self._snap_pt   = None   # current snapped cursor (updated on move)

        self._hl_band      = None
        self._preview_band = None
        self._snap_marker  = None

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

    def _find_snap(self, map_pt):
        """Return (snapped_pt, is_snapped) by checking all vertex positions."""
        tol  = self._hit_tol()
        rect = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                            map_pt.x()+tol, map_pt.y()+tol)
        best, best_d = None, tol
        for lyr in self._vector_layers():
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                it = geom.vertices()
                while it.hasNext():
                    v   = it.next()
                    vpt = QgsPointXY(v.x(), v.y())
                    d   = map_pt.distance(vpt)
                    if d < best_d:
                        best_d = d
                        best   = vpt
        return (best, True) if best else (map_pt, False)

    def _clear_bands(self):
        self._rm(self._hl_band)
        self._hl_band = None
        self._rm(self._preview_band)
        self._preview_band = None

    def _reset(self):
        self._clear_bands()
        if self._snap_marker:
            self._snap_marker.setVisible(False)
        self._state     = _ST_SELECT
        self._sel_layer = None
        self._sel_fid   = None
        self._sel_geom  = None
        self._base_pt   = None
        self._snap_pt   = None

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
        self._log("\nClick destination  |  Esc / RMB to cancel")

    def _update_snap_and_preview(self, map_pt):
        snap_pt, snapped = self._find_snap(map_pt)
        self._snap_pt = snap_pt
        if self._snap_marker:
            self._snap_marker.setCenter(snap_pt)
            self._snap_marker.setVisible(snapped)
        if self._state == _ST_PLACE:
            dx = snap_pt.x() - self._base_pt.x()
            dy = snap_pt.y() - self._base_pt.y()
            moved = QgsGeometry(self._sel_geom)
            moved.translate(dx, dy)
            self._preview_band.setToGeometry(moved, self._sel_layer)

    def _commit(self, map_pt):
        snap_pt, _ = self._find_snap(map_pt)
        dx = snap_pt.x() - self._base_pt.x()
        dy = snap_pt.y() - self._base_pt.y()
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
        self.canvas.setFocus()

        self._snap_marker = QgsVertexMarker(self.canvas)
        self._snap_marker.setColor(_C_SNAP)
        self._snap_marker.setIconType(QgsVertexMarker.ICON_BOX)
        self._snap_marker.setIconSize(12)
        self._snap_marker.setPenWidth(2)
        self._snap_marker.setVisible(False)

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
        self._clear_bands()
        self._rm(self._snap_marker)
        self._snap_marker = None
        self._state = _ST_SELECT
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        if self._state in (_ST_BASE, _ST_PLACE):
            self._update_snap_and_preview(self.toMapCoordinates(event.pos()))

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())

        if event.button() == Qt.RightButton:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("\nMove cancelled")
            else:
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
            snap_pt, _ = self._find_snap(map_pt)
            self._enter_place(snap_pt)

        elif self._state == _ST_PLACE:
            self._commit(map_pt)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("\nMove cancelled")
            else:
                self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.deactivate()
