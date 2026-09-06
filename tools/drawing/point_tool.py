# -*- coding: utf-8 -*-
"""
PointTool — places individual point features.

Each click commits one Point feature to the points layer.
Enter or Esc exits the command.  Multiple points can be placed in a single
command session.
"""

from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsFeature

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class PointTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def activate(self):
        super().activate()
        self._transition(ToolState.ACTING)
        self._request_input("xy", "Specify point:")

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED) and sem.point:
            self._commit_point(sem.point)
            self._go_home()
        elif sem.type == EventType.CONFIRM:
            self._go_home()

    def _on_hover(self, sem: SemanticEvent):
        if sem.point:
            dyn = getattr(self._ctx, 'dyn_widget', None)
            if dyn is not None:
                dyn.set_live_pair(sem.point.x(), sem.point.y())

    def _commit_point(self, pt: QgsPointXY):
        geom = QgsGeometry.fromPointXY(pt)

        ef = getattr(self._ctx, 'entity_factory', None)
        if ef:
            ef.commit(geom, self._ctx.active_cad_layer)
            return

        # fallback: write via storage (handles CRS transformation)
        self._ctx.storage_manager.add_point(geom, self._ctx.active_cad_layer)
