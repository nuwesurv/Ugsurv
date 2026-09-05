# -*- coding: utf-8 -*-
"""VertexProvider — snaps to vertices (endpoints/nodes) of line features.

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


def _layer_ok(lyr) -> bool:
    """Safely test a layer reference — guards against deleted C++ objects and hidden layers."""
    if lyr is None:
        return False
    try:
        if not lyr.isValid():
            return False
        from qgis.core import QgsProject
        node = QgsProject.instance().layerTreeRoot().findLayer(lyr.id())
        return node is not None and node.isVisible()
    except RuntimeError:
        return False


def _line_polygon_layers(storage):
    """Lines and circles — for vertex/nearest snapping."""
    layers = []
    for attr in ("lines_layer", "circles_layer"):
        lyr = getattr(storage, attr, None)
        if _layer_ok(lyr):
            layers.append(lyr)
    return layers


def _geometry_layers(storage):
    """All geometry layers — re-exported for use by other providers."""
    layers = []
    for attr in ("points_layer", "lines_layer", "circles_layer"):
        lyr = getattr(storage, attr, None)
        if _layer_ok(lyr):
            layers.append(lyr)
    return layers


def _dist(a: QgsPointXY, b: QgsPointXY) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())
