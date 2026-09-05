# -*- coding: utf-8 -*-
"""
Renderer helpers used by PropertiesDock.

apply_point_color_renderer  — builds a rule-based renderer for the _points
    layer:  null/empty Symbol → basic white circle;
            non-empty Symbol  → SVG marker using the path stored in Symbol.

The other apply_* functions are stubs kept so the import block in
properties_panel.py stays intact while those renderers are added later.
"""

import math
import os

from qgis.core import (
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsProperty,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsSvgMarkerSymbolLayer,
)


# ── re-export so properties_panel.py can import circle_attrs from here ────────

def circle_attrs(cx: float, cy: float, radius: float) -> dict:
    circumference = 2 * math.pi * radius
    area_sqm      = math.pi * radius ** 2
    return {
        "center_x":      round(cx, 6),
        "center_y":      round(cy, 6),
        "radius":        round(radius, 6),
        "circumference": round(circumference, 6),
        "area_sqm":      round(area_sqm, 6),
        "area_acres":    round(area_sqm * 0.000247105, 8),
    }


# ── point layer renderer ──────────────────────────────────────────────────────

def apply_point_color_renderer(layer) -> None:
    """Rebuild the points layer renderer.

    SVG rules are kept for features with a non-null Symbol path.
    The else rule drives shape/color/size from the symbol/color/symbol_size
    feature fields so each point renders according to its own attributes.
    """
    root = QgsRuleBasedRenderer.Rule(None)

    # Collect unique SVG paths currently stored in the layer
    svg_paths = set()
    for feat in layer.getFeatures():
        val = feat["Symbol"]
        if val and val != "basic" and isinstance(val, str):
            svg_paths.add(val)

    # One explicit rule per unique SVG path
    for path in sorted(svg_paths):
        escaped = path.replace("'", "''")
        svg_sl  = QgsSvgMarkerSymbolLayer(path, 8.0)
        svg_sym = QgsMarkerSymbol()
        svg_sym.changeSymbolLayer(0, svg_sl)
        rule = QgsRuleBasedRenderer.Rule(svg_sym)
        rule.setLabel(os.path.splitext(os.path.basename(path))[0])
        rule.setFilterExpression(f'"Symbol" = \'{escaped}\'')
        root.appendChild(rule)

    # Else rule — shape/color/size driven by feature attributes
    basic_sym = QgsMarkerSymbol.createSimple({
        "name":               "circle",
        "color":              "255,255,255,255",
        "outline_color":      "47,47,47,255",
        "outline_width":      "0.6",
        "outline_width_unit": "MM",
        "size":               "2.0",
        "size_unit":          "MM",
    })
    sl = basic_sym.symbolLayer(0)
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyName,
        QgsProperty.fromExpression("coalesce(\"symbol\", 'circle')"),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyFillColor,
        QgsProperty.fromExpression("coalesce(\"color\", '#ffffff')"),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertySize,
        QgsProperty.fromExpression("coalesce(\"symbol_size\", 2.0)"),
    )

    rule_else = QgsRuleBasedRenderer.Rule(basic_sym)
    rule_else.setLabel("symbol")
    rule_else.setIsElse(True)
    root.appendChild(rule_else)

    layer.setRenderer(QgsRuleBasedRenderer(root))


# ── stubs for other layer types (implemented elsewhere / not yet needed) ──────

def apply_circle_color_renderer(layer) -> None:
    pass


def apply_polyline_color_renderer(layer) -> None:
    """Apply the basic line style: solid green, 0.35 mm (matches the shipped QML)."""
    sym = QgsLineSymbol.createSimple({
        "color":           "100,203,83,255",
        "line_style":      "solid",
        "line_width":      "0.35",
        "line_width_unit": "MM",
        "joinstyle":       "bevel",
        "capstyle":        "square",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


def apply_hatch_renderer(layer) -> None:
    pass


def apply_dimension_style(layer) -> None:
    pass
