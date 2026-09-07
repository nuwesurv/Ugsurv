# -*- coding: utf-8 -*-
"""
EraseTool — SELECTING → Enter/Delete key = immediate commit, no ACTING phase.

Deletes all features in SelectionModel from their respective layers'
edit-buffers.  Never calls commitChanges().
"""

from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsWkbTypes

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core.pending_action import PendingAction


class EraseTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.DELETE, EventType.CONFIRM):
            self._do_erase()

    def _do_erase(self):  # noqa: C901
        sel = self._ctx.selection_model
        if sel.is_empty():
            return
        hist = getattr(self._ctx, 'action_history', None)
        if hist:
            hist.begin_group()
        _GTYPE = {0: "point", 1: "line", 2: "polygon"}
        layer_counts = {}  # lid -> {name, count, gtype}
        for lid, fid in list(sel):
            layer = QgsProject.instance().mapLayer(lid)
            if layer is None:
                continue
            if not layer.isEditable():
                layer.startEditing()
            if lid not in layer_counts:
                feat = layer.getFeature(fid)
                gtype = int(QgsWkbTypes.geometryType(feat.geometry().wkbType())) if feat.isValid() else None
                layer_counts[lid] = {"name": layer.name(), "count": 0, "gtype": gtype}
            layer_counts[lid]["count"] += 1
            layer.deleteFeature(fid)
            if hist:
                hist.record_step(layer.id())

        if hist:
            hist.end_group()
        sel.clear()
        cmd_dock = getattr(self._ctx, 'cmd_dock', None)
        if cmd_dock and layer_counts:
            parts = []
            for info in layer_counts.values():
                n = info["count"]
                g = _GTYPE.get(info["gtype"], "feature")
                parts.append(f"{n} {g}{'s' if n > 1 else ''} from '{info['name']}'")
            cmd_dock.log("Deleted " + ", ".join(parts) + ".", "#ff8888")
        self._go_home()
