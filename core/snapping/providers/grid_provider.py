# -*- coding: utf-8 -*-
"""
GridProvider — independent grid snap.  Snaps to the nearest grid point.
Grid spacing is controlled via SnapSettings (defaults to 1.0 map units).
"""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType


class GridProvider:
    KEY = "grid"

    def __init__(self):
        self.spacing_x: float = 1.0
        self.spacing_y: float = 1.0
        self.origin_x:  float = 0.0
        self.origin_y:  float = 0.0

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        sx, sy = self.spacing_x, self.spacing_y
        if sx <= 0 or sy <= 0:
            return []
        gx = round((raw.x() - self.origin_x) / sx) * sx + self.origin_x
        gy = round((raw.y() - self.origin_y) / sy) * sy + self.origin_y
        pt   = QgsPointXY(gx, gy)
        dist = math.hypot(raw.x() - gx, raw.y() - gy)
        return [SnapResult(pt, SnapType.GRID, dist)]
