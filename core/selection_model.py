# -*- coding: utf-8 -*-
"""
SelectionModel — layer-independent selection set, independent of tools.

Stores (layer_id, feature_id) tuples.  Persists across tool switches.
Enforces CAD-layer lock: locked layers cannot be added to the selection.
Emits selectionChanged when the set changes.
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsProject


class SelectionModel(QObject):
    selectionChanged = pyqtSignal()   # emitted whenever the set changes

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: set = set()          # {(layer_id, fid)}
        self._locked_cad_layers: set = set()  # names of locked CAD layers

    # ── lock enforcement ─────────────────────────────────────────────────
    def set_locked_cad_layers(self, names: set):
        self._locked_cad_layers = set(names)

    def _cad_layer_of(self, layer_id: str, fid: int) -> str:
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            return ""
        feat = layer.getFeature(fid)
        return feat["cad_layer"] if feat.isValid() else ""

    def _is_locked(self, layer_id: str, fid: int) -> bool:
        return self._cad_layer_of(layer_id, fid) in self._locked_cad_layers

    # ── mutation ─────────────────────────────────────────────────────────
    def add(self, layer_id: str, fid: int):
        if self._is_locked(layer_id, fid):
            return
        item = (layer_id, fid)
        if item not in self._items:
            self._items.add(item)
            self.selectionChanged.emit()

    def remove(self, layer_id: str, fid: int):
        item = (layer_id, fid)
        if item in self._items:
            self._items.discard(item)
            self.selectionChanged.emit()

    def toggle(self, layer_id: str, fid: int):
        item = (layer_id, fid)
        if item in self._items:
            self._items.discard(item)
        else:
            if not self._is_locked(layer_id, fid):
                self._items.add(item)
        self.selectionChanged.emit()

    def clear(self):
        if self._items:
            self._items.clear()
            self.selectionChanged.emit()

    def set_from(self, items):
        """Replace entire selection set (skips locked items)."""
        new = {(lid, fid) for lid, fid in items if not self._is_locked(lid, fid)}
        if new != self._items:
            self._items = new
            self.selectionChanged.emit()

    # ── query ─────────────────────────────────────────────────────────────
    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __contains__(self, item):
        return item in self._items

    @property
    def items(self) -> list:
        return list(self._items)

    def is_empty(self) -> bool:
        return len(self._items) == 0

    def layer_ids(self) -> set:
        return {lid for lid, _ in self._items}

    def fids_for_layer(self, layer_id: str) -> list:
        return [fid for lid, fid in self._items if lid == layer_id]
