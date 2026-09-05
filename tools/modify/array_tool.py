# -*- coding: utf-8 -*-
"""
ArrayTool — SELECTING → enter parameters → commit.

Rect:  select → Rows/Cols → X/Y spacing → execute.
Polar: select → Count/Angle → click center → execute.

'P' key switches to polar; 'R' switches back to rect (any sub-state in ACTING).
Enter with empty fields uses current defaults and advances.
"""

import math

from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsFeature

from ._modify_base import _ModifyBase, _translate_geom, selected_features
from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core.circle_utils import (
    is_circle, circle_params, build_circle_geom, set_circle_attrs_on_feature,
)


class ArrayTool(_ModifyBase):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._array_type = "rect"
        self._rows    = 3
        self._cols    = 3
        self._row_gap = 1.0
        self._col_gap = 1.0
        self._count   = 6
        self._angle   = 360.0
        self._center: QgsPointXY | None = None
        self._sub     = None   # sub-state inside ACTING

    # ── lifecycle ─────────────────────────────────────────────────────────

    def activate(self):
        # Skip _ModifyBase.activate() — array uses its own parameter flow,
        # not the base-point/destination pattern.
        BaseTool.activate(self)
        self._base_pt = None
        self._center  = None
        self._sub     = None
        sel = self._ctx.selection_model
        if sel and not sel.is_empty():
            self._transition(ToolState.ACTING)
            self._enter_param_phase()
        else:
            self._transition(ToolState.SELECTING)
            self._request_input("no_value", "Select features [Enter=confirm]:")

    def canvasReleaseEvent(self, event):
        # Base handles drag-box selection; after it may auto-transition to
        # ACTING with the wrong prompt — patch it here.
        super().canvasReleaseEvent(event)
        if self._state == ToolState.ACTING and self._sub is None:
            self._enter_param_phase()

    # ── parameter phase ───────────────────────────────────────────────────

    def _enter_param_phase(self):
        if self._array_type == "rect":
            self._sub = "rows_cols"
            self._request_input("rowcol", "Rows, Cols [P=polar]:")
            self._set_live(float(self._rows), float(self._cols))
        else:
            self._sub = "count_angle"
            self._request_input("count_ang", "Count, Angle° [R=rect]:")
            self._set_live(float(self._count), self._angle)

    def _set_live(self, a: float, b: float):
        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn and hasattr(dyn, 'set_live_pair'):
            dyn.set_live_pair(a, b)

    # ── event handling ────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if self._state == ToolState.SELECTING:
            # Delegate to base (handles POINT_PICKED, SHIFT_CLICK, CONFIRM→ACTING).
            super()._on_event(sem)
            if self._state == ToolState.ACTING:
                # Base transitioned on CONFIRM — start parameter entry.
                self._enter_param_phase()
            return

        if self._state == ToolState.ACTING:
            # Mode switch available from any sub-state.
            if sem.type == EventType.KEY_CHAR:
                ch = sem.char
                if ch == 'P' and self._array_type != "polar":
                    self._array_type = "polar"
                    self._center = None
                    self._sub = None
                    self._enter_param_phase()
                    return
                if ch == 'R' and self._array_type != "rect":
                    self._array_type = "rect"
                    self._sub = None
                    self._enter_param_phase()
                    return

            if self._sub == "rows_cols":
                if sem.type == EventType.COORDINATE_ENTERED and sem.point:
                    self._rows = max(1, int(round(sem.point.x())))
                    self._cols = max(1, int(round(sem.point.y())))
                    self._sub = "spacing"
                    self._request_input("dxdy", "X spacing, Y spacing:")
                    self._set_live(self._col_gap, self._row_gap)
                elif sem.type == EventType.CONFIRM:
                    # Enter with no typed values → keep defaults, advance.
                    self._sub = "spacing"
                    self._request_input("dxdy", "X spacing, Y spacing:")
                    self._set_live(self._col_gap, self._row_gap)

            elif self._sub == "spacing":
                if sem.type == EventType.COORDINATE_ENTERED and sem.point:
                    self._col_gap = sem.point.x()
                    self._row_gap = sem.point.y()
                    self._execute_array()
                elif sem.type == EventType.CONFIRM:
                    self._execute_array()

            elif self._sub == "count_angle":
                if sem.type == EventType.COORDINATE_ENTERED and sem.point:
                    self._count = max(2, int(round(sem.point.x())))
                    self._angle = sem.point.y()
                    self._sub = "pick_center"
                    self._request_input("no_value", "Click center point:")
                elif sem.type == EventType.CONFIRM:
                    # keep defaults, advance to center pick
                    self._sub = "pick_center"
                    self._request_input("no_value", "Click center point:")

            elif self._sub == "pick_center":
                if sem.type == EventType.POINT_PICKED and sem.point:
                    self._center = sem.point
                    self._execute_array()

    # ── execution ─────────────────────────────────────────────────────────

    def _execute_array(self):
        if self._array_type == "rect":
            self._exec_rect()
        elif self._array_type == "polar":
            self._exec_polar()
        self._ctx.selection_model.clear()
        self._clear_rubber_bands()
        self._go_home()

    def _exec_rect(self):
        for layer, feat in selected_features(self._ctx):
            if not layer.isEditable():
                layer.startEditing()
            geom = feat.geometry()
            circ = is_circle(geom)
            if circ:
                orig_center, orig_radius = circle_params(geom)
            for r in range(self._rows):
                for c in range(self._cols):
                    if r == 0 and c == 0:
                        continue
                    dx = c * self._col_gap
                    dy = -r * self._row_gap
                    if circ:
                        new_center = QgsPointXY(orig_center.x() + dx,
                                                orig_center.y() + dy)
                        new_geom = build_circle_geom(new_center, orig_radius)
                    else:
                        new_geom = _translate_geom(geom, dx, dy)
                    nf = QgsFeature(layer.fields())
                    nf.setGeometry(new_geom)
                    nf["cad_layer"] = feat["cad_layer"]
                    if circ:
                        set_circle_attrs_on_feature(nf, new_center, orig_radius)
                    layer.addFeature(nf)

    def _exec_polar(self):
        if self._center is None:
            return
        cx, cy = self._center.x(), self._center.y()
        for layer, feat in selected_features(self._ctx):
            if not layer.isEditable():
                layer.startEditing()
            geom = feat.geometry()
            circ = is_circle(geom)
            if circ:
                orig_center, orig_radius = circle_params(geom)
            for i in range(1, self._count):
                angle_deg = self._angle * i / self._count
                if circ:
                    a = math.radians(angle_deg)
                    cos_a, sin_a = math.cos(a), math.sin(a)
                    dx = orig_center.x() - cx
                    dy = orig_center.y() - cy
                    new_center = QgsPointXY(cx + dx * cos_a - dy * sin_a,
                                            cy + dx * sin_a + dy * cos_a)
                    new_geom = build_circle_geom(new_center, orig_radius)
                else:
                    g = QgsGeometry(geom)
                    g.rotate(angle_deg, self._center)
                    new_geom = g
                nf = QgsFeature(layer.fields())
                nf.setGeometry(new_geom)
                nf["cad_layer"] = feat["cad_layer"]
                if circ:
                    set_circle_attrs_on_feature(nf, new_center, orig_radius)
                layer.addFeature(nf)

    def _update_preview(self, cursor_pt: QgsPointXY):
        pass
