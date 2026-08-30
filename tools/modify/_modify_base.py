# -*- coding: utf-8 -*-
"""
_ModifyBase — shared logic for modify tools that operate on a selection.

Lifecycle: SELECTING (Enter confirms) → ACTING → commit → IDLE.
Subclasses provide _on_base_point() and _on_destination().
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsProject, QgsWkbTypes
from qgis.gui import QgsVertexMarker

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


def _translate_geom(geom: QgsGeometry, dx: float, dy: float) -> QgsGeometry:
    g = QgsGeometry(geom)
    g.translate(dx, dy)
    return g


def _rotate_geom(geom: QgsGeometry, center: QgsPointXY,
                 angle_deg: float) -> QgsGeometry:
    g = QgsGeometry(geom)
    g.rotate(angle_deg, center)
    return g


def _scale_geom(geom: QgsGeometry, center: QgsPointXY,
                sx: float, sy: float) -> QgsGeometry:
    g = QgsGeometry(geom)
    # Scale each vertex manually since QgsGeometry has no scale()
    verts = [(v.x(), v.y()) for v in g.vertices()]
    if not verts:
        return g
    # rebuild
    new_verts = [QgsPointXY(
        center.x() + (x - center.x()) * sx,
        center.y() + (y - center.y()) * sy,
    ) for x, y in verts]
    wtype = int(QgsWkbTypes.geometryType(g.wkbType()))
    if wtype == 1:   # Line
        return QgsGeometry.fromPolylineXY(new_verts)
    if wtype == 2:   # Polygon
        return QgsGeometry.fromPolygonXY([new_verts])
    return QgsGeometry.fromMultiPointXY(new_verts)


def selected_features(ctx):
    """Yield (layer, feat) for all items in SelectionModel."""
    sel = ctx.selection_model
    for lid, fid in sel:
        layer = QgsProject.instance().mapLayer(lid)
        if layer:
            feat = layer.getFeature(fid)
            if feat.isValid():
                yield layer, feat


class _ModifyBase(BaseTool):
    CURSOR = Qt.CursorShape.SizeAllCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._base_pt: QgsPointXY | None = None
        self._preview_rbs = []
        self._base_marker: QgsVertexMarker | None = None

    def activate(self):
        super().activate()
        self._base_pt = None
        # if selection is non-empty, skip SELECTING → go straight to ACTING
        if self._ctx.selection_model and not self._ctx.selection_model.is_empty():
            self._transition(ToolState.ACTING)
        else:
            self._transition(ToolState.SELECTING)

    def _on_event(self, sem: SemanticEvent):
        if self._state == ToolState.SELECTING:
            if sem.type == EventType.CONFIRM:
                if not self._ctx.selection_model.is_empty():
                    self._transition(ToolState.ACTING)
            elif sem.type in (EventType.POINT_PICKED, EventType.SHIFT_CLICK):
                # delegate to select tool behaviour (minimal: just pick)
                pass

        elif self._state == ToolState.ACTING:
            if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
                if sem.point:
                    self._handle_act_point(sem.point)
            elif sem.type == EventType.VALUE_ENTERED:
                self._handle_act_value(float(sem.value))
            elif sem.type == EventType.CONFIRM:
                self._do_cancel()

    def _handle_act_point(self, pt: QgsPointXY):
        if self._base_pt is None:
            self._base_pt = pt
            self._show_base_marker(pt)
        else:
            self._commit_transform(pt)
            self._clear_base_marker()

    def _handle_act_value(self, value: float):
        pass   # subclasses may override for typed distance/angle

    def _commit_transform(self, dest_pt: QgsPointXY):
        pass   # overridden by each modify tool

    def _on_hover(self, sem: SemanticEvent):
        if self._state == ToolState.ACTING and self._base_pt and sem.point:
            self._update_preview(sem.point)

    def _update_preview(self, cursor_pt: QgsPointXY):
        pass   # overridden

    def _show_base_marker(self, pt: QgsPointXY):
        if self._base_marker is None:
            self._base_marker = QgsVertexMarker(self.canvas())
            self._base_marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self._base_marker.setIconSize(14)
            self._base_marker.setPenWidth(2)
            self._base_marker.setColor(_style.BASE_PT_COLOR)
        self._base_marker.setCenter(pt)

    def _clear_base_marker(self):
        if self._base_marker is not None:
            try:
                self.canvas().scene().removeItem(self._base_marker)
            except Exception:
                pass
            self._base_marker = None

    def _on_cancel_hook(self):
        self._clear_base_marker()
        self._base_pt = None
