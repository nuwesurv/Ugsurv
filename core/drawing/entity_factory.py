# -*- coding: utf-8 -*-
"""
EntityFactory — given a finished geometry, routes it to the correct
GeoPackage table (points/lines/polygons) and stamps the cad_layer value.

Commit path (��4):
    PendingAction.commit() → EntityFactory.commit(geometry, cad_layer)
      → pick table by geometry type
      → layer.startEditing() if not already
      → layer.addFeature(feat)   [edit-buffer only — NEVER commitChanges()]
"""

from qgis.core import (
    QgsGeometry, QgsFeature, QgsWkbTypes, QgsVectorLayer,
)


class EntityFactory:
    def __init__(self, storage_manager):
        self._storage = storage_manager

    def commit(self, geometry: QgsGeometry, cad_layer: str,
               extra_attrs: dict = None) -> bool:
        """
        Write geometry into the correct edit-buffer.
        Returns True on success.
        """
        layer = self._pick_layer(geometry)
        if layer is None:
            return False
        if not self._storage.enabled:
            return False

        if not layer.isEditable():
            layer.startEditing()

        feat = QgsFeature(layer.fields())
        feat.setGeometry(geometry)
        feat["cad_layer"] = cad_layer
        if extra_attrs:
            for k, v in extra_attrs.items():
                feat[k] = v
        layer.addFeature(feat)
        return True

    # ── routing ───────────────────────────────────────────────────────────
    def _pick_layer(self, geometry: QgsGeometry) -> QgsVectorLayer | None:
        # geometry.type() returns int (0=Point,1=Line,2=Polygon) across QGIS 3.x
        gtype = int(geometry.type())
        if gtype == 0:   # Point
            return self._storage.points_layer
        if gtype == 1:   # Line
            return self._storage.lines_layer
        if gtype == 2:   # Polygon
            return self._storage.polygons_layer
        return None

    # ── geometry normalisation ────────────────────────────────────────────
    @staticmethod
    def ensure_multi(geometry: QgsGeometry) -> QgsGeometry:
        """Promote single-part to Multi* so it matches the table schema."""
        if not QgsWkbTypes.isMultiType(geometry.wkbType()):
            geometry.convertToMultiType()
        return geometry
