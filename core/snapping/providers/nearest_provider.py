# -*- coding: utf-8 -*-
"""NearestProvider — snaps to the closest point on any line or arc."""

import math
from qgis.core import (
    QgsPointXY, QgsPoint,
    QgsCompoundCurve, QgsCircularString,
)
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _line_polygon_layers, _dist


class NearestProvider:
    KEY = "nearest"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        raw_pt = QgsPoint(raw.x(), raw.y())
        for layer in _line_polygon_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue

                cs = _find_circular_string(geom)
                if cs is not None:
                    pt = _nearest_on_arc(raw, cs)
                    if pt is not None:
                        results.append(SnapResult(pt, SnapType.NEAREST, _dist(raw, pt)))
                    continue

                try:
                    sqr_dist, closest, _, _ = geom.closestSegmentWithContext(raw_pt)
                except Exception:
                    continue
                if closest is None or sqr_dist < 0:
                    continue
                pt = QgsPointXY(closest.x(), closest.y())
                results.append(SnapResult(pt, SnapType.NEAREST, math.sqrt(max(0.0, sqr_dist))))
        return results


def _find_circular_string(geom) -> 'QgsCircularString | None':
    """Return the first QgsCircularString inside geom, or None.

    Uses type-name comparison to avoid isinstance failures with SIP-wrapped types.
    """
    curve = geom.constGet()
    name = type(curve).__name__
    if name == 'QgsCircularString':
        return curve
    if name == 'QgsCompoundCurve':
        for i in range(curve.nCurves()):
            c = curve.curveAt(i)
            if type(c).__name__ == 'QgsCircularString':
                return c
    return None


def _nearest_on_arc(raw: QgsPointXY, cs: 'QgsCircularString') -> QgsPointXY | None:
    """Compute exact nearest point on the circle defined by a CircularString."""
    if cs.numPoints() < 3:
        return None
    pts = cs.points()
    cx, cy, r = _circumscribed_circle(pts[0], pts[1], pts[2])
    if r < 1e-10:
        return None
    dx = raw.x() - cx
    dy = raw.y() - cy
    d = math.hypot(dx, dy)
    if d < 1e-10:
        return None
    return QgsPointXY(cx + r * dx / d, cy + r * dy / d)


def _circumscribed_circle(p0, p1, p2):
    """Return (cx, cy, radius) of the circle through three QgsPoint-like objects."""
    ax, ay = p0.x(), p0.y()
    bx, by = p1.x(), p1.y()
    cx, cy = p2.x(), p2.y()
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-10:
        return 0.0, 0.0, 0.0
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) +
          (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) +
          (cx**2 + cy**2) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)
