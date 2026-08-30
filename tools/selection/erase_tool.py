# -*- coding: utf-8 -*-
"""
EraseTool — SELECTING → Enter/Delete key = immediate commit, no ACTING phase.

Deletes all features in SelectionModel from their respective layers'
edit-buffers.  Never calls commitChanges().
"""

from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core.pending_action import PendingAction


class EraseTool(BaseTool):
    CURSOR = Qt.CursorShape.ForbiddenCursor

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.DELETE, EventType.CONFIRM):
            self._do_erase()

    def _do_erase(self):
        sel = self._ctx.selection_model
        if sel.is_empty():
            return

        for lid, fid in list(sel):
            layer = QgsProject.instance().mapLayer(lid)
            if layer is None:
                continue
            if not layer.isEditable():
                layer.startEditing()
            layer.deleteFeature(fid)

        sel.clear()
        self._transition(ToolState.IDLE)
