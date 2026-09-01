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
        Write geometry (in project CRS) into the correct edit-buffer.
        StorageManager.add_line/add_point handle the CRS transformation to
        the layer's fixed CRS (EPSG:32636) automatically.
        Returns True on success.
        """
        if not self._storage.enabled:
            return False

        gtype = int(geometry.type())

        if gtype == 0:    # Point
            ok = self._storage.add_point(geometry, cad_layer)
        elif gtype in (1, 2):   # Line or Polygon boundary → lines layer
            ok = self._storage.add_line(geometry, cad_layer)
        else:
            return False

        if ok and extra_attrs:
            # Extra attributes are rare; write them via the layer directly.
            # The feature was just appended so we query it back by last fid.
            layer = (self._storage.points_layer if gtype == 0
                     else self._storage.lines_layer)
            if layer:
                fids = sorted(f.id() for f in layer.getFeatures())
                if fids:
                    layer.changeAttributeValues(
                        fids[-1],
                        {layer.fields().indexFromName(k): v
                         for k, v in extra_attrs.items()},
                    )
        return ok

    # ── routing ───────────────────────────────────────────────────────────
    def _pick_layer(self, geometry: QgsGeometry) -> QgsVectorLayer | None:
        # geometry.type() returns int (0=Point,1=Line,2=Polygon) across QGIS 3.x
        gtype = int(geometry.type())
        if gtype == 0:   # Point
            return self._storage.points_layer
        if gtype in (1, 2):  # Line or Polygon boundary → lines layer
            return self._storage.lines_layer
        return None

    # ── geometry normalisation ────────────────────────────────────────────
    @staticmethod
    def ensure_multi(geometry: QgsGeometry) -> QgsGeometry:
        """Promote single-part to Multi* so it matches the table schema."""
        if not QgsWkbTypes.isMultiType(geometry.wkbType()):
            geometry.convertToMultiType()
        return geometry
