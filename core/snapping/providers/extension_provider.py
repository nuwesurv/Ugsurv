# -*- coding: utf-8 -*-
"""
ExtensionProvider — snaps to the extension of existing segments beyond
their endpoints.  Projects raw_point onto the extension line and returns
the closest point on that extension (outside the original segment).

Also stores the active endpoint and unit-direction vector so drawing tools
can resolve a typed distance to an exact point along the extension.
"""

import math
from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _geometry_layers, _dist


def _extension_info(p: QgsPointXY, a: QgsPointXY, b: QgsPointXY):
    """Project p onto the extension of segment a-b beyond its endpoints.

    Returns (foot, endpoint, ux, uy) where:
      foot     — projected point on the extension line
      endpoint — the segment endpoint being extended from
      (ux, uy) — unit vector pointing from endpoint into the extension
    Returns None if p projects inside the segment.
    """
    dx, dy = b.x() - a.x(), b.y() - a.y()
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return None
    t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / len_sq
    if 0.0 <= t <= 1.0:
        return None   # inside segment — other providers handle this
    seg_len = math.sqrt(len_sq)
    ux, uy  = dx / seg_len, dy / seg_len
    foot    = QgsPointXY(a.x() + t * dx, a.y() + t * dy)
    if t > 1.0:
        return foot, b, ux, uy      # extending beyond B in the A→B direction
    else:
        return foot, a, -ux, -uy   # extending beyond A in the B→A direction


class ExtensionProvider:
    KEY = "extension"

    def __init__(self):
        # Set by query(); tools read these for distance-entry along the extension.
        self.active_endpoint:  QgsPointXY | None = None
        self.active_direction: tuple | None      = None   # (ux, uy)

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results   = []
        best_dist = float('inf')
        self.active_endpoint  = None
        self.active_direction = None

        for layer in _geometry_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
                for i in range(len(pts) - 1):
                    info = _extension_info(raw, pts[i], pts[i + 1])
                    if info is None:
                        continue
                    foot, ep, ux, uy = info
                    d = _dist(raw, foot)
                    results.append(SnapResult(foot, SnapType.EXTENSION, d))
                    if d < best_dist:
                        best_dist             = d
                        self.active_endpoint  = ep
                        self.active_direction = (ux, uy)

        return results
