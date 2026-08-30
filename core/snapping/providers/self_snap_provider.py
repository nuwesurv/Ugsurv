# -*- coding: utf-8 -*-
"""
SelfSnapProvider — snaps the cursor against the entity currently being drawn
(e.g. polyline closing to its own start point).
"""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType


class SelfSnapProvider:
    KEY = "self"

    def __init__(self):
        self._points: list[QgsPointXY] = []

    def set_sketch_points(self, points: list):
        self._points = list(points)

    def clear(self):
        self._points.clear()

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for pt in self._points:
            dist = math.hypot(raw.x() - pt.x(), raw.y() - pt.y())
            results.append(SnapResult(pt, SnapType.SELF, dist))
        return results
