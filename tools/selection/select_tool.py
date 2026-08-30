# -*- coding: utf-8 -*-
"""
SelectTool — click, window-drag, crossing-drag, Shift-toggle.

Lifecycle: SELECTING only.  Feeds SelectionModel.

Window drag  (left → right, fully enclosed) vs
Crossing drag (right → left, touch counts).
"""

from qgis.PyQt.QtCore import Qt, QPoint, QRect
from qgis.core import (
    QgsPointXY, QgsRectangle, QgsGeometry, QgsWkbTypes,
    QgsProject, QgsFeatureRequest,
)
from qgis.gui import QgsRubberBand

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


class SelectTool(BaseTool):
    CURSOR = Qt.CursorShape.ArrowCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._drag_start: QgsPointXY | None  = None
        self._drag_rb: QgsRubberBand | None   = None

    # ── override canvasPressEvent to capture drag start ──────────────────
    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = self._translator._canvas_point(event, self._ctx)
        sem = self._translator.translate_press(event, self._ctx)
        self._dispatch(sem)

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start:
            end_pt = self._translator._canvas_point(event, self._ctx)
            self._finish_drag(self._drag_start, end_pt,
                              int(event.modifiers()))
            self._drag_start = None
            if self._drag_rb:
                self._drag_rb.reset()
                self._drag_rb = None

    def canvasMoveEvent(self, event):
        if self._drag_start:
            end_pt = self._translator._canvas_point(event, self._ctx)
            self._update_drag_preview(self._drag_start, end_pt)
        super().canvasMoveEvent(event)

    # ── semantic event handler ────────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.POINT_PICKED:
            self._pick_at_point(sem.point, shift=False)

        elif sem.type == EventType.SHIFT_CLICK:
            self._pick_at_point(sem.point, shift=True)

        elif sem.type == EventType.CONFIRM:
            pass  # selection stays for the next tool

    def _on_hover(self, sem: SemanticEvent):
        pass  # no preview needed for SELECT in hover

    # ── drag rubber-band preview ──────────────────────────────────────────
    def _update_drag_preview(self, start: QgsPointXY, end: QgsPointXY):
        if self._drag_rb is None:
            self._drag_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            self._drag_rb.setWidth(1)
        is_window = end.x() >= start.x()
        color  = _style.SELECT_WIN_FILL    if is_window else _style.SELECT_CROSS_FILL
        border = _style.SELECT_WIN_BORDER  if is_window else _style.SELECT_CROSS_BORDER
        self._drag_rb.setColor(border)
        self._drag_rb.setFillColor(color)
        rect = QgsGeometry.fromRect(QgsRectangle(start, end))
        self._drag_rb.setToGeometry(rect)

    # ── point pick ────────────────────────────────────────────────────────
    def _pick_at_point(self, pt: QgsPointXY, shift: bool):
        sel = self._ctx.selection_model
        if not shift:
            sel.clear()
        tol = self._ctx.snap_engine._px_to_map_units(
            5, self._ctx.canvas
        ) if self._ctx.snap_engine else 0.001
        rect = QgsRectangle(pt.x()-tol, pt.y()-tol,
                            pt.x()+tol, pt.y()+tol)
        for layer_id, layer in self._all_geometry_layers():
            for feat in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                if shift:
                    sel.toggle(layer_id, feat.id())
                else:
                    sel.add(layer_id, feat.id())
                return  # one feature per click

    # ── box selection ─────────────────────────────────────────────────────
    def _finish_drag(self, start: QgsPointXY, end: QgsPointXY, modifiers: int):
        if abs(start.x() - end.x()) < 1e-6 and abs(start.y() - end.y()) < 1e-6:
            return  # micro-drag: treat as single click, already handled in press
        is_window   = end.x() >= start.x()   # left→right = window
        rect = QgsRectangle(
            min(start.x(), end.x()), min(start.y(), end.y()),
            max(start.x(), end.x()), max(start.y(), end.y()),
        )
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        sel   = self._ctx.selection_model
        if not shift:
            sel.clear()
        for layer_id, layer in self._all_geometry_layers():
            for feat in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                geom = feat.geometry()
                if is_window:
                    # fully enclosed
                    if QgsRectangle(geom.boundingBox()).contains(rect) or \
                       rect.contains(geom.boundingBox()):
                        sel.add(layer_id, feat.id())
                else:
                    # crossing: geometry touches rect
                    if geom.intersects(QgsGeometry.fromRect(rect)):
                        sel.add(layer_id, feat.id())

    def _all_geometry_layers(self):
        sm = self._ctx.storage_manager
        result = []
        for attr, _label in [
            ("points_layer", "points"),
            ("lines_layer", "lines"),
            ("polygons_layer", "polygons"),
        ]:
            lyr = getattr(sm, attr, None)
            if lyr and lyr.isValid():
                result.append((lyr.id(), lyr))
        return result

    def _on_cancel_hook(self):
        if self._drag_rb:
            self._drag_rb.reset()
            self._drag_rb = None
        self._drag_start = None
