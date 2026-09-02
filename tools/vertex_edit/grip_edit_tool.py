# -*- coding: utf-8 -*-
"""
GripEditTool — grip-based vertex editing (§6).

Selecting objects shows their vertices as grip markers automatically.

Two interaction modes:
  DRAG  — press on grip, hold and move, release to commit (classic drag).
           Threshold: >= DRAG_THRESHOLD_PX pixels of movement.
  HOT   — short click on grip (< threshold) enters hover mode; the grip
           follows the snapped cursor.  Commit via:
             • second left-click at snapped position
             • type a distance + Enter (extension snap active)
             • type absolute coordinates (COORDINATE_ENTERED)
             • Enter to confirm at current snap position
           Escape cancels.

Topology-aware: if QgsProject.topologicalEditing() is on, coincident
vertices on neighbouring features move together.
"""

import math
from dataclasses import dataclass
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsGeometry, QgsProject, QgsWkbTypes,
    QgsFeatureRequest, QgsRectangle,
)
from qgis.gui import QgsVertexMarker

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType, SnapType
from ...core import style as _style


GRIP_TOLERANCE_PX = 8
DRAG_THRESHOLD_PX = 8   # pixels; less than this = click, not drag


@dataclass
class Grip:
    layer_id:   str
    fid:        int
    vertex_idx: int
    pt:         QgsPointXY
    marker:     object = None   # QgsVertexMarker


class GripEditTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._grips:         list[Grip]        = []
        self._hot_grip:      Grip | None       = None
        self._hot_mode:      bool              = False   # hover-to-place mode
        self._drag_start_px: object            = None   # QPoint pixel position at press
        self._drag_geoms:    dict              = {}     # {(lid,fid): original_wkt}
        self._last_snap_pt:  QgsPointXY | None = None   # last snapped pt during hover
        self._preview_rb                       = None   # geometry preview rubber band

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
            if self._hot_mode and self._hot_grip:
                # Second click while in hot mode → commit at snapped position
                sem = self._translator.translate_press(event, self._ctx)
                if sem and sem.point:
                    self._commit_grip_and_finish(sem.point)
                return

            raw = self._translator._canvas_point(event, self._ctx)
            hot = self._grip_at(raw)
            if hot:
                self._hot_grip      = hot
                self._drag_start_px = event.pos()
                self._last_snap_pt  = None
                self._snapshot_drag_geom(hot)
                return

        super().canvasPressEvent(event)

    def canvasMoveEvent(self, event):
        if self._hot_mode and self._hot_grip:
            # Hot mode: let BaseTool run the snap engine → calls _on_hover
            super().canvasMoveEvent(event)
            return

        if self._hot_grip and self._drag_start_px:
            # Drag mode: run snap engine for preview + extension guide
            sem = self._translator.translate_move(event, self._ctx)
            pt  = (sem.point if sem and sem.point
                   else self._translator._canvas_point(event, self._ctx))
            self._last_snap_pt = pt
            self._move_grip_preview(self._hot_grip, pt)
            self._update_snap_marker(sem)
            self._update_extension_guide(
                sem.snap_type if sem else SnapType.NONE, pt
            )
            return

        super().canvasMoveEvent(event)

    def canvasReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._hot_grip and not self._hot_mode):
            drag_px = 0
            if self._drag_start_px:
                drag_px = math.hypot(
                    event.pos().x() - self._drag_start_px.x(),
                    event.pos().y() - self._drag_start_px.y(),
                )
            if drag_px >= DRAG_THRESHOLD_PX:
                # Real drag → commit at last snapped position
                pt = (self._last_snap_pt
                      or self._translator._canvas_point(event, self._ctx))
                self._commit_grip_and_finish(pt)
            else:
                # Short click → enter hot mode
                self._hot_mode      = True
                self._drag_start_px = None
                self._request_input("value",
                                    "Extension distance  /  click to place:")
            return

    # ── hover (hot mode) ─────────────────────────────────────────────────
    def _on_hover(self, sem: SemanticEvent):
        if self._hot_mode and self._hot_grip and sem.point:
            self._last_snap_pt = sem.point
            self._move_grip_preview(self._hot_grip, sem.point)
            self._update_extension_guide(sem.snap_type, sem.point)

    # ── keyboard events (hot mode) ────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):
        if not (self._hot_mode and self._hot_grip):
            return

        if sem.type == EventType.VALUE_ENTERED:
            pt = self._try_extension_distance(sem.value)
            if pt:
                self._commit_grip_and_finish(pt)

        elif sem.type == EventType.COORDINATE_ENTERED and sem.point:
            self._commit_grip_and_finish(sem.point)

        elif sem.type == EventType.CONFIRM:
            if self._last_snap_pt:
                self._commit_grip_and_finish(self._last_snap_pt)

    # ── grip geometry ──────────────────────────────────────────────────────
    def _grip_at(self, pt: QgsPointXY) -> Grip | None:
        tol = self._px_to_mu(GRIP_TOLERANCE_PX)
        best, best_d = None, tol
        for g in self._grips:
            d = math.hypot(g.pt.x() - pt.x(), g.pt.y() - pt.y())
            if d < best_d:
                best_d = d
                best   = g
        return best

    def _snapshot_drag_geom(self, grip: Grip):
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer:
            feat = layer.getFeature(grip.fid)
            if feat.isValid():
                self._drag_geoms[(grip.layer_id, grip.fid)] = feat.geometry().asWkt()

    def _move_grip_preview(self, grip: Grip, new_pt: QgsPointXY):
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        wkt = self._drag_geoms.get((grip.layer_id, grip.fid))
        if wkt is None:
            return
        geom  = QgsGeometry.fromWkt(wkt)
        moved = _replace_vertex(geom, grip.vertex_idx, new_pt)
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                moved.type(), _style.RB_PREVIEW, 1
            )
        self._preview_rb.setToGeometry(moved)

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

        if QgsProject.instance().topologicalEditing():
            self._fix_topology(grip, new_pt)

    def _commit_grip_and_finish(self, new_pt: QgsPointXY):
        self._commit_grip_move(self._hot_grip, new_pt)
        self._hot_grip      = None
        self._hot_mode      = False
        self._drag_start_px = None
        self._last_snap_pt  = None
        self._drag_geoms.clear()
        self._clear_rubber_bands()
        self._preview_rb   = None
        self._ext_guide_rb = None
        self._rebuild_grips()

    def _fix_topology(self, grip: Grip, new_pt: QgsPointXY):
        tol    = self._px_to_mu(2)
        old_pt = grip.pt
        rect   = QgsRectangle(
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

    def _on_cancel_hook(self):
        self._hot_grip      = None
        self._hot_mode      = False
        self._drag_start_px = None
        self._last_snap_pt  = None
        self._drag_geoms.clear()
        self._preview_rb    = None


def _replace_vertex(geom: QgsGeometry, idx: int, new_pt: QgsPointXY) -> QgsGeometry:
    """Return a copy of geom with vertex at idx replaced by new_pt."""
    pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
    if 0 <= idx < len(pts):
        pts[idx] = new_pt
    wtype    = int(QgsWkbTypes.geometryType(geom.wkbType()))
    is_multi = QgsWkbTypes.isMultiType(geom.wkbType())
    if wtype == 0:    # Point
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
