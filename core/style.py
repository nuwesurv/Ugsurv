# -*- coding: utf-8 -*-
"""
Central style constants for the UgSurv CAD plugin.

Import this module wherever consistent colors or cursors are needed.
  from . import style          (from within core/)
  from ...core import style    (from tools/ subpackages)
  from ..core import style     (from ui/)

Never scatter raw QColor literals across tool files — add a constant here
and reference it.
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from qgis.gui import QgsVertexMarker


# ══ Snap marker ═══════════════════════════════════════════════════════════════
_CC_COLOR = QColor(66, 135, 245)    # unified blue for all snap indicators
_CC_SIZE  = 9                       # icon size, pixels
_CC_PEN_W = 2                       # pen width
_SNAP_PX  = 10                      # pixel radius for snap search tolerance

SNAP_ICON_SIZE = _CC_SIZE
SNAP_PEN_WIDTH = _CC_PEN_W

# Safe fallback: ICON_TRIANGLE was added in QGIS 3.26
_ICON_TRIANGLE = getattr(QgsVertexMarker, 'ICON_TRIANGLE',
                         QgsVertexMarker.ICON_DOUBLE_TRIANGLE)

# Icon shape per snap type — used by base_tool._SNAP_STYLES
SNAP_ICON = {
    'endpoint':     QgsVertexMarker.ICON_BOX,           # vertex of line/polygon
    'point':        QgsVertexMarker.ICON_CIRCLE,        # point feature
    'center':       QgsVertexMarker.ICON_CROSS,
    'midpoint':     _ICON_TRIANGLE,
    'intersection': QgsVertexMarker.ICON_X,
    'nearest':      QgsVertexMarker.ICON_DOUBLE_TRIANGLE,
}

# Individual snap colors kept as fallback / future reference
SNAP_GRID_COLOR      = QColor(160, 160, 160)   # grid stays gray (distinct category)


# ══ Rubber-band standards ════════════════════════════════════════════════════
# All rubber bands use exactly 2 colors: pure red and pure blue, dashed style, width 2.
# Never scatter raw QColor literals — add a semantic alias below instead.

# The 2 base colors (fully opaque stroke, near-invisible fill)
RB_RED       = QColor(255,   0,   0)      # red  — primary / active / hover / destructive
RB_BLUE      = QColor(  0,   0, 255)      # blue — secondary / reference / source / preview

# Faint fills for polygon-type rubber bands
RB_RED_FILL  = QColor(255,   0,   0,  10)
RB_BLUE_FILL = QColor(  0,   0, 255,  10)

# Line style and width — single standard across all rubber bands
RB_LINE_STYLE   = Qt.PenStyle.DashLine
RB_WIDTH        = 2    # standard
RB_WIDTH_THICK  = 2    # kept for backward-compat
RB_WIDTH_SELECT = 2    # drag-selection window box

# Identify / pick tool aliases (TopologySolver, AppendGeometry, …)
RB_IDENTIFY_RED        = RB_RED
RB_IDENTIFY_RED_FILL   = RB_RED_FILL
RB_IDENTIFY_BLUE       = RB_BLUE
RB_IDENTIFY_BLUE_FILL  = RB_BLUE_FILL
RB_IDENTIFY_WIDTH      = RB_WIDTH

# ── Semantic aliases ──────────────────────────────────────────────────────────
RB_DRAW         = RB_RED             # live drawing preview (vertices being placed)
RB_DRAW_FILL    = RB_RED_FILL
RB_HOVER        = RB_RED             # cursor hovering over a candidate feature
RB_SOURCE       = RB_BLUE            # geometry selected as input for an operation
RB_SOURCE_FILL  = RB_BLUE_FILL
RB_PREVIEW      = RB_RED             # ghost showing destination / result
RB_PREVIEW_FILL = RB_RED_FILL
RB_EDGE         = RB_BLUE            # confirmed reference / cutting edges
RB_DESTROY      = RB_RED             # segment that will be removed (trim, break)
RB_EXTEND       = RB_BLUE            # extension boundary
RB_OFFSET_GUIDE = RB_BLUE            # perpendicular measurement guide
RB_ANGLE_ARC    = RB_BLUE            # angle arc / reference line (polyline)
RB_MIRROR_AXIS  = RB_RED             # mirror axis line
RB_MIRROR       = RB_BLUE            # mirrored geometry ghost

# Aliases kept for backward compatibility
PREVIEW_DRAW   = RB_DRAW
MIRROR_AXIS    = RB_MIRROR_AXIS
PREVIEW_MIRROR = RB_MIRROR


# ══ Selection rubber-bands ════════════════════════════════════════════════════
SELECT_WIN_FILL      = QColor(  0,   0, 255,  60)
SELECT_WIN_BORDER    = QColor(  0,   0, 255, 200)
SELECT_CROSS_FILL    = QColor(255,   0,   0,  60)
SELECT_CROSS_BORDER  = QColor(255,   0,   0, 200)
STRETCH_CROSS_FILL   = QColor(255,   0,   0,  50)
STRETCH_CROSS_BORDER = QColor(255,   0,   0, 200)


# ══ Selection rubber-band overlay ════════════════════════════════════════════
SELECT_OVERLAY        = QColor(  0,   0, 255, 220)   # blue outline
SELECT_OVERLAY_FILL   = QColor(  0,   0, 255,  30)   # faint blue fill

# ══ Vertex / grip markers ═════════════════════════════════════════════════════
GRIP_COLOR    = QColor(  0, 120, 255)   # blue boxes on selected features
MIDGRIP_COLOR = QColor(100, 220,  80)   # green plus signs at edge midpoints
BASE_PT_COLOR = QColor(255, 165,   0)   # orange crosshair (transform base point)


# ══ CAD cursor ════════════════════════════════════════════════════════════════
_cad_cursor: "QCursor | None" = None


def snap_toolbar_icon() -> QIcon:
    """Blue crosshair-with-box icon for the snap settings toolbar button."""
    size, c, gap, box = 18, 8, 2, 2
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setPen(QPen(_CC_COLOR, 2))
    p.drawLine(0, c, c - gap - box, c)
    p.drawLine(c + gap + box, c, size - 1, c)
    p.drawLine(c, 0, c, c - gap - box)
    p.drawLine(c, c + gap + box, c, size - 1)
    p.setPen(QPen(_CC_COLOR, 1))
    p.drawRect(c - box, c - box, box * 2, box * 2)
    p.end()
    return QIcon(px)


def ortho_toolbar_icon() -> QIcon:
    """Amber right-angle icon for the ortho toggle toolbar button."""
    size, m = 18, 3
    col = QColor(220, 140, 0)
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    cx, cy = m, size - m - 1   # corner: left edge, bottom edge
    p.setPen(QPen(col, 2))
    p.drawLine(cx, m, cx, cy)                    # vertical arm
    p.drawLine(cx, cy, size - m - 1, cy)         # horizontal arm
    # small right-angle mark at the corner
    box = 4
    p.setPen(QPen(col, 1))
    p.drawLine(cx, cy - box, cx + box, cy - box)
    p.drawLine(cx + box, cy - box, cx + box, cy)
    p.end()
    return QIcon(px)


def cad_crosshair_cursor() -> QCursor:
    """Red precision crosshair cursor for all CAD drawing tools. Cached after first call."""
    global _cad_cursor
    if _cad_cursor is None:
        size, c, gap, box = 41, 20, 5, 3
        px = QPixmap(size, size)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setPen(QPen(QColor(220, 30, 30), 1))
        p.drawLine(0, c, c - gap, c)
        p.drawLine(c + gap, c, size - 1, c)
        p.drawLine(c, 0, c, c - gap)
        p.drawLine(c, c + gap, c, size - 1)
        p.drawRect(c - box, c - box, box * 2, box * 2)
        p.end()
        _cad_cursor = QCursor(px, c, c)
    return _cad_cursor
