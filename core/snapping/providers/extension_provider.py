# -*- coding: utf-8 -*-
"""
ExtensionProvider — snaps to the extension of existing segments beyond
their endpoints.  Projects raw_point onto the extension line and returns
the closest point on that extension (outside the original segment).
"""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _geometry_layers, _dist


def _extension_foot(p: QgsPointXY, a: QgsPointXY, b: QgsPointXY):
    """Foot of p on line through a-b, but only if outside the segment."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    len_sq = dx*dx + dy*dy
    if len_sq < 1e-12:
        return None
    t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / len_sq
    if 0.0 <= t <= 1.0:
        return None   # inside segment — vertex/midpoint providers handle this
    return QgsPointXY(a.x() + t*dx, a.y() + t*dy)


class ExtensionProvider:
    KEY = "extension"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _geometry_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
                for i in range(len(pts) - 1):
                    foot = _extension_foot(raw, pts[i], pts[i+1])
                    if foot:
                        results.append(SnapResult(foot, SnapType.EXTENSION,
                                                  _dist(raw, foot)))
        return results
