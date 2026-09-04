# -*- coding: utf-8 -*-
"""
CropTool — clip a raster to a rectangle drawn on the canvas.

Workflow
────────
1. Activate with a pre-selected raster (via SelectTool), or click a raster.
2. Drag to draw the clip rectangle.
3. Release → new clipped GeoTIFF (<name>_crop.tif) saved next to source and
   added to the project.

Esc → cancel.
"""

import contextlib
import os

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint

from ...core import style as _style
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsGeometry, QgsPointXY, QgsProject,
    QgsRasterLayer, QgsRectangle, QgsWkbTypes,
)

_ST_PICK = 0   # waiting for user to click a raster
_ST_RECT = 1   # raster selected; drawing clip rectangle

_HINT = {
    _ST_PICK: "Click a raster to crop",
    _ST_RECT: "Drag to draw clip rectangle",
}

_HINT_STYLE = (
    "QLabel{background:rgba(20,20,20,210);color:#f0f0f0;"
    "border:1px solid rgba(255,255,255,80);border-radius:4px;"
    "padding:3px 8px;font-size:9pt;}"
)


class CropTool(QgsMapTool):
    """Clip a raster to a user-drawn rectangle using GDAL Warp."""

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state:      int                    = _ST_PICK
        self._raster:     QgsRasterLayer | None  = None
        self._drag_start: QgsPointXY | None      = None
        self._drag_rb:    QgsRubberBand | None   = None
        self._extent_rb:  QgsRubberBand | None   = None   # raster extent indicator

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()

    # ── logging / hint ────────────────────────────────────────────────────

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

    # ── helpers ───────────────────────────────────────────────────────────

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    def _find_raster_at(self, map_pt):
        """Return the topmost file-based QgsRasterLayer whose extent contains map_pt."""
        root = QgsProject.instance().layerTreeRoot()
        for lyr in root.layerOrder():
            if not isinstance(lyr, QgsRasterLayer) or not lyr.isValid():
                continue
            if lyr.providerType() != 'gdal':
                continue
            if lyr.extent().contains(map_pt):
                return lyr
        return None

    def _show_extent_band(self, lyr):
        self._rm(self._extent_rb)
        ext = lyr.extent()
        pts = [
            QgsPointXY(ext.xMinimum(), ext.yMinimum()),
            QgsPointXY(ext.xMaximum(), ext.yMinimum()),
            QgsPointXY(ext.xMaximum(), ext.yMaximum()),
            QgsPointXY(ext.xMinimum(), ext.yMaximum()),
        ]
        self._extent_rb = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
        self._extent_rb.setColor(_style.RB_BLUE_FILL)
        self._extent_rb.setStrokeColor(_style.RB_BLUE)
        self._extent_rb.setWidth(_style.RB_WIDTH)
        self._extent_rb.setLineStyle(_style.RB_LINE_STYLE)
        self._extent_rb.setToGeometry(QgsGeometry.fromPolygonXY([pts]), None)

    def _update_drag_rb(self, start, end):
        if self._drag_rb is None:
            self._drag_rb = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
            self._drag_rb.setColor(_style.RB_RED_FILL)
            self._drag_rb.setStrokeColor(_style.RB_RED)
            self._drag_rb.setWidth(_style.RB_WIDTH)
            self._drag_rb.setLineStyle(_style.RB_LINE_STYLE)
        rect = QgsRectangle(start, end)
        self._drag_rb.setToGeometry(QgsGeometry.fromRect(rect))

    def _clear_rubber_bands(self):
        self._rm(self._drag_rb)
        self._rm(self._extent_rb)
        self._drag_rb   = None
        self._extent_rb = None

    # ── crop logic ────────────────────────────────────────────────────────

    def _do_crop(self, clip_rect: QgsRectangle):
        lyr = self._raster
        src = lyr.source()
        base, ext = os.path.splitext(src)
        dest = base + '_crop' + ext
        i = 2
        while os.path.exists(dest):
            dest = base + f'_crop{i}' + ext
            i += 1

        from osgeo import gdal
        ds = gdal.Open(src)
        if ds is None:
            self._log(f"  Cannot open '{lyr.name()}'", "#ff8844")
            return False
        ds = None  # release file handle; gdal.Warp takes the path directly

        # gdal.Warp handles both north-up and rotated geotransforms;
        # gdal.Translate -projwin fails on rotated rasters.
        opts = gdal.WarpOptions(
            outputBounds=(
                clip_rect.xMinimum(), clip_rect.yMinimum(),
                clip_rect.xMaximum(), clip_rect.yMaximum(),
            ),
            format='GTiff',
        )
        ds_out = gdal.Warp(dest, src, options=opts)
        ds_out = None

        if not os.path.exists(dest):
            self._log("  Crop failed — check rectangle intersects raster", "#ff5555")
            return False

        crop_name = os.path.splitext(os.path.basename(dest))[0]
        new_lyr = QgsRasterLayer(dest, crop_name)
        if new_lyr.isValid():
            QgsProject.instance().addMapLayer(new_lyr)
            self._log(f"  Cropped → {dest}", "#88ff88")
        else:
            self._log("  Crop output invalid", "#ff8844")
            return False
        return True

    # ── state helpers ─────────────────────────────────────────────────────

    def _select_raster(self, lyr):
        self._raster = lyr
        self._state  = _ST_RECT
        self._show_extent_band(lyr)
        self._log(
            f"  Raster '{lyr.name()}' selected — drag to draw clip rectangle",
            "#aaddff",
        )

    def _reset(self):
        self._clear_rubber_bands()
        self._raster     = None
        self._drag_start = None
        self._state      = _ST_PICK

    # ── QgsMapTool interface ──────────────────────────────────────────────

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        preselected = getattr(self._ctx, 'selected_raster', None)
        if preselected is not None and preselected.isValid():
            self._ctx.selected_raster = None
            self._select_raster(preselected)
        else:
            self._log("CROP  ──  click a raster, then drag to clip rectangle", "#aaddff")

    def deactivate(self):
        self._clear_rubber_bands()
        self._hint.hide()
        self._raster     = None
        self._drag_start = None
        self._state      = _ST_PICK
        super().deactivate()

    def canvasMoveEvent(self, event):
        self._show_hint(event.pos())
        if self._state == _ST_RECT and self._drag_start:
            end = self.toMapCoordinates(event.pos())
            self._update_drag_rb(self._drag_start, end)

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._hint.hide()
            self._go_home()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        map_pt = self.toMapCoordinates(event.pos())

        if self._state == _ST_PICK:
            raster = self._find_raster_at(map_pt)
            if raster:
                self._select_raster(raster)
            else:
                self._log("  No raster found at click", "#ffaa44")
            return

        if self._state == _ST_RECT:
            self._drag_start = map_pt

        self._canvas.setFocus()

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._state != _ST_RECT or self._drag_start is None:
            return

        end = self.toMapCoordinates(event.pos())
        rect = QgsRectangle(self._drag_start, end)
        self._rm(self._drag_rb)
        self._drag_rb    = None
        self._drag_start = None

        if rect.width() < 1e-8 or rect.height() < 1e-8:
            self._log("  Rectangle too small — try again", "#ffaa44")
            return

        self._do_crop(rect)
        self._go_home()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._state == _ST_RECT:
                self._reset()
                self._log("  Crop cancelled — click a raster to restart", "#ffaaaa")
            else:
                self._hint.hide()
                self._go_home()
