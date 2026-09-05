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
    QgsCircularString, QgsCompoundCurve, QgsWkbTypes,
)


def is_circle(geom: QgsGeometry) -> bool:
    """Return True if geometry is a circle — bare CircularString or CompoundCurve(CircularString).

    Circles are drawn as CircularString but the circles layer is CompoundCurve type,
    so QGIS wraps them in CompoundCurve on write and returns CompoundCurve on read.
    Both forms must be detected.
    """
    flat = QgsWkbTypes.flatType(geom.wkbType())
    if flat == QgsWkbTypes.CircularString:
        return True
    if flat == QgsWkbTypes.CompoundCurve:
        cc = geom.constGet()
        return (cc.nCurves() == 1 and
                QgsWkbTypes.flatType(cc.curveAt(0).wkbType()) == QgsWkbTypes.CircularString)
    return False


def _get_circular_string(geom: QgsGeometry):
    """Extract the QgsCircularString from a circle geometry (bare or wrapped in CompoundCurve)."""
    if QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.CircularString:
        return geom.constGet()
    return geom.constGet().curveAt(0)


def circle_params(geom: QgsGeometry) -> tuple[QgsPointXY, float]:
    """Extract (center, radius) from a CircularString circle geometry."""
    cs = _get_circular_string(geom)
    p0 = cs.pointN(0)   # East:  (cx+r, cy)
    p2 = cs.pointN(2)   # West:  (cx-r, cy)
    p1 = cs.pointN(1)   # South: (cx,   cy-r)
    p3 = cs.pointN(3)   # North: (cx,   cy+r)
    cx = (p0.x() + p2.x()) / 2
    cy = (p1.y() + p3.y()) / 2
    r  = math.hypot(p0.x() - cx, p0.y() - cy)
    return QgsPointXY(cx, cy), r


def build_circle_geom(center: QgsPointXY, radius: float) -> QgsGeometry:
    """Build a circle geometry (CompoundCurve wrapping CircularString) from center + radius.

    The circles layer uses CompoundCurve as its WKB type, so returning a bare
    CircularString causes the data provider to reject the geometry at commit time.
    """
    cx, cy, r = center.x(), center.y(), radius
    cs = QgsCircularString()
    cs.setPoints([
        QgsPoint(cx + r, cy),
        QgsPoint(cx,     cy - r),
        QgsPoint(cx - r, cy),
        QgsPoint(cx,     cy + r),
        QgsPoint(cx + r, cy),
    ])
    cc = QgsCompoundCurve()
    cc.addCurve(cs)
    return QgsGeometry(cc)


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
