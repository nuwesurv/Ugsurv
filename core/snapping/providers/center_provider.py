# -*- coding: utf-8 -*-
"""CenterProvider — snaps to centroids / circle centres of features."""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _geometry_layers, _dist
from .nearest_provider import _find_circular_string, _circumscribed_circle


class CenterProvider:
    KEY = "center"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _geometry_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue

                cs = _find_circular_string(geom)
                if cs is not None:
                    pt = _arc_circle_center(cs)
                else:
                    c = geom.centroid().asPoint()
                    pt = QgsPointXY(c.x(), c.y())

                if pt is not None:
                    results.append(SnapResult(pt, SnapType.CENTER, _dist(raw, pt)))
        return results


def _arc_circle_center(cs) -> QgsPointXY | None:
    if cs.numPoints() < 3:
        return None
    pts = cs.points()
    cx, cy, _ = _circumscribed_circle(pts[0], pts[1], pts[2])
    return QgsPointXY(cx, cy)
