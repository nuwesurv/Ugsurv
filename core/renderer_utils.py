# -*- coding: utf-8 -*-
"""
Renderer helpers shared across the plugin.

apply_point_color_renderer  — single-symbol SVG renderer for the points layer.
    PropertyName is driven by the 'symbol' field; NULL/empty/'basic' features
    fall back to map_icons/basic.svg.  Color and size come from feature fields.

apply_polyline_color_renderer / apply_circle_color_renderer — single-symbol
    data-driven renderers; color, line_type, and line_thickness come from fields.
"""

import os

from qgis.core import (
    QgsExpression,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsProperty,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsSvgMarkerSymbolLayer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor, QFont

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASIC_CIRCLE_SVG = os.path.join(_PLUGIN_DIR, "map_icons", "basic.svg")


def apply_point_color_renderer(layer) -> None:
    """Single-symbol data-driven renderer for the points layer.

    One QgsSvgMarkerSymbolLayer covers all features. PropertyName resolves
    to the SVG path stored in the 'symbol' field; NULL / empty / 'basic'
    features fall back to the built-in basic.svg circle.  Color and size
    come from the 'color' and 'symbol_size' fields respectively.
    """
    svg_sl = QgsSvgMarkerSymbolLayer(_BASIC_CIRCLE_SVG, 8.0)
    svg_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyName,
        QgsProperty.fromExpression(
            f"coalesce(nullif(nullif(\"symbol\", ''), 'basic'), "
            f"{QgsExpression.quotedString(_BASIC_CIRCLE_SVG)})"
        ),
    )
    svg_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyFillColor,
        QgsProperty.fromExpression("coalesce(\"color\", '#ffffff')"),
    )
    svg_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertySize,
        QgsProperty.fromExpression("coalesce(\"symbol_size\", 8.0)"),
    )
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, svg_sl)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


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
    pal.placement = QgsPalLayerSettings.AroundPoint
    pal.dist = 1.5
    pal.distUnits = QgsUnitTypes.RenderMillimeters

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def apply_polyline_color_renderer(layer) -> None:
    """Data-driven renderer for the lines layer.

    Color, line style, and width are read from the color, line_type, and
    line_thickness feature attributes at draw time.
    """
    sym = QgsLineSymbol.createSimple({
        "color":           "#64cb53",
        "line_style":      "solid",
        "line_width":      "0.35",
        "line_width_unit": "MM",
        "joinstyle":       "bevel",
        "capstyle":        "square",
    })
    sl = sym.symbolLayer(0)
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeColor,
        QgsProperty.fromExpression("coalesce(\"color\", '#64cb53')"),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeStyle,
        QgsProperty.fromExpression("coalesce(\"line_type\", 'solid')"),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeWidth,
        QgsProperty.fromExpression("coalesce(\"line_thickness\", 0.35)"),
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


def apply_circle_color_renderer(layer) -> None:
    """Data-driven renderer for the circles layer.

    Color, line style, and width are read from the color, line_type, and
    line_thickness feature attributes at draw time.
    """
    sym = QgsLineSymbol.createSimple({
        "color":           "#64cb53",
        "line_style":      "solid",
        "line_width":      "0.35",
        "line_width_unit": "MM",
        "joinstyle":       "bevel",
        "capstyle":        "square",
    })
    sl = sym.symbolLayer(0)
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeColor,
        QgsProperty.fromExpression("coalesce(\"color\", '#64cb53')"),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeStyle,
        QgsProperty.fromExpression("coalesce(\"line_type\", 'solid')"),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeWidth,
        QgsProperty.fromExpression("coalesce(\"line_thickness\", 0.35)"),
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


def apply_hatch_renderer(layer) -> None:
    pass


def apply_dimension_style(layer) -> None:
    pass
