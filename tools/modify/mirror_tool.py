# -*- coding: utf-8 -*-
"""
MirrorTool — SELECTING → click mirror-line point 1 → click point 2 →
live flip preview → confirm (with erase-source option in PENDING_CONFIRM).
"""

import math
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes

from ._modify_base import _ModifyBase, selected_features
from ...core.base_tool import ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style
from ...core.circle_utils import (
    is_circle, circle_params, build_circle_geom,
    update_circle_attrs, set_circle_attrs_on_feature,
)
from qgis.core import QgsFeature


def _mirror_point(pt: QgsPointXY, p1: QgsPointXY, p2: QgsPointXY) -> QgsPointXY:
    dx = p2.x() - p1.x()
    dy = p2.y() - p1.y()
    len_sq = dx*dx + dy*dy
    if len_sq < 1e-12:
        return pt
    t = ((pt.x() - p1.x()) * dx + (pt.y() - p1.y()) * dy) / len_sq
    fx = p1.x() + t * dx
    fy = p1.y() + t * dy
    return QgsPointXY(2*fx - pt.x(), 2*fy - pt.y())


def _mirror_geometry(geom: QgsGeometry, p1: QgsPointXY, p2: QgsPointXY) -> QgsGeometry:
    if is_circle(geom):
        center, radius = circle_params(geom)
        new_center = _mirror_point(center, p1, p2)
        return build_circle_geom(new_center, radius)

    verts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
    mirrored = [_mirror_point(v, p1, p2) for v in verts]
    wtype = int(QgsWkbTypes.geometryType(geom.wkbType()))
    if wtype == 0:   # Point
        g = QgsGeometry.fromMultiPointXY(mirrored)
    elif wtype == 1:  # Line
        g = QgsGeometry.fromPolylineXY(mirrored)
        g.convertToMultiType()
    else:             # Polygon
        g = QgsGeometry.fromPolygonXY([mirrored])
        g.convertToMultiType()
    return g


class MirrorTool(_ModifyBase):

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._mirror_p1: QgsPointXY | None = None
        self._mirror_p2: QgsPointXY | None = None
        self._erase_source = False

    def activate(self):
        self._mirror_p1 = self._mirror_p2 = None
        super().activate()
        if self._state == ToolState.ACTING:
            self._request_input("xy", "Click first point of mirror axis:")

    def _on_event(self, sem: SemanticEvent):
        if self._state == ToolState.PENDING_CONFIRM:
            if sem.type == EventType.CONFIRM:
                self._execute_mirror(erase=False)
            elif sem.type == EventType.KEY_CHAR and sem.char == 'Y':
                self._execute_mirror(erase=True)
            return
        # Fix prompt when Enter confirms selection and auto-transitions to ACTING
        if (self._state == ToolState.SELECTING
                and sem.type == EventType.CONFIRM
                and not self._ctx.selection_model.is_empty()):
            self._transition(ToolState.ACTING)
            self._update_prompt("Click first point of mirror axis:")
            return
        super()._on_event(sem)

    def _finish_sel_drag(self, start, end, shift):
        super()._finish_sel_drag(start, end, shift)
        if self._state == ToolState.ACTING:
            self._update_prompt("Click first point of mirror axis:")

    def _handle_act_point(self, pt: QgsPointXY):
        if self._mirror_p1 is None:
            self._mirror_p1 = pt
            self._show_base_marker(pt)
            self._update_prompt("Click second point of mirror axis:")
        else:
            self._mirror_p2 = pt
            self._transition(ToolState.PENDING_CONFIRM)
            self._request_input("no_value", "Enter = copy  /  Y = move (erase source):")

    def _on_hover(self, sem: SemanticEvent):
        if sem.point is None:
            return
        if self._state == ToolState.ACTING:
            dyn = getattr(self._ctx, 'dyn_widget', None)
            if dyn:
                dyn.set_live_pair(sem.point.x(), sem.point.y())
        if (self._state in (ToolState.ACTING, ToolState.PENDING_CONFIRM)
                and self._mirror_p1 is not None):
            self._update_preview(sem.point)

    def _execute_mirror(self, erase: bool):  # noqa: C901
        if self._mirror_p1 is None or self._mirror_p2 is None:
            return
        hist = getattr(self._ctx, 'action_history', None)
        if hist:
            hist.begin_group()
        for layer, feat in selected_features(self._ctx):
            orig_geom = feat.geometry()
            new_geom  = _mirror_geometry(orig_geom, self._mirror_p1, self._mirror_p2)
            if not layer.isEditable():
                layer.startEditing()
            if erase:
                layer.changeGeometry(feat.id(), new_geom)
                if hist:
                    hist.record_step(layer.id())
                if is_circle(orig_geom):
                    new_center, new_radius = circle_params(new_geom)
                    update_circle_attrs(layer, feat.id(), new_center, new_radius)
                    if hist:
                        hist.record_step(layer.id())
            else:
                new_feat = QgsFeature(layer.fields())
                new_feat.setGeometry(new_geom)
                new_feat.setAttributes(feat.attributes())
                if is_circle(orig_geom):
                    new_center, new_radius = circle_params(new_geom)
                    set_circle_attrs_on_feature(new_feat, new_center, new_radius)
                layer.addFeature(new_feat)
                if hist:
                    hist.record_step(layer.id())
        if hist:
            hist.end_group()
        self._ctx.selection_model.clear()
        self._clear_rubber_bands()
        self._mirror_p1 = self._mirror_p2 = None
        self._go_home()

    def _update_preview(self, cursor_pt: QgsPointXY):
        self._clear_rubber_bands()
        p1 = self._mirror_p1
        p2 = cursor_pt if self._mirror_p2 is None else self._mirror_p2
        if p1 is None:
            return
        # axis line
        axis_rb = self._new_rubber_band(
            QgsWkbTypes.LineGeometry, _style.RB_MIRROR_AXIS, _style.RB_WIDTH
        )
        axis_rb.addPoint(p1)
        axis_rb.addPoint(p2)
        for layer, feat in selected_features(self._ctx):
            rb = self._new_rubber_band(
                feat.geometry().type(), _style.RB_MIRROR, _style.RB_WIDTH
            )
            rb.setToGeometry(_mirror_geometry(feat.geometry(), p1, p2))

    def _on_cancel_hook(self):
        self._mirror_p1 = self._mirror_p2 = None
        self._clear_base_marker()
