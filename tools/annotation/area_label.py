# -*- coding: utf-8 -*-
"""
AreaLabelTool (AREA / AA).

Computes the area of every selected polygon/polyline feature and places a
text label at its centroid on the text layer.

Flow:
  1. Ask for unit  (m²/ha/km²/ac/ft²  — Enter accepts default m²).
  2. Ask for decimal places  (0-6  — Enter accepts default 2).
  3a. If selection is non-empty → label all, go home.
  3b. Otherwise → click features one at a time; Enter/Esc to finish.

Commands: AREA  AA
"""

import os

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsNullSymbolRenderer,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType

_TEXT_LAYER_NAME = "text"
_LAYER_EPSG      = 32636
_LAYER_CRS_AUTH  = f"EPSG:{_LAYER_EPSG}"

# Abbreviation → (display label, multiplier from m²).
# Accepts full abbreviations and single-letter shortcuts (case-insensitive).
_UNIT_MAP = {
    "m": ("m²", 1.0), "m2": ("m²", 1.0), "m²": ("m²", 1.0),
    "h": ("ha", 1e-4), "ha": ("ha", 1e-4),
    "k": ("km²", 1e-6), "km2": ("km²", 1e-6), "km²": ("km²", 1e-6),
    "a": ("ac", 1.0 / 4046.8564), "ac": ("ac", 1.0 / 4046.8564),
    "f": ("ft²", 10.7639104), "ft2": ("ft²", 10.7639104), "ft²": ("ft²", 10.7639104),
}

_DEFAULT_UNIT     = ("m²", 1.0)   # Enter with empty field → m²
_DEFAULT_DECIMALS = 3

# Step identifiers
_STEP_UNITS    = "units"
_STEP_DECIMALS = "decimals"
_STEP_PICKING  = "picking"


# ── Layer management (shared with text_tool.py) ───────────────────────────────

def _get_or_create_text_layer(ctx) -> "QgsVectorLayer | None":
    """Return the text layer, creating it on first use."""
    found = QgsProject.instance().mapLayersByName(_TEXT_LAYER_NAME)
    if found:
        lyr = found[0]
        _ensure_fields(lyr)
        if not lyr.isEditable():
            lyr.startEditing()
        return lyr

    gpkg = getattr(ctx.storage_manager, "gpkg_path", None)
    if gpkg and os.path.exists(gpkg):
        uri = f"{gpkg}|layername={_TEXT_LAYER_NAME}"
        lyr = QgsVectorLayer(uri, _TEXT_LAYER_NAME, "ogr")
        if lyr.isValid():
            _register_layer(lyr)
            _ensure_fields(lyr)
            _apply_text_style(lyr)
            if not lyr.isEditable():
                lyr.startEditing()
            return lyr
        _add_text_table_to_gpkg(gpkg)
        lyr = QgsVectorLayer(f"{gpkg}|layername={_TEXT_LAYER_NAME}", _TEXT_LAYER_NAME, "ogr")
        if lyr.isValid():
            _register_layer(lyr)
            _apply_text_style(lyr)
            if not lyr.isEditable():
                lyr.startEditing()
            return lyr

    mem = QgsVectorLayer(f"Point?crs={_LAYER_CRS_AUTH}", _TEXT_LAYER_NAME, "memory")
    mem.dataProvider().addAttributes([
        QgsField("label_text", QVariant.String),
        QgsField("cad_layer",  QVariant.String),
    ])
    mem.updateFields()
    _register_layer(mem)
    _apply_text_style(mem)
    mem.startEditing()
    return mem


def _ensure_fields(layer: QgsVectorLayer):
    existing = {f.name() for f in layer.fields()}
    attrs = []
    if "label_text" not in existing:
        attrs.append(QgsField("label_text", QVariant.String))
    if "cad_layer" not in existing:
        attrs.append(QgsField("cad_layer", QVariant.String))
    if attrs:
        layer.dataProvider().addAttributes(attrs)
        layer.updateFields()


def _add_text_table_to_gpkg(gpkg: str):
    tmp = QgsVectorLayer(f"Point?crs={_LAYER_CRS_AUTH}", _TEXT_LAYER_NAME, "memory")
    tmp.dataProvider().addAttributes([
        QgsField("label_text", QVariant.String),
        QgsField("cad_layer",  QVariant.String),
    ])
    tmp.updateFields()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName           = "GPKG"
    opts.layerName            = _TEXT_LAYER_NAME
    opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
    QgsVectorFileWriter.writeAsVectorFormatV3(
        tmp, gpkg, QgsProject.instance().transformContext(), opts
    )


