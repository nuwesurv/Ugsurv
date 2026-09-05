# -*- coding: utf-8 -*-
"""VertexProvider — snaps to vertices (endpoints/nodes) of line features.

Point features are handled by PointProvider with SnapType.POINT.
"""

import math
from qgis.core import QgsPointXY, QgsProject, QgsVectorLayer, QgsWkbTypes
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
        node = QgsProject.instance().layerTreeRoot().findLayer(lyr.id())
        return node is not None and node.isVisible()
    except RuntimeError:
        return False


def _visible_vector_layers():
    """All visible QgsVectorLayer instances in the current project."""
    root = QgsProject.instance().layerTreeRoot()
    result = []
    for lyr in QgsProject.instance().mapLayers().values():
        if not isinstance(lyr, QgsVectorLayer):
            continue
        try:
            if not lyr.isValid():
                continue
        except RuntimeError:
            continue
        node = root.findLayer(lyr.id())
        if node and node.isVisible():
            result.append(lyr)
    return result


def _line_polygon_layers(storage):
    """All visible line/polygon vector layers in the project."""
    return [
        lyr for lyr in _visible_vector_layers()
        if lyr.geometryType() in (QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry)
    ]


def _geometry_layers(storage):
    """All visible vector layers in the project (any geometry type)."""
    return [
        lyr for lyr in _visible_vector_layers()
        if lyr.geometryType() not in (QgsWkbTypes.UnknownGeometry, QgsWkbTypes.NullGeometry)
    ]


def _dist(a: QgsPointXY, b: QgsPointXY) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())
