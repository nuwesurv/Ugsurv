# -*- coding: utf-8 -*-
"""
IntersectionProvider — computed intersections between features, including
against the in-progress sketch geometry.
"""

import math
from qgis.core import QgsPointXY, QgsGeometry
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _geometry_layers, _dist


class IntersectionProvider:
    KEY = "intersection"

    def __init__(self):
        self._sketch_geom = None   # set by the active tool's rubber-band geometry

    def set_sketch(self, geom):
        self._sketch_geom = geom

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        layers = _geometry_layers(storage)
        feats  = []
        for layer in layers:
            feats.extend(layer.getFeatures(rect))

        # add sketch geometry as a virtual feature
        if self._sketch_geom and not self._sketch_geom.isEmpty():
            class _FakeFeature:
                def geometry(self): return self._sketch_geom
            feats.append(self._sketch_geom)

        for i in range(len(feats)):
            for j in range(i + 1, len(feats)):
                a = feats[i].geometry() if hasattr(feats[i], 'geometry') else feats[i]
                b = feats[j].geometry() if hasattr(feats[j], 'geometry') else feats[j]
                if a.isEmpty() or b.isEmpty():
                    continue
                inter = a.intersection(b)
                if inter.isEmpty():
                    continue
                for v in inter.vertices():
                    pt = QgsPointXY(v.x(), v.y())
                    results.append(SnapResult(pt, SnapType.INTERSECTION,
                                              _dist(raw, pt)))
        return results
