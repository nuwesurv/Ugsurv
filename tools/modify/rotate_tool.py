# -*- coding: utf-8 -*-
"""RotateTool — SELECTING → base point → live angle preview → click/type angle → commit."""

import math
from qgis.core import QgsPointXY

from ._modify_base import _ModifyBase, _rotate_geom, selected_features
from ...core.base_tool import ToolState
from ...core import style as _style


class RotateTool(_ModifyBase):

    def _commit_transform(self, dest_pt: QgsPointXY):
        if self._base_pt is None:
            return
        angle_deg = math.degrees(
            math.atan2(dest_pt.y() - self._base_pt.y(),
                       dest_pt.x() - self._base_pt.x())
        )
        self._apply_rotation(angle_deg)

    def _handle_act_value(self, value: float):
        if self._base_pt:
            self._apply_rotation(value)

    def _apply_rotation(self, angle_deg: float):
        if self._base_pt is None:
            return
        for layer, feat in selected_features(self._ctx):
            new_geom = _rotate_geom(feat.geometry(), self._base_pt, angle_deg)
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
        angle_deg = math.degrees(
            math.atan2(cursor_pt.y() - self._base_pt.y(),
                       cursor_pt.x() - self._base_pt.x())
        )
        for layer, feat in selected_features(self._ctx):
            rb = self._new_rubber_band(
                feat.geometry().type(), _style.PREVIEW_ROTATE, 1
            )
            rb.setToGeometry(_rotate_geom(feat.geometry(), self._base_pt, angle_deg))
