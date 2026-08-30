# -*- coding: utf-8 -*-
"""
PolarConstraint (F10) — locks movement to multiples of a polar angle
increment (default 90°, user-configurable), from the reference point.
"""

import math
from qgis.core import QgsPointXY


class PolarConstraint:
    """Constrains the snapped point to polar angle increments."""

    def __init__(self, increment_deg: float = 90.0):
        self.enabled: bool = False
        self.increment_deg: float = increment_deg
        self._ref: QgsPointXY | None = None

    def set_reference(self, pt: QgsPointXY):
        self._ref = pt

    def apply(self, point: QgsPointXY) -> QgsPointXY:
        if not self.enabled or self._ref is None:
            return point
        dx  = point.x() - self._ref.x()
        dy  = point.y() - self._ref.y()
        dist = math.hypot(dx, dy)
        if dist < 1e-10:
            return point
        angle_rad = math.atan2(dy, dx)
        angle_deg = math.degrees(angle_rad)
        inc = self.increment_deg
        snapped = round(angle_deg / inc) * inc
        snapped_rad = math.radians(snapped)
        return QgsPointXY(
            self._ref.x() + dist * math.cos(snapped_rad),
            self._ref.y() + dist * math.sin(snapped_rad),
        )
