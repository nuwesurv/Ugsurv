# -*- coding: utf-8 -*-
"""Shared geometry attribute helpers used by multiple tools."""

import math
from qgis.core import QgsGeometry, QgsPointXY


def polyline_attrs(geom: QgsGeometry) -> dict:
    """Return a dict of computed attributes for a polyline geometry."""
    length = geom.length() if not geom.isEmpty() else 0.0
    pts = geom.asPolyline() if not geom.isEmpty() else []
    is_closed = (
        len(pts) >= 4
        and abs(pts[0].x() - pts[-1].x()) < 1e-9
        and abs(pts[0].y() - pts[-1].y()) < 1e-9
    )
    if is_closed and len(pts) >= 3:
        area_sqm = QgsGeometry.fromPolygonXY([list(pts)]).area()
    else:
        area_sqm = 0.0
    area_acres = area_sqm * 0.000247105
    return {
        "length":     round(length, 4),
        "closed":     is_closed,
        "area_sqm":   round(area_sqm, 4),
        "area_acres": round(area_acres, 8),
    }


def circle_attrs(cx: float, cy: float, radius: float) -> dict:
    """Return a dict of computed attributes for a circle."""
    circumference = 2 * math.pi * radius
    area_sqm      = math.pi * radius ** 2
    return {
        "center_x":     round(cx, 6),
        "center_y":     round(cy, 6),
        "radius":       round(radius, 6),
        "circumference": round(circumference, 6),
        "area_sqm":     round(area_sqm, 6),
        "area_acres":   round(area_sqm * 0.000247105, 8),
    }
