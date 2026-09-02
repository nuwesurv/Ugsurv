# -*- coding: utf-8 -*-
"""
Helpers for detecting and transforming circle features (QgsCircularString).

Circles are stored with 5 control points:
  p0: (cx+r, cy)   East  – start/end
  p1: (cx,   cy-r) South
  p2: (cx-r, cy)   West
  p3: (cx,   cy+r) North
  p4: (cx+r, cy)   East  – close
"""

import math

from qgis.core import (
    QgsGeometry, QgsPointXY, QgsPoint,
    QgsCircularString, QgsWkbTypes,
)


def is_circle(geom: QgsGeometry) -> bool:
    """Return True if geometry is a QgsCircularString (drawn by CircleDrawer)."""
    return QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.CircularString


def circle_params(geom: QgsGeometry) -> tuple[QgsPointXY, float]:
    """Extract (center, radius) from a CircularString circle geometry."""
    cs = geom.constGet()
    p0 = cs.pointN(0)   # East:  (cx+r, cy)
    p2 = cs.pointN(2)   # West:  (cx-r, cy)
    p1 = cs.pointN(1)   # South: (cx,   cy-r)
    p3 = cs.pointN(3)   # North: (cx,   cy+r)
    cx = (p0.x() + p2.x()) / 2
    cy = (p1.y() + p3.y()) / 2
    r  = math.hypot(p0.x() - cx, p0.y() - cy)
    return QgsPointXY(cx, cy), r


def build_circle_geom(center: QgsPointXY, radius: float) -> QgsGeometry:
    """Build a QgsCircularString circle geometry from center + radius."""
    cx, cy, r = center.x(), center.y(), radius
    cs = QgsCircularString()
    cs.setPoints([
        QgsPoint(cx + r, cy),
        QgsPoint(cx,     cy - r),
        QgsPoint(cx - r, cy),
        QgsPoint(cx,     cy + r),
        QgsPoint(cx + r, cy),
    ])
    return QgsGeometry(cs)


def _circle_attr_dict(cx: float, cy: float, radius: float) -> dict:
    """Derived attributes for a circle (matches circle_attrs in layer_utils)."""
    circumference = 2 * math.pi * radius
    area_sqm      = math.pi * radius ** 2
    return {
        "center_x":      round(cx, 6),
        "center_y":      round(cy, 6),
        "radius":        round(radius, 6),
        "diameter":      round(radius * 2, 6),
        "circumference": round(circumference, 6),
        "area_sqm":      round(area_sqm, 6),
        "area_acres":    round(area_sqm * 0.000247105, 8),
    }


def update_circle_attrs(layer, fid: int, center: QgsPointXY, radius: float) -> None:
    """Write the derived circle attributes back to a layer feature after a transform."""
    attrs = _circle_attr_dict(center.x(), center.y(), radius)
    fields = layer.fields()
    change = {}
    for name, val in attrs.items():
        idx = fields.indexOf(name)
        if idx >= 0:
            change[idx] = val
    if change:
        layer.changeAttributeValues(fid, change)


def set_circle_attrs_on_feature(feat, center: QgsPointXY, radius: float) -> None:
    """Set circle attribute values on a QgsFeature before it is added to a layer."""
    attrs = _circle_attr_dict(center.x(), center.y(), radius)
    for name, val in attrs.items():
        idx = feat.fields().indexOf(name)
        if idx >= 0:
            feat.setAttribute(idx, val)
