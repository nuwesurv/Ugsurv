# -*- coding: utf-8 -*-
"""
DimensionTool (DIM) and AutoDimensionTool (ADIM).

Architecture:
  - Dedicated "dimensions" line layer (in the project GeoPackage if saved,
    else a memory layer).
  - Each dimension feature is a plain LineString(p1, p2) — the measured segment.
  - The line itself is invisible (QgsNullSymbolRenderer).
  - Distance is stored in the "distance" field (Double, metres).
  - QGIS label renderer shows the value along the line in Open Sans Bold Italic.

DIM  — click on an edge (auto-dims nearest segment) or click p1 then p2.
ADIM — click on any feature to dimension every segment of its geometry at once.
"""

import math
import os

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QFont, QColor
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsWkbTypes, QgsRectangle,
    QgsVectorLayer, QgsProject, QgsFeature, QgsField,
    QgsVectorFileWriter, QgsDistanceArea, QgsUnitTypes,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsPalLayerSettings, QgsVectorLayerSimpleLabeling,
    QgsTextFormat, QgsTextBufferSettings, QgsNullSymbolRenderer,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType, SnapType
from ...core import style as _style

_DIM_LAYER_NAME = "dimensions"
_LAYER_EPSG     = 32636
_LAYER_CRS_AUTH = f"EPSG:{_LAYER_EPSG}"

_AUTO_OFFSET_RATIO = 0.12
_AUTO_OFFSET_MIN   = 1.0

_C_PREVIEW = _style.RB_PREVIEW
_C_EDGE    = _style.RB_EDGE


# ── Layer management ──────────────────────────────────────────────────────────

def _get_or_create_dim_layer(ctx) -> QgsVectorLayer | None:
    """Return the _dimensions layer, creating it if it does not exist yet."""
    # 1. Already loaded in the project?
    found = QgsProject.instance().mapLayersByName(_DIM_LAYER_NAME)
    if found:
        layer = found[0]
        _ensure_fields(layer)
        if not layer.isEditable():
            layer.startEditing()
        return layer

    # 2. Exists in the project GeoPackage?
    gpkg = getattr(ctx.storage_manager, 'gpkg_path', None)
    if gpkg and os.path.exists(gpkg):
        uri = f"{gpkg}|layername={_DIM_LAYER_NAME}"
        lyr = QgsVectorLayer(uri, _DIM_LAYER_NAME, "ogr")
        if lyr.isValid():
            _register_layer(lyr)
            _ensure_fields(lyr)
            _apply_dim_style(lyr)
            if not lyr.isEditable():
                lyr.startEditing()
            return lyr
        # Table not yet in GPKG — create it
        _add_dim_table_to_gpkg(gpkg)
        lyr = QgsVectorLayer(f"{gpkg}|layername={_DIM_LAYER_NAME}", _DIM_LAYER_NAME, "ogr")
        if lyr.isValid():
            _register_layer(lyr)
            _apply_dim_style(lyr)
            if not lyr.isEditable():
                lyr.startEditing()
            return lyr

    # 3. Fallback: in-memory layer (project not yet saved)
    mem = QgsVectorLayer(
        f"LineString?crs={_LAYER_CRS_AUTH}", _DIM_LAYER_NAME, "memory"
    )
    mem.dataProvider().addAttributes([QgsField("distance", QVariant.Double)])
    mem.updateFields()
    _register_layer(mem)
    _apply_dim_style(mem)
    mem.startEditing()
    return mem


def _ensure_fields(layer: QgsVectorLayer):
    existing = {f.name() for f in layer.fields()}
    if "distance" not in existing:
        layer.dataProvider().addAttributes([QgsField("distance", QVariant.Double)])
        layer.updateFields()


def _add_dim_table_to_gpkg(gpkg: str):
    crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
    tmp = QgsVectorLayer(
        f"LineString?crs={_LAYER_CRS_AUTH}", _DIM_LAYER_NAME, "memory"
    )
    tmp.dataProvider().addAttributes([QgsField("distance", QVariant.Double)])
    tmp.updateFields()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName  = _DIM_LAYER_NAME
    opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
    QgsVectorFileWriter.writeAsVectorFormatV3(
        tmp, gpkg, QgsProject.instance().transformContext(), opts
    )


