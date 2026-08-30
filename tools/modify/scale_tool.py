# -*- coding: utf-8 -*-
"""ScaleTool — SELECTING → base point → live scale preview → click/type factor → commit."""

import math
from qgis.core import QgsPointXY

from ._modify_base import _ModifyBase, _scale_geom, selected_features
from ...core.base_tool import ToolState
from ...core import style as _style


class ScaleTool(_ModifyBase):

    def _commit_transform(self, dest_pt: QgsPointXY):
        if self._base_pt is None:
            return
        ref_dist = self._base_pt.distance(dest_pt)
        if ref_dist > 0:
            self._apply_scale(ref_dist)

    def _handle_act_value(self, value: float):
        if self._base_pt and value > 0:
            self._apply_scale(value)

    def _apply_scale(self, factor: float):
        if self._base_pt is None:
            return
        for layer, feat in selected_features(self._ctx):
            new_geom = _scale_geom(feat.geometry(), self._base_pt, factor, factor)
            if not layer.isEditable():
                layer.startEditing()
            layer.changeGeometry(feat.id(), new_geom)
        self._ctx.selection_model.clear()
        self._clear_rubber_bands()
        self._base_pt = None
        self._transition(ToolState.IDLE)

    def _update_preview(self, cursor_pt: QgsPointXY):
        self._clear_rubber_bands()
        if self._base_pt is None:
            return
        factor = self._base_pt.distance(cursor_pt)
        if factor <= 0:
            return
        for layer, feat in selected_features(self._ctx):
            rb = self._new_rubber_band(
                feat.geometry().type(), _style.PREVIEW_SCALE, 1
            )
            rb.setToGeometry(_scale_geom(feat.geometry(), self._base_pt, factor, factor))
