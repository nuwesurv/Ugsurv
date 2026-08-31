# -*- coding: utf-8 -*-
"""Semantic event types produced by InputTranslator and consumed by tools."""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class EventType(Enum):
    POINT_PICKED     = auto()   # left-click, carries snapped point
    POINT_HOVER      = auto()   # mouse move, carries snapped point
    CONFIRM          = auto()   # Enter / Spacebar / right-click (context)
    CANCEL           = auto()   # Esc
    DELETE           = auto()   # Delete key
    SHIFT_CLICK      = auto()   # Shift+left-click (toggle selection)
    CTRL_CLICK       = auto()   # Ctrl+left-click (cycle overlapping)
    COORDINATE_ENTERED = auto() # typed "x,y" / "@dx,dy" / "@dist<angle"
    VALUE_ENTERED    = auto()   # typed numeric value (radius, distance, angle…)
    UNDO_STEP        = auto()   # Ctrl+Z mid-command (step back one point)
    KEY_CHAR         = auto()   # single character key (C=close, A=arc, W=width…)


class SnapType(Enum):
    NONE         = "none"
    VERTEX       = "vertex"       # endpoint / node of a line or polygon edge
    POINT        = "point"        # standalone point feature
    MIDPOINT     = "midpoint"
    CENTER       = "center"
    INTERSECTION = "intersection"
    PERPENDICULAR= "perpendicular"
    EXTENSION    = "extension"
    GRID         = "grid"
    SELF         = "self"


@dataclass
class SemanticEvent:
    type:       EventType
    point:      object = None          # QgsPointXY or None
    snap_type:  SnapType = SnapType.NONE
    value:      object = None          # numeric or string for COORDINATE/VALUE_ENTERED
    char:       str = ""               # for KEY_CHAR events
    modifiers:  int = 0                # Qt.KeyboardModifiers bitmask
    raw:        object = None          # original Qt event, kept for edge cases