def _register_layer(layer: QgsVectorLayer):
    root  = QgsProject.instance().layerTreeRoot()
    group = root.findGroup("Ugsurv") or root.insertGroup(0, "Ugsurv")
    QgsProject.instance().addMapLayer(layer, False)
    group.addLayer(layer)


def _apply_dim_style(layer: QgsVectorLayer):
    """Invisible line + Open Sans Bold Italic label along the line."""
    # No geometry drawn — label only
    layer.setRenderer(QgsNullSymbolRenderer())

    pal = QgsPalLayerSettings()
    # Use an expression so we can append ' m' and control decimal places
    pal.isExpression = True
    pal.fieldName    = 'format_number("distance", 1)'
    pal.placement    = QgsPalLayerSettings.Line

    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.6)
    buf.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    buf.setColor(QColor(255, 255, 255))

    fmt  = QgsTextFormat()
    font = QFont("Open Sans")
    font.setBold(True)
    font.setItalic(True)
    fmt.setFont(font)
    fmt.setSize(9)
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    fmt.setColor(QColor(0, 0, 0))
    fmt.setBuffer(buf)
    pal.setFormat(fmt)

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


# ── Shared geometry / measurement helpers ─────────────────────────────────────

def _distance_m(p1: QgsPointXY, p2: QgsPointXY) -> float:
    """Geodesic distance in metres between two points in the project CRS."""
    da = QgsDistanceArea()
    da.setSourceCrs(
        QgsProject.instance().crs(),
        QgsProject.instance().transformContext(),
    )
    da.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
    raw = da.measureLine(p1, p2)
    return da.convertLengthMeasurement(raw, QgsUnitTypes.DistanceMeters)


def _project_to_layer_geom(p1: QgsPointXY, p2: QgsPointXY) -> QgsGeometry:
    """
    Build a LineString(p1, p2) transformed from the project CRS to
    the layer CRS (EPSG:32636).
    """
    proj_crs  = QgsProject.instance().crs()
    layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
    geom = QgsGeometry.fromPolylineXY([p1, p2])
    if proj_crs != layer_crs:
        xform = QgsCoordinateTransform(
            proj_crs, layer_crs, QgsProject.instance()
        )
        geom.transform(xform)
    return geom


def _is_duplicate(layer: QgsVectorLayer, geom: QgsGeometry) -> bool:
    """True if an equivalent dimension already exists (either direction)."""
    pts = geom.asPolyline()
    if len(pts) < 2:
        return False
    rev = QgsGeometry.fromPolylineXY([pts[1], pts[0]])
    for feat in layer.getFeatures():
        fg = feat.geometry()
        if fg and (fg.equals(geom) or fg.equals(rev)):
            return True
    return False


def _add_dim_feature(layer: QgsVectorLayer, geom: QgsGeometry, dist_m: float) -> bool:
    """Write one dimension feature. Returns False if duplicate or layer dead."""
    if not layer or not layer.isValid():
        return False
    if _is_duplicate(layer, geom):
        return False
    if not layer.isEditable():
        layer.startEditing()
    feat = QgsFeature(layer.fields())
    feat.setGeometry(geom)
    feat["distance"] = round(dist_m, 3)
    ok = layer.addFeature(feat)
    if ok:
        layer.triggerRepaint()
    return ok


def _segments_from_geom(geom: QgsGeometry) -> list[tuple[QgsPointXY, QgsPointXY]]:
    """Extract all consecutive vertex pairs from any geometry type."""
    gt = QgsWkbTypes.geometryType(geom.wkbType())
    rings: list[list[QgsPointXY]] = []

    if gt == QgsWkbTypes.PolygonGeometry:
        if geom.isMultipart():
            for poly in geom.asMultiPolygon():
                rings.extend(poly)
        else:
            rings = geom.asPolygon()
    elif gt == QgsWkbTypes.LineGeometry:
        if geom.isMultipart():
            rings = geom.asMultiPolyline()
        else:
            rings = [geom.asPolyline()]

    segments = []
    for ring in rings:
        for i in range(1, len(ring)):
            a, b = ring[i - 1], ring[i]
            if a.distance(b) > 1e-10:   # skip zero-length closing vertex
                segments.append((a, b))
    return segments


