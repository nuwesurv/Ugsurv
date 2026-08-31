# -*- coding: utf-8 -*-
"""PointProvider — snaps to point features (SnapType.POINT).

Distinct from VertexProvider which handles line/polygon vertices (SnapType.VERTEX).
"""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType


class PointProvider:
    KEY = "point"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        layer = getattr(storage, "points_layer", None)
        if not (layer and layer.isValid()):
            return results
        for feat in layer.getFeatures(rect):
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            for v in geom.vertices():
                pt = QgsPointXY(v.x(), v.y())
                results.append(SnapResult(pt, SnapType.POINT, _dist(raw, pt)))
        return results


def _dist(a: QgsPointXY, b: QgsPointXY) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())
