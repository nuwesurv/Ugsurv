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
_SNAP_PX  = 20                      # pixel radius for snap search tolerance

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


# ══ Rubber-band preview colors ════════════════════════════════════════════════
PREVIEW_DEFAULT  = QColor(255, 100,   0, 180)   # BaseTool fallback
PREVIEW_DRAW     = QColor(255, 165,   0, 200)   # drawing tools (line / polyline / circle …)
PREVIEW_MOVE     = QColor(255, 165,   0, 130)   # move
PREVIEW_COPY     = QColor(100, 200, 255, 130)   # copy
PREVIEW_ROTATE   = QColor(255, 200,   0, 130)   # rotate
PREVIEW_SCALE    = QColor(200, 255, 100, 130)   # scale
PREVIEW_MIRROR   = QColor(  0, 200, 200, 130)   # mirror geometry ghost
PREVIEW_STRETCH  = QColor(255, 140,   0, 180)   # stretch vertices
PREVIEW_GRIP     = QColor(255, 165,   0, 180)   # grip-edit vertex drag
MIRROR_AXIS      = QColor(200, 200, 200, 220)   # mirror axis line


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
