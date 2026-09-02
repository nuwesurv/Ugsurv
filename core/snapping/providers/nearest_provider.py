# -*- coding: utf-8 -*-
"""NearestProvider — snaps to the closest point on any line segment."""

import math
from qgis.core import QgsPointXY, QgsPoint
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _line_polygon_layers


class NearestProvider:
    KEY = "nearest"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        raw_pt = QgsPoint(raw.x(), raw.y())
        for layer in _line_polygon_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                try:
                    sqr_dist, closest, _, _ = geom.closestSegmentWithContext(raw_pt)
                except Exception:
                    continue
                if closest is None or sqr_dist < 0:
                    continue
                pt = QgsPointXY(closest.x(), closest.y())
                results.append(SnapResult(pt, SnapType.NEAREST, math.sqrt(max(0.0, sqr_dist))))
        return results
