# -*- coding: utf-8 -*-
"""
CommandDispatcher — single entry point for toolbar clicks, keyboard shortcuts,
and command-line typed input.  All three routes go through here to
ToolManager.activate_tool(), never directly.

Transparent commands (prefixed with ') are dispatched as modal interrupts.
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal


class CommandDispatcher(QObject):
    unknownCommand = pyqtSignal(str)   # emitted when token is not recognised

    def __init__(self, tool_manager, command_registry, tool_factory,
                 parent=None):
        super().__init__(parent)
        self._mgr      = tool_manager
        self._registry = command_registry
        self._factory  = tool_factory   # callable(tool_key) → BaseTool

    # ── primary entry point ──────────────────────────────────────────────
    def dispatch(self, token: str):
        """
        Dispatch a command token.  Called by toolbar buttons, keyboard
        shortcuts, and the command-line widget returnPressed.
        """
        token = token.strip()
        if not token:
            return

        transparent = token.startswith("'")
        if transparent:
            token = token[1:]
            self._dispatch_transparent(token)
            return

        tool_key = self._registry.resolve(token)
        if tool_key is None:
            self.unknownCommand.emit(token)
            return

        tool = self._factory(tool_key)
        if tool is not None:
            self._mgr.activate_tool(tool)

    def dispatch_tool_key(self, tool_key: str):
        """Directly activate by tool_key (used by toolbar action callbacks)."""
        tool = self._factory(tool_key)
        if tool is not None:
            self._mgr.activate_tool(tool)

    # ── transparent (modal) commands ─────────────────────────────────────
    def _dispatch_transparent(self, token: str):
        """Transparent commands (e.g. 'ZOOM) run as a modal interrupt."""
        token_up = token.upper()
        if token_up in ("ZOOM", "Z"):
            self._mgr.suspend_for_modal()
            # QGIS's built-in zoom is triggered by the user via mouse; we just
            # note the suspend so resume is wired up.
        # Additional transparent commands can be added here.
