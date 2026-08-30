# -*- coding: utf-8 -*-
"""VertexProvider — snaps to existing feature vertices."""

import math
from qgis.core import QgsPointXY, QgsWkbTypes
from ..snap_engine import SnapResult
from ...events import SnapType


class VertexProvider:
    KEY = "vertex"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _geometry_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                for v in geom.vertices():
                    pt = QgsPointXY(v.x(), v.y())
                    dist = _dist(raw, pt)
                    results.append(SnapResult(pt, SnapType.VERTEX, dist))
        return results


def _geometry_layers(storage):
    layers = []
    for attr in ("points_layer", "lines_layer", "polygons_layer"):
        lyr = getattr(storage, attr, None)
        if lyr and lyr.isValid():
            layers.append(lyr)
    return layers


def _dist(a: QgsPointXY, b: QgsPointXY) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())
