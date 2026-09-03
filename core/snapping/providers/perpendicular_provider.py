# -*- coding: utf-8 -*-
"""
PerpendicularProvider — perpendicular foot from raw_point onto a segment,
relative to a reference line set during ACTING.
"""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _geometry_layers, _dist
from .nearest_provider import _find_circular_string


def _perp_foot(p: QgsPointXY, a: QgsPointXY, b: QgsPointXY):
    """Return the perpendicular foot of p onto segment a-b, or None."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    len_sq = dx*dx + dy*dy
    if len_sq < 1e-12:
        return None
    t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / len_sq
    if not (0.0 <= t <= 1.0):
        return None
    return QgsPointXY(a.x() + t*dx, a.y() + t*dy)


class PerpendicularProvider:
    KEY = "perpendicular"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _geometry_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                if _find_circular_string(geom) is not None:
                    continue  # chord perp-feet would land inside the arc
                pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
                for i in range(len(pts) - 1):
                    foot = _perp_foot(raw, pts[i], pts[i+1])
                    if foot:
                        results.append(SnapResult(foot, SnapType.PERPENDICULAR,
                                                  _dist(raw, foot)))
        return results
