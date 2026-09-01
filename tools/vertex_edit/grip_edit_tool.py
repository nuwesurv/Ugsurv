# -*- coding: utf-8 -*-
"""
GripEditTool — grip-based vertex editing (§6).

Selecting objects shows their vertices as grip markers automatically.
Click-dragging a grip is a PendingAction that reshapes adjacent segments live.

Topology-aware: if QgsProject.topologicalEditing() is on, coincident
vertices on neighbouring features move together.

Right-click on a hot grip opens: Move / Rotate / Scale / Mirror submenu.
"""

import math
from dataclasses import dataclass, field
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsProject, QgsWkbTypes,
    QgsFeatureRequest, QgsRectangle,
)
from qgis.gui import QgsVertexMarker

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


GRIP_TOLERANCE_PX = 8


@dataclass
class Grip:
    layer_id: str
    fid:      int
    vertex_idx: int
    pt:       QgsPointXY
    marker:   object = None   # QgsVertexMarker


class GripEditTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._grips:      list[Grip]       = []
        self._hot_grip:   Grip | None      = None
        self._drag_start: QgsPointXY | None = None
        self._drag_geoms: dict             = {}   # {(lid,fid): original_wkt}

    # ── activation ────────────────────────────────────────────────────────
    def activate(self):
        super().activate()
        self._rebuild_grips()
        sel = self._ctx.selection_model
        sel.selectionChanged.connect(self._rebuild_grips)

    def deactivate(self):
        sel = self._ctx.selection_model
        try:
            sel.selectionChanged.disconnect(self._rebuild_grips)
        except Exception:
            pass
        self._clear_grips()
        super().deactivate()

    # ── grip building ─────────────────────────────────────────────────────
    def _rebuild_grips(self):
        self._clear_grips()
        sel = self._ctx.selection_model
        for lid, fid in sel:
            layer = QgsProject.instance().mapLayer(lid)
            if layer is None:
                continue
            feat = layer.getFeature(fid)
            if not feat.isValid():
                continue
            for i, v in enumerate(feat.geometry().vertices()):
                pt = QgsPointXY(v.x(), v.y())
                marker = QgsVertexMarker(self.canvas())
                marker.setCenter(pt)
                marker.setColor(_style.GRIP_COLOR)
                marker.setIconSize(10)
                marker.setIconType(QgsVertexMarker.ICON_BOX)
                self._grips.append(Grip(lid, fid, i, pt, marker))

    def _clear_grips(self):
        for g in self._grips:
            try:
                self.canvas().scene().removeItem(g.marker)
            except Exception:
                pass
        self._grips.clear()
        self._hot_grip = None

    # ── canvas overrides ──────────────────────────────────────────────────
    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            raw = self._translator._canvas_point(event, self._ctx)
            hot = self._grip_at(raw)
            if hot:
                self._hot_grip  = hot
                self._drag_start = raw
                self._snapshot_drag_geom(hot)
                return
        super().canvasPressEvent(event)

    def canvasMoveEvent(self, event):
        if self._hot_grip and self._drag_start:
            raw = self._translator._canvas_point(event, self._ctx)
            self._move_grip_preview(self._hot_grip, raw)
            return
        super().canvasMoveEvent(event)

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._hot_grip:
            raw = self._translator._canvas_point(event, self._ctx)
            self._commit_grip_move(self._hot_grip, raw)
            self._hot_grip  = None
            self._drag_start = None
            self._drag_geoms.clear()
            self._rebuild_grips()
            return

    # ── grip logic ─────────────────────────────────────────────────────────
    def _grip_at(self, pt: QgsPointXY) -> Grip | None:
        tol = self._px_to_mu(GRIP_TOLERANCE_PX)
        best, best_d = None, tol
        for g in self._grips:
            d = math.hypot(g.pt.x() - pt.x(), g.pt.y() - pt.y())
            if d < best_d:
                best_d = d
                best = g
        return best

    def _snapshot_drag_geom(self, grip: Grip):
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer:
            feat = layer.getFeature(grip.fid)
            if feat.isValid():
                self._drag_geoms[(grip.layer_id, grip.fid)] = feat.geometry().asWkt()

    def _move_grip_preview(self, grip: Grip, new_pt: QgsPointXY):
        self._clear_rubber_bands()
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        wkt = self._drag_geoms.get((grip.layer_id, grip.fid))
        if wkt is None:
            return
        geom = QgsGeometry.fromWkt(wkt)
        moved = _replace_vertex(geom, grip.vertex_idx, new_pt)
        rb = self._new_rubber_band(
            moved.type(),
            _style.PREVIEW_GRIP, 1
        )
        rb.setToGeometry(moved)

    def _commit_grip_move(self, grip: Grip, new_pt: QgsPointXY):
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        wkt = self._drag_geoms.get((grip.layer_id, grip.fid))
        if wkt is None:
            return
        geom  = QgsGeometry.fromWkt(wkt)
        moved = _replace_vertex(geom, grip.vertex_idx, new_pt)
        if not layer.isEditable():
            layer.startEditing()
        layer.changeGeometry(grip.fid, moved)
        self._clear_rubber_bands()

        # topological editing
        if QgsProject.instance().topologicalEditing():
            self._fix_topology(grip, new_pt)

    def _fix_topology(self, grip: Grip, new_pt: QgsPointXY):
        tol = self._px_to_mu(2)
        old_pt = grip.pt
        rect = QgsRectangle(
            old_pt.x()-tol, old_pt.y()-tol,
            old_pt.x()+tol, old_pt.y()+tol,
        )
        sm = self._ctx.storage_manager
        for attr in ("points_layer", "lines_layer"):
            lyr = getattr(sm, attr, None)
            if not (lyr and lyr.isValid()):
                continue
            for feat in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                if (lyr.id(), feat.id()) == (grip.layer_id, grip.fid):
                    continue
                geom = feat.geometry()
                for vi, v in enumerate(geom.vertices()):
                    vpt = QgsPointXY(v.x(), v.y())
                    if math.hypot(vpt.x()-old_pt.x(), vpt.y()-old_pt.y()) <= tol:
                        new_geom = _replace_vertex(geom, vi, new_pt)
                        if not lyr.isEditable():
                            lyr.startEditing()
                        lyr.changeGeometry(feat.id(), new_geom)

    def _px_to_mu(self, px: int) -> float:
        try:
            return px * self.canvas().mapUnitsPerPixel()
        except Exception:
            return px * 0.001

    def _on_event(self, sem: SemanticEvent):
        pass  # grip drag is handled directly in canvas overrides


def _replace_vertex(geom: QgsGeometry, idx: int, new_pt: QgsPointXY) -> QgsGeometry:
    """Return a copy of geom with vertex at idx replaced by new_pt."""
    pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
    if 0 <= idx < len(pts):
        pts[idx] = new_pt
    wtype    = int(QgsWkbTypes.geometryType(geom.wkbType()))
    is_multi = QgsWkbTypes.isMultiType(geom.wkbType())
    if wtype == 0:   # Point
        g = QgsGeometry.fromMultiPointXY(pts) if is_multi else QgsGeometry.fromPointXY(pts[0])
    elif wtype == 1:  # Line
        g = QgsGeometry.fromPolylineXY(pts)
        if is_multi:
            g.convertToMultiType()
    else:             # Polygon
        g = QgsGeometry.fromPolygonXY([pts])
        if is_multi:
            g.convertToMultiType()
    return g
