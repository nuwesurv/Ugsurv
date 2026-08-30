# -*- coding: utf-8 -*-
"""
PendingAction — one in-progress, uncommitted operation.

Rules:
- commit() writes to the layer edit-buffer only (startEditing / addFeature /
  changeGeometry).  It NEVER calls layer.commitChanges().
- cancel() discards everything.  No trace left in the buffer.
"""

from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry


class PendingAction:
    def __init__(self, layer: QgsVectorLayer, cad_layer: str):
        self._layer = layer
        self._cad_layer = cad_layer
        self._rubber_bands = []   # QgsRubberBand list for preview
        self._snapshots = {}      # {fid: wkt_before} for pre-action snapshot
        self._committed = False
        self._cancelled = False

    # ── preview rubber-bands ──────────────────────────────────────────────
    def add_rubber_band(self, rb):
        self._rubber_bands.append(rb)
        return rb

    def clear_rubber_bands(self):
        for rb in self._rubber_bands:
            try:
                rb.reset()
            except Exception:
                pass
        self._rubber_bands.clear()

    # ── snapshot (for modify tools) ───────────────────────────────────────
    def snapshot_feature(self, fid: int):
        """Record WKT of fid before modification, so cancel() can restore."""
        feat = self._layer.getFeature(fid)
        if feat.isValid():
            self._snapshots[fid] = feat.geometry().asWkt()

    # ── commit path ───────────────────────────────────────────────────────
    def commit_new_feature(self, geometry: QgsGeometry, attributes: dict = None):
        """Add a brand-new feature into the layer edit-buffer."""
        if self._committed or self._cancelled:
            return
        if not self._layer.isEditable():
            self._layer.startEditing()
        feat = QgsFeature(self._layer.fields())
        feat.setGeometry(geometry)
        feat['cad_layer'] = self._cad_layer
        if attributes:
            for k, v in attributes.items():
                feat[k] = v
        self._layer.addFeature(feat)
        self.clear_rubber_bands()
        self._committed = True

    def commit_geometry_change(self, fid: int, new_geometry: QgsGeometry):
        """Replace geometry of an existing feature in the edit-buffer."""
        if self._committed or self._cancelled:
            return
        if not self._layer.isEditable():
            self._layer.startEditing()
        self._layer.changeGeometry(fid, new_geometry)
        self.clear_rubber_bands()
        self._committed = True

    def commit_delete_feature(self, fid: int):
        """Delete a feature from the edit-buffer."""
        if self._committed or self._cancelled:
            return
        if not self._layer.isEditable():
            self._layer.startEditing()
        self._layer.deleteFeature(fid)
        self.clear_rubber_bands()
        self._committed = True

    # ── cancel path ──────────────────────────────────────────────────────
    def cancel(self):
        if self._committed or self._cancelled:
            return
        self.clear_rubber_bands()
        # restore any snapshotted geometries
        for fid, wkt in self._snapshots.items():
            geom = QgsGeometry.fromWkt(wkt)
            if self._layer.isEditable():
                self._layer.changeGeometry(fid, geom)
        self._cancelled = True
