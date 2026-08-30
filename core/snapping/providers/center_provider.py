# -*- coding: utf-8 -*-
"""CenterProvider — snaps to centroids of features."""

from qgis.core import QgsPointXY
from ..snap_engine import SnapResult
from ...events import SnapType
from .vertex_provider import _geometry_layers, _dist


class CenterProvider:
    KEY = "center"

    def query(self, raw: QgsPointXY, rect, storage) -> list[SnapResult]:
        results = []
        for layer in _geometry_layers(storage):
            for feat in layer.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                c = geom.centroid().asPoint()
                pt = QgsPointXY(c.x(), c.y())
                results.append(SnapResult(pt, SnapType.CENTER, _dist(raw, pt)))
        return results
