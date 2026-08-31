# -*- coding: utf-8 -*-
"""
CadToolbar — one QAction per tool, routes through CommandDispatcher.

The entire toolbar is disabled (greyed out) until StorageManager.enabled
is True (§5 gating).
"""

from qgis.PyQt.QtWidgets import QToolBar, QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt
import os

from ..core import style as _style


_TOOL_DEFS = []   # all tools activated via command line; only snap button remains


class CadToolbar(QToolBar):
    def __init__(self, dispatcher, icon_dir: str, parent=None):
        super().__init__("CAD Tools", parent)
        self.setObjectName("UgsurvCadToolbar")
        self._dispatcher = dispatcher
        self._icon_dir   = icon_dir
        self._actions    = {}

        self._build()
        self.setEnabled(False)   # disabled until project saved (§5)

    def _build(self):
        for entry in _TOOL_DEFS:
            key, label, tip, icon_file = entry
            if key is None:
                self.addSeparator()
                continue
            icon_path = os.path.join(self._icon_dir, icon_file)
            icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
            act  = QAction(icon, label, self)
            act.setToolTip(tip)
            act.setCheckable(True)
            act.setObjectName(f"cad_{key}")
            act.triggered.connect(lambda checked, k=key: self._on_action(k))
            self.addAction(act)
            self._actions[key] = act

    def _on_action(self, tool_key: str):
        # Uncheck all others (radio-button feel)
        for k, a in self._actions.items():
            if k != tool_key:
                a.setChecked(False)
        self._dispatcher.dispatch_tool_key(tool_key)

    def add_snap_button(self, callback):
        """Add the snap-settings button with a programmatic icon."""
        act = QAction(_style.snap_toolbar_icon(), "Snap", self)
        act.setToolTip("Snap Settings  (SNAP / OS)")
        act.setCheckable(False)
        act.setObjectName("cad_snap")
        act.triggered.connect(callback)
        self.addAction(act)
        self._actions["snap"] = act

    def set_active_tool(self, tool_key: str | None):
        for k, a in self._actions.items():
            if k == "snap":
                continue   # snap button is never "checked" as a tool
            a.setChecked(k == tool_key)
