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
from ...core import sizing_mode as _sm

_TEXT_LAYER_NAME = "text"

# ── Rectangle constraint ──────────────────────────────────────────────────────
# Keeps strong references to per-layer closures so Qt doesn't GC them.
_RECT_CORRECTORS: "dict[str, object]" = {}
# Pre-edit corner cache: {layer_id: {fid: [pt0, pt1, pt2, pt3]}}
# Populated on editingStarted so _enforce knows which corner is the fixed opposite.
_PRE_EDIT_CORNERS: "dict[str, dict]" = {}


def _connect_rect_constraint(lyr: "QgsVectorLayer"):
    """Connect a geometryChanged handler that forces text-box polygons to stay
    axis-aligned rectangles using the opposite-corner algorithm.

    On editingStarted the pre-edit corners are cached per fid.  When
    geometryChanged fires, the handler:
      1. Finds which corner moved the most vs the cached pre-edit corners.
      2. Fixes the opposite corner (index +2 mod 4) to its pre-edit position.
      3. Commits QgsGeometry.fromRect(QgsRectangle(moved_pt, fixed_opposite)).

    Falls back to the bounding-box approach when no cache entry is available
    (e.g. first edit after plugin load without an editingStarted signal).

    Safe to call multiple times — only wires once per layer."""
    if lyr.id() in _RECT_CORRECTORS:
        return

    _guard = [False]   # mutable flag shared with closure

    def _on_editing_started():
        corners = {}
        for feat in lyr.getFeatures():
            ring = feat.geometry().asPolygon()
            if ring and len(ring[0]) >= 5:
                corners[feat.id()] = list(ring[0][:4])
        _PRE_EDIT_CORNERS[lyr.id()] = corners

    def _enforce(fid: int, geom: "QgsGeometry"):
        if _guard[0]:
            return
        ring = geom.asPolygon()
        if not ring:
            return
        pts = ring[0]
        xs = {round(p.x(), 4) for p in pts}
        ys = {round(p.y(), 4) for p in pts}
        # Already a valid axis-aligned rectangle (4 unique corners + closing pt)
        if len(pts) == 5 and len(xs) == 2 and len(ys) == 2:
            # Update the cache with the newly committed corners so future
            # edits start from the correct pre-edit state.
            layer_cache = _PRE_EDIT_CORNERS.get(lyr.id())
            if layer_cache is not None:
                layer_cache[fid] = list(pts[:4])
            return
        new_corners = list(pts[:4])
        pre_corners = _PRE_EDIT_CORNERS.get(lyr.id(), {}).get(fid)
        if pre_corners and len(pre_corners) == 4 and len(new_corners) == 4:
            # Identify grabbed vertex as the one with the largest displacement.
            def _d2(i):
                dx = new_corners[i].x() - pre_corners[i].x()
                dy = new_corners[i].y() - pre_corners[i].y()
                return dx * dx + dy * dy
            grabbed_idx  = max(range(4), key=_d2)
            fixed_opp    = pre_corners[(grabbed_idx + 2) % 4]
            moved_pt     = new_corners[grabbed_idx]
            rect = QgsRectangle(moved_pt.x(), moved_pt.y(),
                                fixed_opp.x(), fixed_opp.y())
        else:
            rect = geom.boundingBox()
        if rect.isNull() or rect.isEmpty():
            return
        _guard[0] = True
        lyr.changeGeometry(fid, QgsGeometry.fromRect(rect))
        _guard[0] = False

    def _cleanup():
        _RECT_CORRECTORS.pop(lyr.id(), None)
        _PRE_EDIT_CORNERS.pop(lyr.id(), None)

    _RECT_CORRECTORS[lyr.id()] = (_enforce, _on_editing_started)
    lyr.geometryChanged.connect(_enforce)
    lyr.editingStarted.connect(_on_editing_started)
    lyr.willBeDeleted.connect(_cleanup)
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

_DEFAULT_UNIT     = ("ha", 1e-4)   # Enter with empty field → ha
_DEFAULT_DECIMALS = 3

# Step identifiers
_STEP_UNITS    = "units"
_STEP_DECIMALS = "decimals"
_STEP_PICKING  = "picking"


# ── Layer management (shared with text_tool.py) ───────────────────────────────

