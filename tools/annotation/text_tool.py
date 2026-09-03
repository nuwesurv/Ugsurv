# -*- coding: utf-8 -*-
"""TextTool — placeholder (not yet implemented)."""

from qgis.PyQt.QtCore import Qt
from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class TextTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def activate(self):
        super().activate()
        self._transition(ToolState.ACTING)
        self._log("TEXT tool is not yet implemented.")
        self._go_home()

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            self._go_home()

    def _on_cancel_hook(self):
        pass

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)
