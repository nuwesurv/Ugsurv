# -*- coding: utf-8 -*-
"""
GeoreferenceTool — canvas-based georeferencing via dynamic input.

Workflow:
  1. Activation opens a file picker (image / PDF).
  2. The raster dimensions are read; the user then clicks the canvas to place
     the raster (its bottom-right corner follows the cursor until clicked).
  3. After placement the raster is shown on the canvas with a 1:1 pixel-to-map-
     unit dummy geotransform anchored at the chosen position.
  4. The user clicks on recognisable image features.  After each click the
     dynamic-input widget appears in "en" mode and the user types the
     corresponding ground E, N values.
  5. After collecting ≥ 2 GCPs the user presses Enter / right-click to apply
     the geotransform.  The output is saved as <original>_georef.tif (next to
     the input file) and added to the project.

The CRS badge in the dynamic-input widget always shows the project CRS,
which is the coordinate system the user should type GCP coordinates in.
"""

import math
import os
import tempfile

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker

from osgeo import gdal, osr

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class GeoreferenceTool(BaseTool):
    """Canvas-based georeferencing tool (extends BaseTool for dynamic-input wiring)."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._raster_src = None          # path used as GDAL source (after PDF→PNG)
        self._original_filepath = None   # user-selected file (for output path)
        self._display_path = None        # temp TIFF shown on canvas
        self._raster_w = 0
        self._raster_h = 0
        self._gcps = []                  # list of (col, row, E, N)
        self._pending_pixel = None       # (col, row) awaiting ground-coord input
        self._waiting_coord = False
        self._placing_raster = False     # True while user is picking placement point
        self._placement_origin = (0, 0)  # (x0, y0) top-left of display geotransform
        self._placement_rb = None        # QgsRubberBand preview during placement
        self._gcp_markers = []           # QgsVertexMarkers at clicked positions
        self._display_layer = None       # QgsRasterLayer currently on canvas

    # ── lifecycle ─────────────────────────────────────────────────────────

    def activate(self):
        super().activate()
        self._reset_session()

        # Pick raster file
        from qgis.PyQt.QtWidgets import QFileDialog
        filepath, _ = QFileDialog.getOpenFileName(
            None,
            'Select raster / image to georeference',
            '',
            'Raster / Image files (*.png *.jpg *.jpeg *.tif *.tiff *.pdf)',
        )
        if not filepath or not os.path.exists(filepath):
            self._log("Georeferencing cancelled — no file selected.", "#ffaa44")
            self._go_home()
            return

        self._original_filepath = filepath
        ok = self._load_raster_info(filepath)
        if not ok:
            self._log("Could not load the selected file.", "#ff5555")
            self._go_home()
            return

        self._placing_raster = True
        self._transition(ToolState.ACTING)
        self._log(
            f"Raster ready ({self._raster_w}×{self._raster_h} px). "
            "Click on the canvas to place it — bottom-right corner tracks the cursor.",
            "#aaddff",
        )

    def deactivate(self):
        self._clear_placement_rb()
        self._restore_display_layer()
        super().deactivate()

    # ── events ────────────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if self._placing_raster:
            if sem.type == EventType.POINT_PICKED and sem.point:
                self._finalize_placement(sem.point)
            elif sem.type == EventType.CONFIRM:
                self._log("Click on the canvas to place the raster first.", "#ffaa44")
            return

        if sem.type == EventType.POINT_PICKED and not self._waiting_coord:
            if sem.point:
                self._handle_canvas_click(sem.point)

        elif sem.type == EventType.COORDINATE_ENTERED and self._waiting_coord:
            if sem.point:
                self._handle_gcp_coord(sem.point)

        elif sem.type == EventType.CONFIRM:
            if self._waiting_coord:
                # Cancel the pending click (not the whole tool)
                self._waiting_coord = False
                self._pending_pixel = None
                self._request_input("", "")
                self._log("Point cancelled. Click another image location.", "#ffaa44")
            else:
                self._try_georeference()

    def _on_hover(self, sem: SemanticEvent):
        if self._placing_raster and sem.point:
            self._update_placement_rb(sem.point)

    def _on_undo_step(self):
        if self._placing_raster:
            self._do_cancel()
            return
        if self._waiting_coord:
            self._waiting_coord = False
            self._pending_pixel = None
            self._request_input("", "")
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
        self._placing_raster = False
        self._gcps.clear()
        self._clear_gcp_markers()
        self._clear_placement_rb()

    # ── placement ────────────────────────────────────────────────────────

    def _update_placement_rb(self, map_pt: QgsPointXY):
        """Draw a rectangle outline that tracks the cursor (bottom-right corner)."""
        if self._placement_rb is None:
            self._placement_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            self._placement_rb.setColor(QColor(255, 180, 50, 60))
            self._placement_rb.setStrokeColor(QColor(255, 180, 50, 220))
            self._placement_rb.setWidth(2)

        px, py = map_pt.x(), map_pt.y()
        x0 = px - self._raster_w
        y0 = py + self._raster_h   # top in map coords
        points = [
            QgsPointXY(x0, y0),
            QgsPointXY(px, y0),
            QgsPointXY(px, py),
            QgsPointXY(x0, py),
        ]
        self._placement_rb.setToGeometry(QgsGeometry.fromPolygonXY([points]), None)

    def _finalize_placement(self, map_pt: QgsPointXY):
        """Commit the chosen placement and load the raster onto the canvas."""
        self._clear_placement_rb()
        px, py = map_pt.x(), map_pt.y()
        x0 = px - self._raster_w
        y0 = py + self._raster_h   # top-left of geotransform

        try:
            tmp_dir = tempfile.mkdtemp(prefix='ugsurv_georef_')
            tmp_path = os.path.join(tmp_dir, 'display.tif')
            ds = gdal.Open(self._raster_src)
            if ds is None:
                self._log("Failed to open raster for display.", "#ff5555")
                self._go_home()
                return
            ds_tmp = gdal.Translate(tmp_path, ds, format='GTiff')
            ds_tmp.SetGeoTransform([x0, 1, 0, y0, 0, -1])
            ds_tmp = None
            ds = None
        except Exception as exc:
            self._log(f"Placement error: {exc}", "#ff5555")
            self._go_home()
            return

        self._display_path = tmp_path
        self._placement_origin = (x0, y0)

        lyr = QgsRasterLayer(tmp_path, '_georef_display_')
        if not lyr.isValid():
            self._log("Could not display raster on canvas.", "#ff5555")
            self._go_home()
            return
        QgsProject.instance().addMapLayer(lyr)
        self._display_layer = lyr

        ext = lyr.extent()
        if not ext.isNull():
            self.canvas().setExtent(ext)
            self.canvas().refresh()

        self._placing_raster = False

        epsg = self._project_epsg()
        crs_desc = QgsCoordinateReferenceSystem(f"EPSG:{epsg}").description()
        self._log(
            f"Raster placed ({self._raster_w}×{self._raster_h} px). "
            f"GCP coordinates expected in: EPSG:{epsg}  ({crs_desc}). "
            "Click image points → type E, N.  Enter = georeference  Esc = cancel.",
            "#aaddff",
        )
        self._request_input("", "")

    def _clear_placement_rb(self):
        if self._placement_rb is not None:
            try:
                self.canvas().scene().removeItem(self._placement_rb)
            except Exception:
                pass
            self._placement_rb = None

    # ── GCP collection ────────────────────────────────────────────────────

    def _handle_canvas_click(self, map_pt: QgsPointXY):
        x0, y0 = self._placement_origin
        col = map_pt.x() - x0
        row = y0 - map_pt.y()

        # Reject clicks outside the raster bounds (with a small tolerance)
        if not (-5 <= col <= self._raster_w + 5 and -5 <= row <= self._raster_h + 5):
            self._log("Click is outside the raster — try again.", "#ffaa44")
            return

        self._pending_pixel = (col, row)
        self._waiting_coord = True
        n = len(self._gcps) + 1
        epsg = self._project_epsg()
        self._request_input(
            "en",
            f"GCP {n} — enter E, N  [EPSG:{epsg}]",
        )
        self._log(
            f"Pixel ({round(col, 1)}, {round(row, 1)}) — type E, N and press Enter:",
            "#aaddff",
        )

    def _handle_gcp_coord(self, ground_pt: QgsPointXY):
        e, n = ground_pt.x(), ground_pt.y()
        col, row = self._pending_pixel
        self._gcps.append((col, row, e, n))

        x0, y0 = self._placement_origin
        marker_map_pt = QgsPointXY(x0 + col, y0 - row)
        self._add_gcp_marker(marker_map_pt, len(self._gcps))

        self._log(
            f"GCP {len(self._gcps)}:  pixel({round(col,1)}, {round(row,1)})  "
            f"→  E={round(e,3)}  N={round(n,3)}",
            "#aaffaa",
        )
        self._waiting_coord = False
        self._pending_pixel = None
        self._request_input("", "")

        if len(self._gcps) >= 2:
            self._log(
                f"{len(self._gcps)} GCPs collected. "
                "Add more for accuracy or press Enter to georeference.",
                "#88ccff",
            )
        else:
            self._log("Need 1 more point. Click another location.", "#aaddff")

    # ── georeferencing ────────────────────────────────────────────────────

    def _try_georeference(self):
        if len(self._gcps) < 2:
            self._log(
                f"Need at least 2 GCPs (have {len(self._gcps)}).", "#ff8844"
            )
            return
        if len(self._gcps) == 2:
            self._georeference_2pt()
        else:
            self._georeference_gdal_gcps()

    def _georeference_2pt(self):
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

    def _georeference_gdal_gcps(self):
        """Polynomial warp from 3+ GCPs using GDAL."""
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(self._project_epsg())
        wkt = srs.ExportToWkt()

        gdal_gcps = [
            gdal.GCP(float(e), float(n), 0.0, float(col), float(row))
            for col, row, e, n in self._gcps
        ]

        ds = gdal.Open(self._display_path)
        if ds is None:
            self._log("Failed to open display raster.", "#ff5555")
            return

        ds_mem = gdal.GetDriverByName('MEM').CreateCopy('', ds)
        ds_mem.SetGCPs(gdal_gcps, wkt)
        ds = None

        output_path = self._output_path()
        warp_opts = gdal.WarpOptions(
            dstSRS=wkt,
            polynomialOrder=1,
            resampleAlg=gdal.GRA_Bilinear,
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
        ds = gdal.Open(self._display_path)
        if ds is None:
            self._log("Failed to open raster for writing.", "#ff5555")
            return

        output_path = self._output_path()
        ds_out = gdal.Translate(output_path, ds, format='GTiff')
        ds_out.SetGeoTransform(geotransform)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(self._project_epsg())
        ds_out.SetProjection(srs.ExportToWkt())
        ds_out = None
        ds = None

        self._finish(output_path)

    def _finish(self, output_path: str):
        self._remove_display_layer()

        lyr = QgsRasterLayer(output_path, 'Georeferenced Raster')
        if not lyr.isValid():
            self._log(f"Output raster is invalid: {output_path}", "#ff5555")
            self._go_home()
            return

        QgsProject.instance().addMapLayer(lyr)
        self._log(f"Georeferenced → {output_path}", "#aaffaa")

        self._commit_gcp_points()
        self._go_home()

    def _commit_gcp_points(self):
        """Write each GCP ground position to the points layer."""
        storage = getattr(self._ctx, 'storage_manager', None)
        if storage is None or not storage.enabled:
            self._log(
                "GCP points not saved — save the project first to enable the layer.",
                "#ffaa44",
            )
            return

        saved = 0
        for i, (col, row, e, n) in enumerate(self._gcps, 1):
            geom = QgsGeometry.fromPointXY(QgsPointXY(e, n))
            ok = storage.add_point(
                geom,
                cad_layer="0",
                description=f"Georeference Point {i}",
                symbol="benchmark2.svg",
            )
            if ok:
                saved += 1

        if saved:
            self._log(
                f"{saved} GCP point(s) added to points layer  "
                f"(Description='Georeference Point N', Symbol='benchmark2.svg').",
                "#aaffaa",
            )
        else:
            self._log("GCP points could not be written to the layer.", "#ff8844")

    # ── raster loading ────────────────────────────────────────────────────

    def _load_raster_info(self, filepath: str) -> bool:
        """Read raster dimensions without adding anything to the canvas yet."""
        try:
            src = filepath
            if filepath.lower().endswith('.pdf'):
                src = self._pdf_first_page(filepath)
                if src is None:
                    return False

            ds = gdal.Open(src)
            if ds is None:
                return False

            self._raster_w = ds.RasterXSize
            self._raster_h = ds.RasterYSize
            self._raster_src = src
            ds = None
            return True
        except Exception as exc:
            self._log(f"Load error: {exc}", "#ff5555")
            return False

    def _pdf_first_page(self, pdf_path: str):
        try:
            import fitz
            from PIL import Image
            pdf = fitz.open(pdf_path)
            pix = pdf[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            tmp_dir = tempfile.mkdtemp(prefix='ugsurv_georef_pdf_')
            out = os.path.join(tmp_dir, 'page0.png')
            img.save(out, 'PNG')
            pdf.close()
            return out
        except Exception as exc:
            self._log(f"PDF conversion failed: {exc}", "#ff5555")
            return None

    def _remove_display_layer(self):
        if self._display_layer is not None:
            try:
                QgsProject.instance().removeMapLayer(self._display_layer.id())
            except Exception:
                pass
            self._display_layer = None

    def _restore_display_layer(self):
        """Called on deactivate — remove temp display layer if still present."""
        self._remove_display_layer()

    def _output_path(self) -> str:
        """Output TIF placed next to the original input file."""
        base = os.path.splitext(self._original_filepath)[0]
        return f"{base}_georef.tif"

    # ── markers ───────────────────────────────────────────────────────────

    def _add_gcp_marker(self, map_pt: QgsPointXY, index: int):
        m = QgsVertexMarker(self.canvas())
        m.setCenter(map_pt)
        m.setIconType(QgsVertexMarker.ICON_CROSS)
        m.setColor(QColor(255, 100, 0))
        m.setIconSize(18)
        m.setPenWidth(3)
        self._gcp_markers.append(m)

    def _pop_gcp_marker(self):
        if self._gcp_markers:
            m = self._gcp_markers.pop()
            try:
                self.canvas().scene().removeItem(m)
            except Exception:
                pass

    def _clear_gcp_markers(self):
        for m in self._gcp_markers:
            try:
                self.canvas().scene().removeItem(m)
            except Exception:
                pass
        self._gcp_markers.clear()

    # ── helpers ───────────────────────────────────────────────────────────

    def _reset_session(self):
        self._gcps.clear()
        self._pending_pixel = None
        self._waiting_coord = False
        self._placing_raster = False
        self._placement_origin = (0, 0)
        self._clear_gcp_markers()
        self._clear_placement_rb()
        self._remove_display_layer()

    def _project_epsg(self) -> int:
        try:
            return QgsProject.instance().crs().postgisSrid()
        except Exception:
            return 32636

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)
