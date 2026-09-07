# -*- coding: utf-8 -*-
"""
_ModifyBase — shared logic for modify tools that operate on a selection.

Lifecycle:
  If selection non-empty on activate  → ACTING immediately (noun-verb).
  If selection empty on activate      → SELECTING (verb-noun):
      click = pick one feature
      drag  = window/crossing box (auto-transitions to ACTING when done)
      Enter = confirm selection → ACTING
  ACTING: click base point → click destination → commit → IDLE.

Subclasses override _commit_transform() and _update_preview().
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsRectangle, QgsProject,
    QgsWkbTypes, QgsFeatureRequest,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


# ── geometry helpers (used by subclasses) ─────────────────────────────────────

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
    verts = [(v.x(), v.y()) for v in g.vertices()]
    if not verts:
        return g
    new_verts = [QgsPointXY(
        center.x() + (x - center.x()) * sx,
        center.y() + (y - center.y()) * sy,
    ) for x, y in verts]
    wtype = int(QgsWkbTypes.geometryType(g.wkbType()))
    if wtype == 1:
        return QgsGeometry.fromPolylineXY(new_verts)
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


# ── base class ─────────────────────────────────────────────────────────────────

class _ModifyBase(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._base_pt: QgsPointXY | None = None
        self._preview_rbs = []
        self._base_marker: QgsVertexMarker | None = None
        # for SELECTING-state drag box
        self._sel_drag_start: QgsPointXY | None = None
        self._sel_drag_rb: QgsRubberBand | None  = None

    def activate(self):
        super().activate()
        self._base_pt = None
        sel = self._ctx.selection_model
        if sel and not sel.is_empty():
            self._transition(ToolState.ACTING)
            self._request_input("xy", "Click base point:")
        else:
            self._transition(ToolState.SELECTING)
            self._request_input("no_value", "Select features:")

    # ── canvas overrides for SELECTING drag ──────────────────────────────
    def canvasPressEvent(self, event):
        if (self._state == ToolState.SELECTING
                and event.button() == Qt.MouseButton.LeftButton):
            self._sel_drag_start = self._translator._canvas_point(event, self._ctx)
        super().canvasPressEvent(event)

    def canvasReleaseEvent(self, event):
        if (self._state == ToolState.SELECTING
                and event.button() == Qt.MouseButton.LeftButton
                and self._sel_drag_start is not None):
            end = self._translator._canvas_point(event, self._ctx)
            dx = abs(end.x() - self._sel_drag_start.x())
            dy = abs(end.y() - self._sel_drag_start.y())
            if dx > 1e-6 or dy > 1e-6:
                shift = bool(int(event.modifiers()) &
                             Qt.KeyboardModifier.ShiftModifier)
                self._finish_sel_drag(self._sel_drag_start, end, shift)
            self._sel_drag_start = None
            if self._sel_drag_rb:
                self._sel_drag_rb.reset()
                self._sel_drag_rb = None

    def canvasMoveEvent(self, event):
        if self._state == ToolState.SELECTING and self._sel_drag_start:
            end = self._translator._canvas_point(event, self._ctx)
            self._update_sel_drag_preview(self._sel_drag_start, end)
        super().canvasMoveEvent(event)

    # ── semantic event handler ────────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):  # noqa: C901
        if self._state == ToolState.SELECTING:
            if sem.type == EventType.POINT_PICKED:
                self._pick_at_point(sem.point, shift=False)
            elif sem.type == EventType.SHIFT_CLICK:
                self._pick_at_point(sem.point, shift=True)
            elif sem.type == EventType.CONFIRM:
                if not self._ctx.selection_model.is_empty():
                    self._transition(ToolState.ACTING)
                    self._update_prompt("Click base point:")

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
            self._update_prompt("Click destination point:")
        else:
            self._commit_transform(pt)
            self._clear_base_marker()

    def _handle_act_value(self, value: float):
        pass

    def _commit_transform(self, dest_pt: QgsPointXY):
        pass

    def _on_hover(self, sem: SemanticEvent):
        if self._state == ToolState.ACTING and self._base_pt and sem.point:
            self._update_preview(sem.point)

    def _update_preview(self, cursor_pt: QgsPointXY):
        pass

    # ── SELECTING state — feature picking ────────────────────────────────
    def _pick_at_point(self, pt: QgsPointXY, shift: bool):
        if pt is None:
            return
        sel = self._ctx.selection_model
        if not shift:
            sel.clear()
        tol = (self._ctx.snap_engine._px_to_map_units(5, self._ctx.canvas)
               if self._ctx.snap_engine else 0.001)
        rect = QgsRectangle(pt.x() - tol, pt.y() - tol,
                            pt.x() + tol, pt.y() + tol)
        for layer_id, layer in self._all_geometry_layers():
            for feat in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                if shift:
                    sel.toggle(layer_id, feat.id())
                else:
                    sel.add(layer_id, feat.id())
                return  # one feature per click

    def _finish_sel_drag(self, start: QgsPointXY, end: QgsPointXY, shift: bool):
        is_window = end.x() >= start.x()
        rect = QgsRectangle(
            min(start.x(), end.x()), min(start.y(), end.y()),
            max(start.x(), end.x()), max(start.y(), end.y()),
        )
        sel = self._ctx.selection_model
        if not shift:
            sel.clear()
        hits = []
        for layer_id, layer in self._all_geometry_layers():
            for feat in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                geom = feat.geometry()
                if is_window:
                    if rect.contains(geom.boundingBox()):
                        hits.append((layer_id, feat.id()))
                else:
                    if geom.intersects(QgsGeometry.fromRect(rect)):
                        hits.append((layer_id, feat.id()))
        if hits:
            sel.add_batch(hits)
            # auto-transition: drawn the box, got features → go straight to ACTING
            self._transition(ToolState.ACTING)
            self._update_prompt("Click base point:")

    def _update_sel_drag_preview(self, start: QgsPointXY, end: QgsPointXY):
        if self._sel_drag_rb is None:
            self._sel_drag_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            self._sel_drag_rb.setWidth(_style.RB_WIDTH_SELECT)
        is_window = end.x() >= start.x()
        self._sel_drag_rb.setColor(
            _style.SELECT_WIN_BORDER if is_window else _style.SELECT_CROSS_BORDER
        )
        self._sel_drag_rb.setFillColor(
            _style.SELECT_WIN_FILL if is_window else _style.SELECT_CROSS_FILL
        )
        self._sel_drag_rb.setToGeometry(QgsGeometry.fromRect(QgsRectangle(start, end)))

    def _all_geometry_layers(self):
        sm = self._ctx.storage_manager
        result = []
        for attr in ("points_layer", "lines_layer"):
            lyr = getattr(sm, attr, None)
            if lyr and lyr.isValid():
                result.append((lyr.id(), lyr))
        return result

    # ── base-point marker ────────────────────────────────────────────────
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
            except Exception:  # nosec B110
                pass
            self._base_marker = None

    def _on_cancel_hook(self):
        self._clear_base_marker()
        self._base_pt = None
        self._sel_drag_start = None
        if self._sel_drag_rb:
            try:
                self._sel_drag_rb.reset()
            except Exception:  # nosec B110
                pass
            self._sel_drag_rb = None