def _find_feature_near(ctx, map_pt: QgsPointXY):
    """Return (layer, feature) of the nearest non-point, non-dimension feature."""
    tol  = 12 * ctx.canvas.mapUnitsPerPixel()
    rect = QgsRectangle(
        map_pt.x() - tol, map_pt.y() - tol,
        map_pt.x() + tol, map_pt.y() + tol,
    )
    pt_geom   = QgsGeometry.fromPointXY(map_pt)
    best_lyr  = None
    best_feat = None
    best_dist = tol
    for lyr in QgsProject.instance().mapLayers().values():
        if not isinstance(lyr, QgsVectorLayer):
            continue
        if lyr.name() == _DIM_LAYER_NAME:
            continue
        if int(lyr.geometryType()) == 0:   # skip point layers
            continue
        if not lyr.isValid():
            continue
        for feat in lyr.getFeatures(rect):
            g = feat.geometry()
            if g.isNull() or g.isEmpty():
                continue
            d = g.distance(pt_geom)
            if d < best_dist:
                best_dist = d
                best_lyr  = lyr
                best_feat = feat
    return best_lyr, best_feat


def _nearest_segment(map_pt: QgsPointXY, geom: QgsGeometry):
    verts    = list(geom.vertices())
    best_a   = best_b = None
    best_d   = float('inf')
    pt_geom  = QgsGeometry.fromPointXY(map_pt)
    for i in range(len(verts) - 1):
        a   = QgsPointXY(verts[i].x(),   verts[i].y())
        b   = QgsPointXY(verts[i+1].x(), verts[i+1].y())
        seg = QgsGeometry.fromPolylineXY([a, b])
        d   = seg.distance(pt_geom)
        if d < best_d:
            best_d = d
            best_a, best_b = a, b
    return best_a, best_b


# ── DIM tool — single segment ─────────────────────────────────────────────────

