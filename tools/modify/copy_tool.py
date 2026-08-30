# -*- coding: utf-8 -*-
"""
CopyTool — same as MoveTool, original retained.
Loops for multiple copies until Esc/Enter.
"""

from qgis.core import QgsPointXY, QgsFeature

from ._modify_base import _ModifyBase, _translate_geom, selected_features
from ...core.base_tool import ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


class CopyTool(_ModifyBase):

    def _commit_transform(self, dest_pt: QgsPointXY):
        if self._base_pt is None:
            return
        dx = dest_pt.x() - self._base_pt.x()
        dy = dest_pt.y() - self._base_pt.y()
        for layer, feat in selected_features(self._ctx):
            new_geom = _translate_geom(feat.geometry(), dx, dy)
            if not layer.isEditable():
                layer.startEditing()
            new_feat = QgsFeature(layer.fields())
            new_feat.setGeometry(new_geom)
            new_feat["cad_layer"] = feat["cad_layer"]
            layer.addFeature(new_feat)
        # keep selection for repeated copies; just reset base point
        self._clear_rubber_bands()
        self._base_pt = None
        # Stay in ACTING so user can click more copy destinations

    def _update_preview(self, cursor_pt: QgsPointXY):
        self._clear_rubber_bands()
        if self._base_pt is None:
            return
        dx = cursor_pt.x() - self._base_pt.x()
        dy = cursor_pt.y() - self._base_pt.y()
        for layer, feat in selected_features(self._ctx):
            rb = self._new_rubber_band(
                feat.geometry().type(), _style.PREVIEW_COPY, 1
            )
            geom = _translate_geom(feat.geometry(), dx, dy)
            rb.setToGeometry(geom)
