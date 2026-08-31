# -*- coding: utf-8 -*-
"""VertexProvider — snaps to vertices (endpoints/nodes) of line and polygon features.

Point features are handled by PointProvider with SnapType.POINT.
"""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType


class VertexProvider:
    KEY = "vertex"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _line_polygon_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                for v in geom.vertices():
                    pt = QgsPointXY(v.x(), v.y())
                    results.append(SnapResult(pt, SnapType.VERTEX, _dist(raw, pt)))
        return results


def _line_polygon_layers(storage):
    """Lines and polygons only — for vertex/endpoint snapping."""
    layers = []
    for attr in ("lines_layer", "polygons_layer"):
        lyr = getattr(storage, attr, None)
        if lyr and lyr.isValid():
            layers.append(lyr)
    return layers


def _geometry_layers(storage):
    """All three geometry layers — re-exported for use by other providers."""
    layers = []
    for attr in ("points_layer", "lines_layer", "polygons_layer"):
        lyr = getattr(storage, attr, None)
        if lyr and lyr.isValid():
            layers.append(lyr)
    return layers


def _dist(a: QgsPointXY, b: QgsPointXY) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())
