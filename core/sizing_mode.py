# -*- coding: utf-8 -*-
"""Plugin-wide sizing mode.

'points'   — text in pt, widths in mm  (screen-fixed, default)
'mapunits' — everything in map units / metres (scales with zoom)

The setting is persisted in QgsProject custom properties so it survives
project save/load.  Call get() / put() / toggle() to read or change it.
"""

from qgis.core import QgsProject, QgsUnitTypes

_GROUP = "ugsurv"
_ENTRY = "sizing_mode"

POINTS   = "points"
MAPUNITS = "mapunits"


def get() -> str:
    val, _ = QgsProject.instance().readEntry(_GROUP, _ENTRY, POINTS)
    return val if val in (POINTS, MAPUNITS) else POINTS


def put(mode: str):
    QgsProject.instance().writeEntry(_GROUP, _ENTRY, mode)


def is_mapunits() -> bool:
    return get() == MAPUNITS


def toggle() -> str:
    """Toggle between modes; return the new mode name."""
    new = MAPUNITS if get() == POINTS else POINTS
    put(new)
    return new


# ── QGIS unit enum helpers ────────────────────────────────────────────────────

def text_size_unit():
    return QgsUnitTypes.RenderMapUnits if is_mapunits() else QgsUnitTypes.RenderPoints


def text_buffer_unit():
    return QgsUnitTypes.RenderMapUnits if is_mapunits() else QgsUnitTypes.RenderMillimeters


def label_dist_unit():
    return QgsUnitTypes.RenderMapUnits if is_mapunits() else QgsUnitTypes.RenderMillimeters


def line_width_unit_str() -> str:
    """Unit string accepted by QgsLineSymbol.createSimple()."""
    return "MapUnit" if is_mapunits() else "MM"


def marker_size_unit():
    return QgsUnitTypes.RenderMapUnits if is_mapunits() else QgsUnitTypes.RenderMillimeters


# ── Default numeric values (depend on current mode) ───────────────────────────

def default_point_label_size() -> float:
    return 1.5 if is_mapunits() else 8.0


def default_dim_label_size() -> float:
    return 1.5 if is_mapunits() else 9.0


def default_area_label_size() -> float:
    return 2.0 if is_mapunits() else 10.0


def default_point_label_buffer() -> float:
    return 0.2 if is_mapunits() else 0.8


def default_dim_label_buffer() -> float:
    return 0.15 if is_mapunits() else 0.6


def default_label_dist() -> float:
    return 0.5 if is_mapunits() else 1.5


def default_line_width() -> float:
    return 0.05 if is_mapunits() else 0.35


def default_symbol_size() -> float:
    return 2.5  # same in both modes (mm or m)


# ── Properties panel spinner configuration ────────────────────────────────────

def thickness_props() -> dict:
    if is_mapunits():
        return {"suffix": " m",  "min": 0.001, "max": 50.0,   "step": 0.01, "decimals": 3, "default": 0.05}
    return     {"suffix": " mm", "min": 0.01,  "max": 10.0,   "step": 0.05, "decimals": 2, "default": 0.35}


def size_props() -> dict:
    if is_mapunits():
        return {"suffix": " m",  "min": 0.1, "max": 1000.0, "step": 0.1, "decimals": 2, "default": 2.5}
    return     {"suffix": " mm", "min": 0.1, "max": 500.0,  "step": 0.5, "decimals": 2, "default": 2.5}
