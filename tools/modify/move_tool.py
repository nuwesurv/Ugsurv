# -*- coding: utf-8 -*-
"""MoveTool — SELECTING → base point → destination → commit translated geometry."""

from qgis.core import QgsPointXY, QgsProject, QgsWkbTypes
from qgis.gui import QgsRubberBand

from ._modify_base import _ModifyBase, _translate_geom, selected_features
from ...core.base_tool import ToolState
from ...core import style as _style


class MoveTool(_ModifyBase):

    def _commit_transform(self, dest_pt: QgsPointXY):
        if self._base_pt is None:
            return
        dx = dest_pt.x() - self._base_pt.x()
        dy = dest_pt.y() - self._base_pt.y()
        for layer, feat in selected_features(self._ctx):
            new_geom = _translate_geom(feat.geometry(), dx, dy)
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
        dx = cursor_pt.x() - self._base_pt.x()
        dy = cursor_pt.y() - self._base_pt.y()
        for layer, feat in selected_features(self._ctx):
            rb = self._new_rubber_band(
                feat.geometry().type(), _style.PREVIEW_MOVE, 1
            )
            geom = _translate_geom(feat.geometry(), dx, dy)
            rb.setToGeometry(geom)