class DimensionTool(BaseTool):
    """DIM — click on edge = auto-dim segment; or click p1 then p2."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._pts: list[QgsPointXY] = []
        self._preview_rb  = None
        self._edge_rb     = None
        self._dim_layer   = None

    def activate(self):
        super().activate()
        self._dim_layer = _get_or_create_dim_layer(self._ctx)
        self._pts.clear()
        self._transition(ToolState.ACTING)
        self._log("DIM — snap to edge to auto-dimension, or click two points")
        self._request_input("xy", "Snap to edge or click first point:")

    # ── events ────────────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            self._reset()
            self._go_home()
            return
        if sem.type not in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            return
        pt = sem.point
        if pt is None:
            return

        # Edge click with no pts → auto-dim nearest segment
        if len(self._pts) == 0 and sem.snap_type == SnapType.NEAREST:
            if self._auto_dim_segment(pt):
                return

        self._pts.append(pt)

        if len(self._pts) == 1:
            self._log(f"p1: ({round(pt.x(),3)}, {round(pt.y(),3)})  — pick end point")
            self._update_prompt("End point:")

        elif len(self._pts) == 2:
            self._commit_two_point()

    def _on_hover(self, sem: SemanticEvent):
        pt = sem.point
        if pt:
            dyn = getattr(self._ctx, 'dyn_widget', None)
            if dyn is not None:
                dyn.set_live_pair(pt.x(), pt.y())

        if len(self._pts) == 0:
            if sem.snap_type == SnapType.NEAREST and pt:
                _, feat = _find_feature_near(self._ctx, pt)
                if feat:
                    a, b = _nearest_segment(pt, feat.geometry())
                    if a and b:
                        self._show_edge_rb(a, b)
                    else:
                        self._clear_edge_rb()
                else:
                    self._clear_edge_rb()
            else:
                self._clear_edge_rb()

        elif len(self._pts) == 1 and pt:
            self._clear_edge_rb()
            self._update_preview(self._pts[0], pt)

        self._update_extension_guide(sem.snap_type, pt)

    # ── auto-dim segment ──────────────────────────────────────────────────

    def _auto_dim_segment(self, snap_pt: QgsPointXY) -> bool:
        _, feat = _find_feature_near(self._ctx, snap_pt)
        if feat is None:
            return False
        a, b = _nearest_segment(snap_pt, feat.geometry())
        if a is None or b is None:
            return False
        self._commit_pair(a, b)
        return True

    # ── commit ────────────────────────────────────────────────────────────

    def _commit_two_point(self):
        p1, p2 = self._pts[0], self._pts[1]
        self._commit_pair(p1, p2)

    def _commit_pair(self, p1: QgsPointXY, p2: QgsPointXY):
        dist = _distance_m(p1, p2)
        geom = _project_to_layer_geom(p1, p2)
        ok   = _add_dim_feature(self._dim_layer, geom, dist)
        if ok:
            self._log(f"Dimension: {round(dist, 3)} m", "#aaffaa")
        else:
            self._log("Dimension already exists — skipped.", "#ffaa55")
        self._reset()

    # ── rubber bands ──────────────────────────────────────────────────────

    def _show_edge_rb(self, a: QgsPointXY, b: QgsPointXY):
        if self._edge_rb is None:
            self._edge_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _C_EDGE, _style.RB_WIDTH
            )
        self._edge_rb.reset(QgsWkbTypes.LineGeometry)
        self._edge_rb.addPoint(a, False)
        self._edge_rb.addPoint(b, True)

    def _clear_edge_rb(self):
        if self._edge_rb is not None:
            self._edge_rb.reset(QgsWkbTypes.LineGeometry)

    def _update_preview(self, p1: QgsPointXY, p2: QgsPointXY):
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _C_PREVIEW, _style.RB_WIDTH
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        self._preview_rb.addPoint(p1, False)
        self._preview_rb.addPoint(p2, True)

    # ── reset / cancel ────────────────────────────────────────────────────

    def _reset(self):
        self._pts.clear()
        self._clear_rubber_bands()
        self._preview_rb = None
        self._edge_rb    = None

    def _on_cancel_hook(self):
        self._reset()

    def _on_undo_step(self):
        if self._pts:
            self._pts.pop()
            self._clear_rubber_bands()
            self._preview_rb = None
        else:
            self._do_cancel()

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)


# ── ADIM tool — entire feature ────────────────────────────────────────────────

class AutoDimensionTool(BaseTool):
    """ADIM — click on a feature to dimension all of its segments at once."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._hover_rb  = None
        self._dim_layer = None

    def activate(self):
        super().activate()
        self._dim_layer = _get_or_create_dim_layer(self._ctx)
        self._transition(ToolState.ACTING)
        self._log("ADIM — click a feature to dimension all its segments")
        self._request_input("no_value", "Click a feature to auto-dimension:")

        sel = getattr(self._ctx, 'selection_model', None)
        if sel and not sel.is_empty():
            for lid, fid in list(sel):
                layer = QgsProject.instance().mapLayer(lid)
                if layer:
                    feat = layer.getFeature(fid)
                    if feat.isValid():
                        self._dim_segments(feat)
            sel.clear()
            self._go_home()

    # ── events ────────────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            self._reset()
            self._go_home()
            return
        if sem.type not in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            return
        pt = sem.point
        if pt is None:
            return
        self._dim_feature_at(pt)

    def _on_hover(self, sem: SemanticEvent):
        pt = sem.point
        if pt is None:
            self._clear_hover()
            return
        _, feat = _find_feature_near(self._ctx, pt)
        if feat:
            geom = feat.geometry()
            if self._hover_rb is None:
                self._hover_rb = self._new_rubber_band(
                    QgsWkbTypes.LineGeometry, _C_EDGE, _style.RB_WIDTH
                )
            self._hover_rb.reset(QgsWkbTypes.LineGeometry)
            self._hover_rb.setToGeometry(geom)
        else:
            self._clear_hover()

    # ── dim entire feature ────────────────────────────────────────────────

    def _dim_segments(self, feat) -> bool:
        segs  = _segments_from_geom(feat.geometry())
        added = 0
        for a, b in segs:
            dist = _distance_m(a, b)
            geom = _project_to_layer_geom(a, b)
            if _add_dim_feature(self._dim_layer, geom, dist):
                added += 1
        total   = len(segs)
        skipped = total - added
        msg = f"ADIM: {added} dimension{'s' if added != 1 else ''} added"
        if skipped:
            msg += f", {skipped} already existed (skipped)"
        self._log(msg, "#aaffaa" if added else "#ffaa55")
        return added > 0

    def _dim_feature_at(self, pt: QgsPointXY):
        _, feat = _find_feature_near(self._ctx, pt)
        if feat is None:
            self._log("No feature found at click point.", "#ffaa55")
            return
        if self._dim_segments(feat):
            self._go_home()

    # ── reset / cancel ────────────────────────────────────────────────────

    def _clear_hover(self):
        if self._hover_rb is not None:
            self._hover_rb.reset(QgsWkbTypes.LineGeometry)

    def _reset(self):
        self._clear_rubber_bands()
        self._hover_rb = None

    def _on_cancel_hook(self):
        self._reset()

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)
