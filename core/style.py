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
# All rubber bands use exactly 3 colors, dashed style, width 1.
# Never scatter raw QColor literals — add a semantic alias below instead.

# The 3 base colors
RB_RED    = QColor(220,  30,  30, 220)   # red    — hover / destructive
RB_ORANGE = QColor(255, 140,   0, 220)   # orange — drawing preview / ghost
RB_GREEN  = QColor(  0, 200,  80, 220)   # green  — source selection / reference

# Faint fills for polygon-type rubber bands (same hue, very low alpha)
RB_RED_FILL    = QColor(220,  30,  30,  18)
RB_ORANGE_FILL = QColor(255, 140,   0,  18)
RB_GREEN_FILL  = QColor(  0, 200,  80,  18)

# Line style and widths
RB_LINE_STYLE   = Qt.PenStyle.DashLine
RB_WIDTH        = 1    # standard — thin but visible
RB_WIDTH_THICK  = 2    # for destructive-op indicators (trim target)
RB_WIDTH_SELECT = 1    # drag-selection window box

# Pure-primary colours for identify/pick tools (TopologySolver, AppendGeometry, …)
# Stroke is fully opaque; fill is near-invisible so the map underneath shows through.
RB_IDENTIFY_RED        = QColor(255,   0,   0)
RB_IDENTIFY_RED_FILL   = QColor(255,   0,   0,  10)
RB_IDENTIFY_BLUE       = QColor(  0,   0, 255)
RB_IDENTIFY_BLUE_FILL  = QColor(  0,   0, 255,  10)
RB_IDENTIFY_WIDTH      = RB_WIDTH_THICK   # 2 — prominent but not heavy

# ── Semantic aliases ──────────────────────────────────────────────────────────
RB_DRAW         = RB_ORANGE          # live drawing preview (vertices being placed)
RB_DRAW_FILL    = RB_ORANGE_FILL
RB_HOVER        = RB_RED             # cursor hovering over a candidate feature
RB_SOURCE       = RB_GREEN           # geometry selected as input for an operation
RB_SOURCE_FILL  = RB_GREEN_FILL
RB_PREVIEW      = RB_ORANGE          # ghost showing destination / result
RB_PREVIEW_FILL = RB_ORANGE_FILL
RB_EDGE         = RB_GREEN           # confirmed reference / cutting edges
RB_DESTROY      = RB_RED             # segment that will be removed (trim, break)
RB_EXTEND       = RB_GREEN           # extension boundary
RB_OFFSET_GUIDE = RB_ORANGE          # perpendicular measurement guide
RB_ANGLE_ARC    = RB_ORANGE          # angle arc / reference line (polyline)
RB_MIRROR_AXIS  = RB_RED             # mirror axis line
RB_MIRROR       = RB_ORANGE          # mirrored geometry ghost

# Aliases kept for backward compatibility
PREVIEW_DRAW   = RB_DRAW
MIRROR_AXIS    = RB_MIRROR_AXIS
PREVIEW_MIRROR = RB_MIRROR


# ══ Selection rubber-bands ════════════════════════════════════════════════════
SELECT_WIN_FILL      = QColor(  0, 120, 255,  60)
SELECT_WIN_BORDER    = QColor(  0, 120, 255, 200)
SELECT_CROSS_FILL    = QColor(  0, 200,   0,  60)
SELECT_CROSS_BORDER  = QColor(  0, 200,   0, 200)
STRETCH_CROSS_FILL   = QColor(  0, 200,   0,  50)
STRETCH_CROSS_BORDER = QColor(  0, 200,   0, 200)


# ══ Selection rubber-band overlay ════════════════════════════════════════════
SELECT_OVERLAY        = QColor(255, 180,   0, 220)   # amber outline
SELECT_OVERLAY_FILL   = QColor(255, 180,   0,  30)   # faint amber fill

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
