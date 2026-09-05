# -*- coding: utf-8 -*-
"""
Renderer helpers shared across the plugin.

apply_point_color_renderer  — builds a rule-based renderer for the _points
    layer: null/empty Symbol → basic white circle;
           non-empty Symbol  → SVG marker using the path stored in Symbol.

The other apply_* functions are stubs kept so callers compile while those
renderers are added later.
"""

import os

from qgis.core import (
    QgsExpression,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsProperty,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsSvgMarkerSymbolLayer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor, QFont


def apply_point_color_renderer(layer) -> None:
    """Rebuild the points layer renderer.

    SVG rules are kept for features with a non-null Symbol path.
    The else rule drives shape/color/size from the symbol/color/symbol_size
    feature fields so each point renders according to its own attributes.
    """
    root = QgsRuleBasedRenderer.Rule(None)

    svg_paths = set()
    sym_fld_idx = layer.fields().lookupField("symbol")
    for feat in layer.getFeatures():
        val = feat.attribute(sym_fld_idx) if sym_fld_idx >= 0 else None
        if val and isinstance(val, str) and val.lower().endswith(".svg"):
            svg_paths.add(val)

    for path in sorted(svg_paths):
        svg_sl = QgsSvgMarkerSymbolLayer(path, 8.0)
        svg_sl.setDataDefinedProperty(
            QgsSymbolLayer.PropertySize,
            QgsProperty.fromExpression("coalesce(\"symbol_size\", 8.0)"),
        )
        svg_sym = QgsMarkerSymbol()
        svg_sym.changeSymbolLayer(0, svg_sl)
        rule = QgsRuleBasedRenderer.Rule(svg_sym)
        rule.setLabel(os.path.splitext(os.path.basename(path))[0])
        rule.setFilterExpression(f'"symbol" = {QgsExpression.quotedString(path)}')
        root.appendChild(rule)

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
        QgsProperty.fromExpression("coalesce(nullif(\"symbol\", 'basic'), 'circle')"),
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


def apply_point_label_style(layer) -> None:
    """Label points with the Description field: Open Sans 8pt Bold Italic, 0.8 mm white buffer."""
    fmt = QgsTextFormat()
    font = QFont("Open Sans", 8)
    font.setBold(True)
    font.setItalic(True)
    fmt.setFont(font)
    fmt.setSize(8)
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    fmt.setColor(QColor(50, 50, 50, 255))

    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    buf.setColor(QColor(250, 250, 250, 255))
    buf.setFillBufferInterior(False)
    fmt.setBuffer(buf)

    pal = QgsPalLayerSettings()
    pal.fieldName = "Description"
    pal.isExpression = False
    pal.setFormat(fmt)

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


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


def apply_circle_color_renderer(layer) -> None:
    pass


def apply_hatch_renderer(layer) -> None:
    pass


def apply_dimension_style(layer) -> None:
    pass
