# -*- coding: utf-8 -*-
"""PointProvider — snaps to point features (SnapType.POINT).

Distinct from VertexProvider which handles line/polygon vertices (SnapType.VERTEX).
"""

import math
from qgis.core import QgsPointXY, QgsWkbTypes
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _visible_vector_layers


class PointProvider:
    KEY = "point"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _visible_vector_layers():
            if layer.geometryType() != QgsWkbTypes.PointGeometry:
                continue
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
