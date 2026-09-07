# -*- coding: utf-8 -*-
"""
Renderer helpers shared across the plugin.

apply_point_color_renderer  — single-symbol SVG renderer for the points layer.
    PropertyName is driven by the 'symbol' field; NULL/empty/'basic' features
    fall back to map_icons/basic.svg.  Color and size come from feature fields.

apply_polyline_color_renderer / apply_circle_color_renderer — single-symbol
    data-driven renderers; color, line_type, and line_thickness come from fields.

All sizing respects the current plugin sizing_mode ('points' or 'mapunits').
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

from . import sizing_mode as _sm

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASIC_CIRCLE_SVG = os.path.join(_PLUGIN_DIR, "map_icons", "point_icons", "a_point4.svg")


def apply_point_color_renderer(layer) -> None:
    """Single-symbol data-driven renderer for the points layer.

    One QgsSvgMarkerSymbolLayer covers all features. PropertyName resolves
    to the SVG path stored in the 'symbol' field; NULL / empty / 'basic'
    features fall back to the built-in basic.svg circle.  Color and size
    come from the 'color' and 'symbol_size' fields respectively.
    """
    default_sz = _sm.default_symbol_size()
    svg_sl = QgsSvgMarkerSymbolLayer(_BASIC_CIRCLE_SVG, default_sz)
    svg_sl.setSizeUnit(_sm.marker_size_unit())
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
        QgsProperty.fromExpression(f"coalesce(\"symbol_size\", {default_sz})"),
    )
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, svg_sl)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


def apply_point_label_style(layer) -> None:
    """Label points with the Description field using the current sizing mode."""
    fmt = QgsTextFormat()
    font = QFont("Open Sans")
    font.setBold(True)
    font.setItalic(True)
    fmt.setFont(font)
    fmt.setSize(_sm.default_point_label_size())
    fmt.setSizeUnit(_sm.text_size_unit())
    fmt.setColor(QColor(50, 50, 50, 255))

    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(_sm.default_point_label_buffer())
    buf.setSizeUnit(_sm.text_buffer_unit())
    buf.setColor(QColor(250, 250, 250, 255))
    buf.setFillBufferInterior(False)
    fmt.setBuffer(buf)

    pal = QgsPalLayerSettings()
    pal.fieldName = "Description"
    pal.isExpression = False
    pal.setFormat(fmt)
    pal.placement = QgsPalLayerSettings.AroundPoint
    pal.dist = _sm.default_label_dist()
    pal.distUnits = _sm.label_dist_unit()

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def apply_polyline_color_renderer(layer) -> None:
    """Data-driven renderer for the lines layer.

    Color, line style, and width are read from the color, line_type, and
    line_thickness feature attributes at draw time.
    Width unit follows the current sizing mode (mm or map units).
    """
    default_w = _sm.default_line_width()
    sym = QgsLineSymbol.createSimple({
        "color":           "#64cb53",
        "line_style":      "solid",
        "line_width":      str(default_w),
        "line_width_unit": _sm.line_width_unit_str(),
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
        QgsProperty.fromExpression(f"coalesce(\"line_thickness\", {default_w})"),
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


def apply_circle_color_renderer(layer) -> None:
    """Data-driven renderer for the circles layer.

    Color, line style, and width are read from the color, line_type, and
    line_thickness feature attributes at draw time.
    Width unit follows the current sizing mode (mm or map units).
    """
    default_w = _sm.default_line_width()
    sym = QgsLineSymbol.createSimple({
        "color":           "#64cb53",
        "line_style":      "solid",
        "line_width":      str(default_w),
        "line_width_unit": _sm.line_width_unit_str(),
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
        QgsProperty.fromExpression(f"coalesce(\"line_thickness\", {default_w})"),
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


def apply_hatch_renderer(layer) -> None:
    pass


def apply_dimension_style(layer) -> None:
    pass
