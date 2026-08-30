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


_TOOL_DEFS = [
    # (tool_key, label, tooltip, icon_filename)
    # ── Selection / Utility ─────────────────────────────────────────────
    ("select",      "Select",    "Select features (S)",          "select.png"),
    ("erase",       "Erase",     "Erase selected features (E)",  "erase.png"),
    ("stretch",     "Stretch",   "Stretch (crossing window)",    "stretch.png"),
    (None, None, None, None),  # separator
    # ── Drawing ─────────────────────────────────────────────────────────
    ("point",       "Point",     "Point (PT)",                   "point.png"),
    ("line",        "Line",      "Line (L)",                     "line.png"),
    ("polyline",    "Polyline",  "Polyline (PL)",                "polyline.png"),
    ("rectangle",   "Rectangle", "Rectangle (REC)",              "rectangle.png"),
    ("circle",      "Circle",    "Circle (C)",                   "circle.png"),
    ("arc",         "Arc",       "Arc (A)",                      "arc.png"),
    ("polygon",     "Polygon",   "Regular Polygon (POL)",        "polygon.png"),
    (None, None, None, None),
    # ── Modify ──────────────────────────────────────────────────────────
    ("move",        "Move",      "Move (M)",                     "move.png"),
    ("copy",        "Copy",      "Copy (CP)",                    "copy.png"),
    ("rotate",      "Rotate",    "Rotate (RO)",                  "rotate.png"),
    ("scale",       "Scale",     "Scale (SC)",                   "scale.png"),
    ("mirror",      "Mirror",    "Mirror (MI)",                  "mirror.png"),
    ("offset",      "Offset",    "Offset (O)",                   "offset.png"),
    ("trim",        "Trim",      "Trim (TR)",                    "trim.png"),
    ("extend",      "Extend",    "Extend (EX)",                  "extend.png"),
    ("fillet",      "Fillet",    "Fillet (F)",                   "fillet.png"),
    ("array",       "Array",     "Array (AR)",                   "array.png"),
    (None, None, None, None),
    # ── Vertex Edit ─────────────────────────────────────────────────────
    ("grip_edit",   "Grips",     "Grip-based vertex edit (V)",   "grip.png"),
    ("add_vertex",  "Add Vertex","Add vertex (AV)",              "add_vertex.png"),
    ("remove_vertex","Del Vertex","Remove vertex (RV)",          "rem_vertex.png"),
    ("break",       "Break",     "Break (BR)",                   "break.png"),
    ("join",        "Join",      "Join (J)",                     "join.png"),
    (None, None, None, None),
    # ── Annotation ──────────────────────────────────────────────────────
    ("dimension",   "Dimension", "Dimension (DIM)",              "dimension.png"),
    ("text",        "Text",      "Text (T)",                     "text.png"),
]


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

    def add_snap_button(self, callback, icon_path: str = ""):
        """Add a separator then a non-checkable snap-settings button."""
        self.addSeparator()
        icon = QIcon(icon_path) if icon_path and os.path.exists(icon_path) else QIcon()
        act  = QAction(icon, "Snap", self)
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
