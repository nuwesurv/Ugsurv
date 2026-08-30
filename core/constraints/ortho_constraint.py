# -*- coding: utf-8 -*-
"""
OrthoConstraint (F8) — locks movement to horizontal or vertical, relative
to the last confirmed point.  Applied as a post-snap stage.
"""

import math
from qgis.core import QgsPointXY


class OrthoConstraint:
    """Constrains the snapped point to 0/90/180/270 from reference."""

    def __init__(self):
        self.enabled: bool = False
        self._ref: QgsPointXY | None = None   # last confirmed point

    def set_reference(self, pt: QgsPointXY):
        self._ref = pt

    def apply(self, point: QgsPointXY) -> QgsPointXY:
        if not self.enabled or self._ref is None:
            return point
        dx = point.x() - self._ref.x()
        dy = point.y() - self._ref.y()
        # snap to whichever axis the cursor is closer to
        if abs(dx) >= abs(dy):
            return QgsPointXY(point.x(), self._ref.y())
        else:
            return QgsPointXY(self._ref.x(), point.y())