def _get_or_create_text_layer(ctx) -> "QgsVectorLayer | None":
    """Return the text (Polygon) layer, migrating a legacy Point layer if needed.

    The layer is intentionally left out of edit mode so QGIS's built-in node
    tool cannot modify individual vertices.  Each commit method calls
    startEditing / commitChanges itself.
    """
    found = QgsProject.instance().mapLayersByName(_TEXT_LAYER_NAME)
    if found:
        lyr = found[0]
        if lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
            if lyr.isEditable():
                lyr.commitChanges()
            _ensure_fields(lyr)
            _connect_rect_constraint(lyr)
            return lyr
        # Legacy Point layer — remove so we can recreate as Polygon
        if found[0].isEditable():
            found[0].rollBack()
        QgsProject.instance().removeMapLayer(lyr.id())

    gpkg = getattr(ctx.storage_manager, "gpkg_path", None)
    if gpkg and os.path.exists(gpkg):
        uri = f"{gpkg}|layername={_TEXT_LAYER_NAME}"
        lyr = QgsVectorLayer(uri, _TEXT_LAYER_NAME, "ogr")
        if lyr.isValid() and lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
            _register_layer(lyr)
            _ensure_fields(lyr)
            _apply_text_style(lyr)
            _connect_rect_constraint(lyr)
            return lyr
        # Table missing or wrong type — overwrite with Polygon table
        _add_text_table_to_gpkg(gpkg)
        lyr = QgsVectorLayer(f"{gpkg}|layername={_TEXT_LAYER_NAME}", _TEXT_LAYER_NAME, "ogr")
        if lyr.isValid():
            _register_layer(lyr)
            _apply_text_style(lyr)
            _connect_rect_constraint(lyr)
            return lyr

    mem = QgsVectorLayer(f"Polygon?crs={_LAYER_CRS_AUTH}", _TEXT_LAYER_NAME, "memory")
    mem.dataProvider().addAttributes([
        QgsField("label_text", QVariant.String),
        QgsField("cad_layer",  QVariant.String),
    ])
    mem.updateFields()
    _register_layer(mem)
    _apply_text_style(mem)
    _connect_rect_constraint(mem)
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
    tmp = QgsVectorLayer(f"Polygon?crs={_LAYER_CRS_AUTH}", _TEXT_LAYER_NAME, "memory")
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
    """Invisible polygon (text box) with Open Sans Bold label at centroid."""
    layer.setRenderer(QgsNullSymbolRenderer())

    fmt = QgsTextFormat()
    font = QFont("Open Sans")
    font.setBold(True)
    fmt.setFont(font)
    fmt.setSize(_sm.default_area_label_size())
    fmt.setSizeUnit(_sm.text_size_unit())
    fmt.setColor(QColor(30, 30, 30))

    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(1.0)
    buf.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    buf.setColor(QColor(255, 255, 255, 230))
    fmt.setBuffer(buf)

    pal = QgsPalLayerSettings()
    pal.isExpression     = False
    pal.fieldName        = "label_text"
    pal.placement        = QgsPalLayerSettings.Horizontal
    pal.fitInPolygonOnly = False
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
        self._log("AREA — M=m²  H=ha  K=km²  A=ac  F=ft²  (Enter = ha)")
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
            if self._write_label(feat.geometry(), src.crs(), label):
                added += 1
            else:
                skipped += 1
        return added, skipped

    def _write_label(self, geom: QgsGeometry,
                     src_crs: QgsCoordinateReferenceSystem, label: str) -> bool:
        centroid = geom.centroid()
        if centroid.isNull() or centroid.isEmpty():
            return False
        layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        if src_crs != layer_crs:
            centroid.transform(
                QgsCoordinateTransform(src_crs, layer_crs, QgsProject.instance())
            )
        canvas = getattr(self._ctx, "canvas", None)
        mup    = canvas.mapUnitsPerPixel() if canvas else 1.0
        pt     = centroid.asPoint()
        hw, hh = 60 * mup, 15 * mup
        box    = QgsGeometry.fromRect(
            QgsRectangle(pt.x() - hw, pt.y() - hh, pt.x() + hw, pt.y() + hh)
        )
        if not self._text_layer.isEditable():
            if not self._text_layer.startEditing():
                return False
        f = QgsFeature(self._text_layer.fields())
        f.setGeometry(box)
        f.setAttribute("label_text", label)
        f.setAttribute("cad_layer", "area")
        ok = self._text_layer.addFeature(f)
        if not ok:
            return False
        self._text_layer.commitChanges()
        canvas = getattr(self._ctx, "canvas", None)
        if canvas:
            canvas.refresh()
        return True

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