def _register_layer(layer: QgsVectorLayer):
    root  = QgsProject.instance().layerTreeRoot()
    group = root.findGroup("Ugsurv") or root.insertGroup(0, "Ugsurv")
    QgsProject.instance().addMapLayer(layer, False)
    group.addLayer(layer)


def _apply_text_style(layer: QgsVectorLayer):
    """Invisible point + Open Sans Bold label centred on the point."""
    layer.setRenderer(QgsNullSymbolRenderer())

    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.5)
    buf.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    buf.setColor(QColor(255, 255, 255))

    fmt = QgsTextFormat()
    font = QFont("Open Sans")
    font.setBold(True)
    fmt.setFont(font)
    fmt.setSize(10)
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    fmt.setColor(QColor(30, 30, 30))
    fmt.setBuffer(buf)

    pal = QgsPalLayerSettings()
    pal.isExpression = False
    pal.fieldName    = "label_text"
    pal.placement    = QgsPalLayerSettings.OverPoint
    pal.quadOffset   = QgsPalLayerSettings.QuadrantOver
    pal.setFormat(fmt)

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _line_to_polygon(geom: QgsGeometry) -> QgsGeometry:
    """Close an open or closed polyline into a polygon for area measurement."""
    if geom.isMultipart():
        rings = geom.asMultiPolyline()
    else:
        rings = [geom.asPolyline()]
    closed = []
    for ring in rings:
        if len(ring) < 2:
            continue
        r = list(ring)
        if r[0] != r[-1]:
            r.append(r[0])
        closed.append(r)
    if not closed:
        return QgsGeometry()
    if len(closed) == 1:
        return QgsGeometry.fromPolygonXY([closed[0]])
    return QgsGeometry.fromMultiPolygonXY([[r] for r in closed])


def _area_label_text(geom: QgsGeometry, src_layer: QgsVectorLayer,
                     unit_label: str, unit_factor: float, decimals: int) -> "str | None":
    """Return formatted area string for a polygon/line geometry, or None if invalid."""
    gt = QgsWkbTypes.geometryType(geom.wkbType())
    if gt == QgsWkbTypes.LineGeometry:
        geom = _line_to_polygon(geom)
        if geom.isNull() or geom.isEmpty():
            return None
    elif gt != QgsWkbTypes.PolygonGeometry:
        return None
    da = QgsDistanceArea()
    da.setSourceCrs(src_layer.crs(), QgsProject.instance().transformContext())
    da.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
    area_m2 = da.convertAreaMeasurement(
        da.measureArea(geom), QgsUnitTypes.AreaSquareMeters
    )
    if abs(area_m2) < 1e-6:
        return None
    return f"{area_m2 * unit_factor:.{decimals}f} {unit_label}"


def _find_feature_near(ctx, map_pt: QgsPointXY):
    """Return (layer, feature) of the nearest polygon or line feature."""
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
        if lyr.name() == _TEXT_LAYER_NAME:
            continue
        if int(lyr.geometryType()) not in (
            QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry
        ):
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


# ── Tool ──────────────────────────────────────────────────────────────────────

