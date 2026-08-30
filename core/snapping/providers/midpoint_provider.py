# -*- coding: utf-8 -*-
"""MidpointProvider — snaps to segment midpoints."""

import math
from qgis.core import QgsPointXY, QgsGeometry
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _geometry_layers, _dist


class MidpointProvider:
    KEY = "midpoint"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _geometry_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                for part in geom.asGeometryCollection() or [geom]:
                    pts = _extract_points(part)
                    for i in range(len(pts) - 1):
                        mid = QgsPointXY(
                            (pts[i].x() + pts[i+1].x()) / 2,
                            (pts[i].y() + pts[i+1].y()) / 2,
                        )
                        results.append(SnapResult(mid, SnapType.MIDPOINT,
                                                  _dist(raw, mid)))
        return results


def _extract_points(geom: QgsGeometry) -> list:
    return [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
