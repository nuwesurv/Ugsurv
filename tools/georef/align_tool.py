# -*- coding: utf-8 -*-
"""
AlignTool — re-georeference an existing on-canvas raster by clicking GCPs.

Workflow:
  1. Activation: user clicks on a raster already loaded in the canvas to select it.
  2. The raster's current geotransform is read to determine pixel ↔ map mapping.
  3. The user clicks recognisable features on the raster and types the corresponding
     ground E, N values via the dynamic-input widget (same as GeoreferenceTool).
  4. With ≥ 2 GCPs, pressing Enter writes a new geotransform and saves the result
     as <original>_aligned.tif alongside the source file.

Command: ALIGN / AL
"""

import math
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsMapLayerType,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker

from osgeo import gdal, osr

from ...core.base_tool import BaseTool, ToolState
from ...core import style as _style
from ...core.events import SemanticEvent, EventType


class AlignTool(BaseTool):
    """Re-georeference an existing raster layer using canvas-picked GCPs."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._source_layer = None        # QgsRasterLayer the user selected
        self._source_path = None         # GDAL-openable path from lyr.source()
        self._original_filepath = None   # same, kept for output path
        self._raster_w = 0
        self._raster_h = 0
        self._gt = None                  # full 6-element GDAL geotransform tuple
        self._pixel_scale = 1.0          # approximate map-units/pixel for bounds check
        self._placement_origin = (0, 0)  # (x0, y0) = top-left from existing gt
        self._gcps = []                  # list of (col, row, E, N)
        self._pending_pixel = None       # (col, row) awaiting ground-coord input
        self._pending_marker = None      # yellow marker shown immediately on click
        self._selection_rb = None        # blue outline around the selected raster
        self._waiting_coord = False
        self._selecting_raster = True    # phase 1: user picks the raster
        self._gcp_markers = []           # confirmed GCP markers (red)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def activate(self):
        super().activate()
        self._reset_session()
        self._selecting_raster = True
        self._transition(ToolState.ACTING)

        iface = getattr(self._ctx, 'iface', None)
        active = iface.activeLayer() if iface else None
        if (active is not None
                and active.type() == QgsMapLayerType.RasterLayer
                and os.path.exists(active.source())
                and self._read_raster_info(active)):
            self._source_layer = active
            self._selecting_raster = False
            self._show_selection_rb(active)
            epsg = self._project_epsg()
            crs_desc = QgsCoordinateReferenceSystem(f"EPSG:{epsg}").description()
            self._log(
                f"Selected: '{active.name()}'  ({self._raster_w}×{self._raster_h} px). "
                f"GCP coordinates in EPSG:{epsg} ({crs_desc}). "
                "Click image points → type E, N.  Enter = align  Esc = cancel.",
                "#aaddff",
            )
            self._request_input("no_value", "Click image point to set GCP 1")
        else:
            self._log(
                "Click on a raster in the canvas to select it for alignment.", "#aaddff"
            )
            self._request_input("no_value", "Click on a raster to select")

    def deactivate(self):
        self._clear_selection_rb()
        self._clear_pending_marker()
        self._clear_gcp_markers()
        self._source_layer = None   # don't remove — we didn't create it
        super().deactivate()

    # ── events ────────────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if self._selecting_raster:
            if sem.type == EventType.POINT_PICKED and sem.point:
                self._select_raster_at(sem.point)
            elif sem.type == EventType.CONFIRM:
                self._log("Click on a raster in the canvas first.", "#ffaa44")
            return

        if sem.type == EventType.POINT_PICKED and not self._waiting_coord:
            if sem.point:
                self._handle_canvas_click(sem.point)

        elif sem.type == EventType.COORDINATE_ENTERED and self._waiting_coord:
            if sem.point:
                self._handle_gcp_coord(sem.point)

        elif sem.type == EventType.CONFIRM:
            if self._waiting_coord:
                self._waiting_coord = False
                self._pending_pixel = None
                self._clear_pending_marker()
                self._request_input("no_value", self._gcp_click_prompt())
                self._log("Point cancelled. Click another image location.", "#ffaa44")
            else:
                self._try_align()

    def _on_undo_step(self):
        if self._selecting_raster:
            self._do_cancel()
            return
        if self._waiting_coord:
            self._waiting_coord = False
            self._pending_pixel = None
            self._clear_pending_marker()
            self._request_input("no_value", self._gcp_click_prompt())
            self._log("Point cancelled.", "#ffaa44")
        elif self._gcps:
            self._gcps.pop()
            self._pop_gcp_marker()
            self._log(f"GCP removed. {len(self._gcps)} remaining.", "#ffaa44")
        else:
            self._do_cancel()

    def _on_cancel_hook(self):
        self._waiting_coord = False
        self._pending_pixel = None
        self._selecting_raster = True
        self._gcps.clear()
        self._clear_gcp_markers()
        self._clear_pending_marker()
        self._clear_selection_rb()

    # ── raster selection ─────────────────────────────────────────────────

    def _select_raster_at(self, map_pt: QgsPointXY):
        """Find the topmost file-based raster layer whose extent contains map_pt.

        Only layers backed by a real file on disk are considered — WMS/WMTS/XYZ
        tile services and any other URL-sourced layers are skipped.
        """
        found = None
        for lyr in reversed(list(QgsProject.instance().mapLayers().values())):
            if lyr.type() != QgsMapLayerType.RasterLayer:
                continue
            if not os.path.exists(lyr.source()):
                continue
            if lyr.extent().contains(map_pt):
                found = lyr
                break

        if found is None:
            self._log(
                "No raster found at that location — click directly on a raster.", "#ffaa44"
            )
            return

        ok = self._read_raster_info(found)
        if not ok:
            return

        self._source_layer = found
        self._selecting_raster = False
        self._show_selection_rb(found)

        epsg = self._project_epsg()
        crs_desc = QgsCoordinateReferenceSystem(f"EPSG:{epsg}").description()
        self._log(
            f"Selected: '{found.name()}'  ({self._raster_w}×{self._raster_h} px). "
            f"GCP coordinates in EPSG:{epsg} ({crs_desc}). "
            "Click image points → type E, N.  Enter = align  Esc = cancel.",
            "#aaddff",
        )
        self._request_input("no_value", "Click image point to set GCP 1")

    def _read_raster_info(self, lyr) -> bool:
        """Read pixel dimensions and geotransform from an existing raster layer."""
        try:
            src = lyr.source()
            ds = gdal.Open(src)
            if ds is None:
                self._log(f"Cannot open raster: {src}", "#ff5555")
                return False

            gt = ds.GetGeoTransform()
            self._raster_w = ds.RasterXSize
            self._raster_h = ds.RasterYSize
            self._gt = gt
            self._source_path = src
            self._original_filepath = src
            ds = None

            self._placement_origin = (gt[0], gt[3])   # top-left (x0, y0)
            # Approximate pixel size for bounds checking (handles north-up rasters)
            self._pixel_scale = gt[1] if gt[1] > 1e-12 else 1.0
            return True
        except Exception as exc:
            self._log(f"Load error: {exc}", "#ff5555")
            return False

    # ── GCP collection ────────────────────────────────────────────────────

    def _map_to_pixel(self, map_pt: QgsPointXY):
        """Convert a map coordinate to (col, row) in the raster's pixel space."""
        gt = self._gt
        mx, my = map_pt.x(), map_pt.y()
        x0, y0 = gt[0], gt[3]
        det = gt[1] * gt[5] - gt[2] * gt[4]
        if abs(det) < 1e-20:
            # Fallback: north-up assumption
            col = (mx - x0) / (gt[1] or 1)
            row = (my - y0) / (gt[5] or -1)
        else:
            col = ((mx - x0) * gt[5] - (my - y0) * gt[2]) / det
            row = ((my - y0) * gt[1] - (mx - x0) * gt[4]) / det
        return col, row

    def _pixel_to_map(self, col: float, row: float) -> QgsPointXY:
        """Convert raster pixel (col, row) back to a map coordinate."""
        gt = self._gt
        mx = gt[0] + col * gt[1] + row * gt[2]
        my = gt[3] + col * gt[4] + row * gt[5]
        return QgsPointXY(mx, my)

    def _handle_canvas_click(self, map_pt: QgsPointXY):
        col, row = self._map_to_pixel(map_pt)

        tol = 5  # pixels tolerance at the edge
        if not (-tol <= col <= self._raster_w + tol and -tol <= row <= self._raster_h + tol):
            self._log("Click is outside the raster — try again.", "#ffaa44")
            return

        self._pending_pixel = (col, row)
        self._waiting_coord = True

        self._clear_pending_marker()
        self._add_pending_marker(map_pt)

        n = len(self._gcps) + 1
        epsg = self._project_epsg()
        self._request_input("en", f"GCP {n} — enter E, N  [EPSG:{epsg}]")
        self._log(
            f"Pixel ({round(col, 1)}, {round(row, 1)}) — type E, N and press Enter:",
            "#aaddff",
        )

    def _handle_gcp_coord(self, ground_pt: QgsPointXY):
        e, n = ground_pt.x(), ground_pt.y()
        col, row = self._pending_pixel
        self._gcps.append((col, row, e, n))

        marker_pt = self._pixel_to_map(col, row)
        self._clear_pending_marker()
        self._add_gcp_marker(marker_pt, len(self._gcps))

        self._log(
            f"GCP {len(self._gcps)}:  pixel({round(col,1)}, {round(row,1)})  "
            f"→  E={round(e,3)}  N={round(n,3)}",
            "#aaffaa",
        )
        self._waiting_coord = False
        self._pending_pixel = None

        if len(self._gcps) >= 2:
            self._log(
                f"{len(self._gcps)} GCPs collected. "
                "Add more for accuracy or press Enter to align.",
                "#88ccff",
            )
        else:
            self._log("Need 1 more point. Click another location.", "#aaddff")
        self._request_input("no_value", self._gcp_click_prompt())

    # ── alignment ─────────────────────────────────────────────────────────

    def _try_align(self):
        if len(self._gcps) < 2:
            self._log(f"Need at least 2 GCPs (have {len(self._gcps)}).", "#ff8844")
            return
        if len(self._gcps) == 2:
            self._align_2pt()
        else:
            self._align_gdal_gcps()

    def _align_2pt(self):
        """2-point conformal (similarity) transform."""
        col1, row1, e1, n1 = self._gcps[0]
        col2, row2, e2, n2 = self._gcps[1]
        dc, dr = col2 - col1, row2 - row1
        de, dn = e2 - e1, n2 - n1
        img_dist = math.hypot(dc, dr)
        map_dist = math.hypot(de, dn)
        if img_dist < 1e-10:
            self._log("Both GCP pixels are at the same location — can't compute.", "#ff5555")
            return

        scale = map_dist / img_dist
        angle_img = math.atan2(-dr, dc)
        angle_map = math.atan2(dn, de)
        theta = angle_map - angle_img

        gt1 = scale * math.cos(theta)
        gt2 = scale * math.sin(theta)
        gt4 = gt2
        gt5 = -gt1
        gt0 = e1 - gt1 * col1 - gt2 * row1
        gt3 = n1 - gt4 * col1 - gt5 * row1

        self._write_and_load([gt0, gt1, gt2, gt3, gt4, gt5])

    def _align_gdal_gcps(self):
        """Polynomial warp from 3+ GCPs using GDAL."""
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(self._project_epsg())
        wkt = srs.ExportToWkt()

        gdal_gcps = [
            gdal.GCP(float(e), float(n), 0.0, float(col), float(row))
            for col, row, e, n in self._gcps
        ]

        ds = gdal.Open(self._source_path)
        if ds is None:
            self._log("Failed to open raster.", "#ff5555")
            return

        ds_mem = gdal.GetDriverByName('MEM').CreateCopy('', ds)
        ds_mem.SetGCPs(gdal_gcps, wkt)
        ds = None

        output_path = self._output_path()
        warp_opts = gdal.WarpOptions(
            dstSRS=wkt,
            polynomialOrder=1,
            resampleAlg=gdal.GRA_Cubic,
            creationOptions=['COMPRESS=LZW', 'PREDICTOR=2', 'BIGTIFF=IF_NEEDED'],
        )
        ds_out = gdal.Warp(output_path, ds_mem, options=warp_opts)
        if ds_out is None:
            self._log("GDAL warp failed.", "#ff5555")
            ds_mem = None
            return
        ds_out = None
        ds_mem = None

        self._finish(output_path)

    def _write_and_load(self, geotransform: list):
        ds = gdal.Open(self._source_path)
        if ds is None:
            self._log("Failed to open raster for writing.", "#ff5555")
            return

        output_path = self._output_path()
        ds_out = gdal.Translate(
            output_path, ds, format='GTiff',
            creationOptions=['COMPRESS=LZW', 'PREDICTOR=2', 'BIGTIFF=IF_NEEDED'],
        )
        ds_out.SetGeoTransform(geotransform)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(self._project_epsg())
        ds_out.SetProjection(srs.ExportToWkt())
        ds_out = None
        ds = None

        self._finish(output_path)

    def _finish(self, output_path: str):
        lyr = QgsRasterLayer(output_path, 'Aligned Raster')
        if not lyr.isValid():
            self._log(f"Output raster is invalid: {output_path}", "#ff5555")
            self._go_home()
            return

        QgsProject.instance().addMapLayer(lyr)
        self._log(f"Aligned  →  {output_path}", "#aaffaa")
        self._commit_gcp_points()
        self._go_home()

    def _commit_gcp_points(self):
        storage = getattr(self._ctx, 'storage_manager', None)
        if storage is None or not storage.enabled:
            self._log(
                "GCP points not saved — save the project first to enable the layer.",
                "#ffaa44",
            )
            return

        _plugin_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        _symbol_path = os.path.join(
            _plugin_dir, "map_icons", "point_icons", "benchmark1.svg"
        )

        saved = 0
        for _col, _row, e, n in self._gcps:
            geom = QgsGeometry.fromPointXY(QgsPointXY(e, n))
            ok = storage.add_point(
                geom,
                cad_layer="0",
                description="GCP",
                symbol=_symbol_path,
                symbol_size=4.0,
            )
            if ok:
                saved += 1

        if saved:
            layer = storage._points_layer
            if layer is not None:
                from ...core.renderer_utils import apply_point_color_renderer
                apply_point_color_renderer(layer)
                layer.triggerRepaint()
            self._log(
                f"{saved} GCP point(s) added  "
                f"(Description='GCP', Symbol='benchmark1.svg', Size=4px).",
                "#aaffaa",
            )
        else:
            self._log("GCP points could not be written to the layer.", "#ff8844")

    def _output_path(self) -> str:
        base = os.path.splitext(self._original_filepath)[0]
        return f"{base}_aligned.tif"

    # ── markers ───────────────────────────────────────────────────────────

    def _add_gcp_marker(self, map_pt: QgsPointXY, index: int):
        m = QgsVertexMarker(self.canvas())
        m.setCenter(map_pt)
        m.setIconType(QgsVertexMarker.ICON_CROSS)
        m.setColor(_style.RB_RED)
        m.setIconSize(18)
        m.setPenWidth(3)
        self._gcp_markers.append(m)

    def _pop_gcp_marker(self):
        if self._gcp_markers:
            m = self._gcp_markers.pop()
            try:
                self.canvas().scene().removeItem(m)
            except Exception:  # nosec B110
                pass

    def _clear_gcp_markers(self):
        for m in self._gcp_markers:
            try:
                self.canvas().scene().removeItem(m)
            except Exception:  # nosec B110
                pass
        self._gcp_markers.clear()

    def _add_pending_marker(self, map_pt: QgsPointXY):
        m = QgsVertexMarker(self.canvas())
        m.setCenter(map_pt)
        m.setIconType(QgsVertexMarker.ICON_X)
        m.setColor(QColor('#ffcc00'))
        m.setIconSize(20)
        m.setPenWidth(3)
        self._pending_marker = m

    def _clear_pending_marker(self):
        if self._pending_marker is not None:
            try:
                self.canvas().scene().removeItem(self._pending_marker)
            except Exception:  # nosec B110
                pass
            self._pending_marker = None

    def _gcp_click_prompt(self) -> str:
        n = len(self._gcps) + 1
        if len(self._gcps) >= 2:
            return f"Click image point for GCP {n}  or  Enter to align"
        return f"Click image point to set GCP {n}"

    def _show_selection_rb(self, lyr):
        """Draw a blue dashed rectangle around the selected raster's extent."""
        self._clear_selection_rb()
        ext = lyr.extent()
        points = [
            QgsPointXY(ext.xMinimum(), ext.yMaximum()),
            QgsPointXY(ext.xMaximum(), ext.yMaximum()),
            QgsPointXY(ext.xMaximum(), ext.yMinimum()),
            QgsPointXY(ext.xMinimum(), ext.yMinimum()),
        ]
        rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
        rb.setStrokeColor(_style.RB_SOURCE)
        rb.setFillColor(_style.RB_SOURCE_FILL)
        rb.setWidth(_style.RB_WIDTH)
        rb.setLineStyle(_style.RB_LINE_STYLE)
        rb.setToGeometry(QgsGeometry.fromPolygonXY([points]), None)
        self._selection_rb = rb

    def _clear_selection_rb(self):
        if self._selection_rb is not None:
            try:
                self.canvas().scene().removeItem(self._selection_rb)
            except Exception:  # nosec B110
                pass
            self._selection_rb = None

    # ── helpers ───────────────────────────────────────────────────────────

    def _reset_session(self):
        self._gcps.clear()
        self._pending_pixel = None
        self._waiting_coord = False
        self._selecting_raster = True
        self._placement_origin = (0, 0)
        self._pixel_scale = 1.0
        self._gt = None
        self._source_layer = None
        self._source_path = None
        self._original_filepath = None
        self._clear_gcp_markers()
        self._clear_pending_marker()
        self._clear_selection_rb()

    def _project_epsg(self) -> int:
        try:
            return QgsProject.instance().crs().postgisSrid()
        except Exception:
            return 32636

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)