class AreaLabelTool(BaseTool):
    """AREA / AA — label selected features with their area at the centroid."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._text_layer  = None
        self._step        = _STEP_UNITS
        self._unit_label  = _DEFAULT_UNIT[0]
        self._unit_factor = _DEFAULT_UNIT[1]
        self._decimals    = _DEFAULT_DECIMALS

    def activate(self):
        super().activate()
        self._text_layer  = _get_or_create_text_layer(self._ctx)
        self._unit_label  = _DEFAULT_UNIT[0]
        self._unit_factor = _DEFAULT_UNIT[1]
        self._decimals    = _DEFAULT_DECIMALS
        self._transition(ToolState.ACTING)
        self._ask_units()

    # ── step prompts ──────────────────────────────────────────────────────

    def _ask_units(self):
        self._step = _STEP_UNITS
        self._log("AREA — M=m²  H=ha  K=km²  A=ac  F=ft²  (Enter = m²)")
        self._request_input("unit", "Unit:")

    def _ask_decimals(self):
        self._step = _STEP_DECIMALS
        self._request_input("integer", "Decimal places:")

    def _start_picking(self):
        sel = getattr(self._ctx, "selection_model", None)
        if sel and not sel.is_empty():
            added, skipped = self._label_selection(sel)
            sel.clear()
            self._report(added, skipped)
            self._go_home()
            return
        self._step = _STEP_PICKING
        self._log("Click a polygon or polyline — Enter to finish")
        self._request_input("no_value", "Click feature:")

    # ── events ────────────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if self._step == _STEP_UNITS:
            self._handle_units(sem)
        elif self._step == _STEP_DECIMALS:
            self._handle_decimals(sem)
        elif self._step == _STEP_PICKING:
            self._handle_picking(sem)

    def _handle_units(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            self._ask_decimals()
            return
        if sem.type == EventType.VALUE_ENTERED:
            key = str(sem.value).strip().lower()
            entry = _UNIT_MAP.get(key)
            if entry is None:
                self._log(f"Unknown unit '{sem.value}'. Try m2/ha/km2/ac/ft2.", "#ffaa55")
                return
            self._unit_label, self._unit_factor = entry
            self._ask_decimals()

    def _handle_decimals(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            self._start_picking()
            return
        if sem.type == EventType.VALUE_ENTERED:
            try:
                self._decimals = max(0, min(6, int(float(str(sem.value)))))
            except (TypeError, ValueError):
                self._log("Enter a number 0-6 or press Enter for default (3).", "#ffaa55")
                return
            self._start_picking()

    def _handle_picking(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            self._go_home()
            return
        if sem.type not in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            return
        pt = sem.point
        if pt is None:
            return
        lyr, feat = _find_feature_near(self._ctx, pt)
        if feat is None:
            self._log("No polygon/polyline found at click point.", "#ffaa55")
            return
        label = _area_label_text(feat.geometry(), lyr,
                                 self._unit_label, self._unit_factor, self._decimals)
        if label is None:
            self._log("Feature has no measurable area.", "#ffaa55")
            return
        self._write_label(feat.geometry(), lyr.crs(), label)
        self._log(f"Area: {label}", "#aaffaa")

    # ── label all selected ────────────────────────────────────────────────

    def _label_selection(self, sel) -> "tuple[int, int]":
        added   = 0
        skipped = 0
        for lid, fid in list(sel):
            src = QgsProject.instance().mapLayer(lid)
            if not src or not isinstance(src, QgsVectorLayer):
                skipped += 1
                continue
            feat = src.getFeature(fid)
            if not feat.isValid():
                skipped += 1
                continue
            label = _area_label_text(feat.geometry(), src,
                                     self._unit_label, self._unit_factor, self._decimals)
            if label is None:
                skipped += 1
                continue
            self._write_label(feat.geometry(), src.crs(), label)
            added += 1
        return added, skipped

    def _write_label(self, geom: QgsGeometry,
                     src_crs: QgsCoordinateReferenceSystem, label: str):
        centroid  = geom.centroid()
        layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        if src_crs != layer_crs:
            centroid.transform(
                QgsCoordinateTransform(src_crs, layer_crs, QgsProject.instance())
            )
        if not self._text_layer.isEditable():
            self._text_layer.startEditing()
        f = QgsFeature(self._text_layer.fields())
        f.setGeometry(centroid)
        f["label_text"] = label
        f["cad_layer"]  = getattr(self._ctx, "active_cad_layer", "0")
        self._text_layer.addFeature(f)
        self._text_layer.triggerRepaint()

    # ── undo step ─────────────────────────────────────────────────────────

    def _on_undo_step(self):
        if self._step == _STEP_DECIMALS:
            self._ask_units()
        elif self._step == _STEP_PICKING:
            self._ask_decimals()
        else:
            self._go_home()

    # ── reporting / cancel ────────────────────────────────────────────────

    def _report(self, added: int, skipped: int):
        msg = f"AREA: {added} label{'s' if added != 1 else ''} placed."
        if skipped:
            msg += f"  {skipped} skipped (not polygon or zero area)."
        self._log(msg, "#aaffaa" if added else "#ffaa55")

    def _on_cancel_hook(self):
        self._step = _STEP_UNITS

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, "cmd_dock", None)
        if dock:
            dock.log(msg, color)
