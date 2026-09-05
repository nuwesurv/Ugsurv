# -*- coding: utf-8 -*-
"""
ActionHistory — plugin-level undo/redo stack.

Each completed user action (draw, move, delete …) is recorded as a group of
{layer_id: step_count} entries.  Undo/redo delegate to each layer's
QgsUndoStack, which QGIS automatically maintains for every addFeature,
changeGeometry, changeAttributeValues and deleteFeature call in the edit buffer.

Usage
─────
Wrap every logical action in begin_group / record_step / end_group:

    hist.begin_group()
    layer.changeGeometry(fid, new_geom)
    hist.record_step(layer.id())          # one call per layer write
    hist.end_group()

For multi-step operations (move 10 features) open one group, record each step,
then close:

    hist.begin_group()
    for layer, fid, geom in features:
        layer.changeGeometry(fid, geom)
        hist.record_step(layer.id())
    hist.end_group()

Signals
───────
can_undo_changed(bool)   emitted when the undo stack becomes available or empty
can_redo_changed(bool)   emitted when the redo stack becomes available or empty
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsProject


class ActionHistory(QObject):
    """Plugin-level undo/redo stack, backed by per-layer QgsUndoStack."""

    can_undo_changed = pyqtSignal(bool)
    can_redo_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._undo_log: list[dict] = []   # [{layer_id: step_count}, ...]
        self._redo_log: list[dict] = []
        self._current_group: dict | None = None

    # ── group recording ───────────────────────────────────────────────────
    def begin_group(self):
        """Open a new action group (call before any layer writes)."""
        self._current_group = {}

    def record_step(self, layer_id: str):
        """Record one undo step on layer_id within the open group.

        Call once for each layer.addFeature / changeGeometry /
        changeAttributeValues / deleteFeature that the action performs.
        """
        if self._current_group is None:
            return
        self._current_group[layer_id] = self._current_group.get(layer_id, 0) + 1

    def end_group(self):
        """Close the group and push it to the undo stack.

        Clears the redo stack (new action invalidates future redo).
        No-op if the group is empty (nothing was written).
        """
        group = self._current_group
        self._current_group = None
        if not group:
            return
        was_can_undo = bool(self._undo_log)
        was_can_redo = bool(self._redo_log)
        self._undo_log.append(group)
        self._redo_log.clear()
        if not was_can_undo:
            self.can_undo_changed.emit(True)
        if was_can_redo:
            self.can_redo_changed.emit(False)

    def cancel_group(self):
        """Discard the open group without recording (action was cancelled)."""
        self._current_group = None

    # ── undo / redo ───────────────────────────────────────────────────────
    def undo(self) -> bool:
        """Undo the last action group. Returns True if something was undone."""
        if not self._undo_log:
            return False
        group = self._undo_log.pop()
        was_can_redo = bool(self._redo_log)
        self._redo_log.append(group)
        for layer_id, count in group.items():
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None:
                for _ in range(count):
                    layer.undoStack().undo()
                layer.triggerRepaint()
        if not self._undo_log:
            self.can_undo_changed.emit(False)
        if not was_can_redo:
            self.can_redo_changed.emit(True)
        return True

    def redo(self) -> bool:
        """Redo the last undone action group. Returns True if something was redone."""
        if not self._redo_log:
            return False
        group = self._redo_log.pop()
        was_can_undo = bool(self._undo_log)
        self._undo_log.append(group)
        for layer_id, count in group.items():
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None:
                for _ in range(count):
                    layer.undoStack().redo()
                layer.triggerRepaint()
        if not self._redo_log:
            self.can_redo_changed.emit(False)
        if not was_can_undo:
            self.can_undo_changed.emit(True)
        return True

    # ── state ─────────────────────────────────────────────────────────────
    @property
    def can_undo(self) -> bool:
        return bool(self._undo_log)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_log)

    def clear(self):
        """Discard the entire history (call on project clear or open)."""
        was_undo = bool(self._undo_log)
        was_redo = bool(self._redo_log)
        self._undo_log.clear()
        self._redo_log.clear()
        self._current_group = None
        if was_undo:
            self.can_undo_changed.emit(False)
        if was_redo:
            self.can_redo_changed.emit(False)
