# -*- coding: utf-8 -*-
"""
ArrayTool — SELECTING → parameters (rectangular, polar, or path) → live
preview → commit.

Default: rectangular.  'P' key switches to polar; 'T' to path.
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsPointXY, QgsGeometry, QgsFeature

from ._modify_base import _ModifyBase, _translate_geom, selected_features
from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class ArrayTool(_ModifyBase):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._array_type = "rect"   # rect | polar | path
        self._rows    = 3
        self._cols    = 3
        self._row_gap = 1.0
        self._col_gap = 1.0
        self._count   = 6      # polar
        self._angle   = 360.0  # polar total
        self._center: QgsPointXY | None = None

    def activate(self):
        # Skip _ModifyBase.activate() which sets up a base-point/destination flow
        # that array doesn't use.
        BaseTool.activate(self)
        self._base_pt = None
        self._center  = None
        sel = self._ctx.selection_model
        if sel and not sel.is_empty():
            self._transition(ToolState.ACTING)
            self._request_input("no_value", self._prompt())
        else:
            self._transition(ToolState.SELECTING)
            self._request_input("no_value", "Select features [Enter=confirm]:")

    def _prompt(self) -> str:
        if self._array_type == "rect":
            return (f"Rect {self._rows}×{self._cols}  gap {self._col_gap:.1f}/{self._row_gap:.1f}"
                    f"  [P=polar  Enter=execute]:")
        center_str = "click center" if self._center is None else "center set"
        return (f"Polar ×{self._count}  {self._angle:.0f}°  {center_str}"
                f"  [R=rect  Enter=execute]:")

    def canvasReleaseEvent(self, event):
        # Let base handle drag-selection; after it may auto-transition to ACTING
        # with the wrong prompt — fix it here.
        super().canvasReleaseEvent(event)
        if self._state == ToolState.ACTING:
            self._request_input("no_value", self._prompt())

    def _on_event(self, sem: SemanticEvent):
        if self._state == ToolState.SELECTING:
            # Delegate picking (POINT_PICKED / SHIFT_CLICK) to base class; it
            # also handles CONFIRM → ACTING transition, but sets the wrong prompt.
            super()._on_event(sem)
            if self._state == ToolState.ACTING:
                # Base transitioned on CONFIRM — fix the input prompt for array.
                self._request_input("no_value", self._prompt())
            return

        if self._state == ToolState.ACTING:
            if sem.type == EventType.KEY_CHAR:
                ch = sem.char
                if ch == 'P':   self._array_type = "polar"
                elif ch == 'R': self._array_type = "rect"
                elif ch == 'T': self._array_type = "path"
                self._update_prompt(self._prompt())
            elif sem.type == EventType.CONFIRM:
                self._execute_array()
            elif sem.type == EventType.POINT_PICKED and sem.point:
                if self._array_type == "polar" and self._center is None:
                    self._center = sem.point
                    self._update_prompt(self._prompt())

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
            for r in range(self._rows):
                for c in range(self._cols):
                    if r == 0 and c == 0:
                        continue
                    new_geom = _translate_geom(feat.geometry(),
                                               c * self._col_gap,
                                               r * self._row_gap)
                    nf = QgsFeature(layer.fields())
                    nf.setGeometry(new_geom)
                    nf["cad_layer"] = feat["cad_layer"]
                    layer.addFeature(nf)

    def _exec_polar(self):
        if self._center is None:
            return
        for layer, feat in selected_features(self._ctx):
            if not layer.isEditable():
                layer.startEditing()
            for i in range(1, self._count):
                angle_deg = self._angle * i / self._count
                g = QgsGeometry(feat.geometry())
                g.rotate(angle_deg, self._center)
                nf = QgsFeature(layer.fields())
                nf.setGeometry(g)
                nf["cad_layer"] = feat["cad_layer"]
                layer.addFeature(nf)

    def _update_preview(self, cursor_pt: QgsPointXY):
        pass   # simplified: no live array preview
