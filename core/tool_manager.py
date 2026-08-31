# -*- coding: utf-8 -*-
"""
ToolManager — single entry point for tool activation from toolbar, command
line, or keyboard shortcut.

Two interrupt kinds (§2):
  - Modal interrupt  (pan/zoom/osnap override): suspend → run → resume.
  - Command interrupt (user starts different command): abandon current
    PendingAction entirely, activate new tool.
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal


class ToolManager(QObject):
    toolChanged = pyqtSignal(object)   # emits new BaseTool (or None)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self._canvas        = canvas
        self._active        = None     # current BaseTool
        self._home          = None     # permanent base tool (SelectTool)
        self._suspend_stack = []       # [(tool, snapshot)] for modal interrupts

    # ── primary API ──────────────────────────────────────────────────────
    def activate_tool(self, tool):
        """Activate tool.  If another is active, it is abandoned (command interrupt)."""
        if self._active is tool:
            return  # already active, no-op
        if self._active is not None:
            # command interrupt: abandon current operation
            self._active.cancel() if hasattr(self._active, 'cancel') else None
            self._active.deactivate()
        self._active = tool
        self._canvas.setMapTool(tool)
        self.toolChanged.emit(tool)

    def set_home(self, tool):
        """Set the permanent base tool and activate it immediately."""
        self._home = tool
        self.activate_tool(tool)

    def go_home(self):
        """Return to the home tool. No-op if already there."""
        if self._home is not None and self._active is not self._home:
            self.activate_tool(self._home)

    @property
    def home_tool(self):
        return self._home

    def deactivate(self):
        """Deactivate the active tool without activating another (teardown only)."""
        if self._active is not None:
            self._active.deactivate()
            self._canvas.unsetMapTool(self._active)
            self._active = None
            self.toolChanged.emit(None)

    @property
    def active_tool(self):
        return self._active

    # ── modal interrupt ──────────────────────────────────────────────────
    def suspend_for_modal(self):
        """Suspend the active tool for a modal interrupt (pan/zoom)."""
        if self._active is not None and hasattr(self._active, 'suspend'):
            snap = self._active.suspend()
            self._suspend_stack.append((self._active, snap))

    def resume_from_modal(self):
        """Resume the suspended tool after modal action ends."""
        if self._suspend_stack:
            tool, snap = self._suspend_stack.pop()
            if hasattr(tool, 'resume'):
                tool.resume(snap)
            # Re-set the map tool so canvas events flow back to it
            self._canvas.setMapTool(tool)
