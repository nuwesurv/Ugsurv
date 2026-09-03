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

Right-click on a line endpoint shows a context menu:
  Extend     — add one vertex in the endpoint's outward bearing direction
  Add Points — continue drawing from that endpoint (multi-vertex append)

Topology-aware: if QgsProject.topologicalEditing() is on, coincident
vertices on neighbouring features move together.
"""

import math
from dataclasses import dataclass
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import QMenu
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
    layer_id:    str
    fid:         int
    vertex_idx:  int
    pt:          QgsPointXY
    marker:      object = None   # QgsVertexMarker
    is_endpoint: bool   = False  # True for first/last vertex of a single-part line


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

        # extend mode: right-click endpoint → type distance or click new endpoint
        self._extend_grip:      Grip | None  = None
        self._extend_dir:       tuple | None = None   # (dx, dy) unit outward vector
        self._extend_orig_pts:  list | None  = None

        # add-points mode: right-click endpoint → append multiple vertices interactively
        self._add_pts_grip:     Grip | None  = None
        self._add_pts_orig_pts: list | None  = None
        self._add_pts_new:      list         = []
        self._add_pts_rb:       object       = None   # solid rubber band for committed new segments

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
            geom     = feat.geometry()
            is_line  = (QgsWkbTypes.geometryType(geom.wkbType())
                        == QgsWkbTypes.GeometryType.LineGeometry)
            is_multi = geom.isMultipart()
            verts    = list(geom.vertices())
            n        = len(verts)
            for i, v in enumerate(verts):
                pt    = QgsPointXY(v.x(), v.y())
                is_ep = is_line and not is_multi and n >= 2 and (i == 0 or i == n - 1)
                marker = QgsVertexMarker(self.canvas())
                marker.setCenter(pt)
                marker.setColor(_style.GRIP_COLOR)
                marker.setIconSize(10)
                marker.setIconType(QgsVertexMarker.ICON_BOX)
                self._grips.append(Grip(lid, fid, i, pt, marker, is_endpoint=is_ep))

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
        btn = event.button()

        # ── right-click: active-mode commit/cancel only (menu shown on release) ──
        if btn == Qt.MouseButton.RightButton:
            if self._add_pts_grip is not None:
                if self._add_pts_new:
                    self._commit_add_pts()
                else:
                    self._cancel_add_pts()
                return
            if self._extend_grip is not None:
                if self._last_snap_pt:
                    self._commit_extend(self._last_snap_pt)
                else:
                    self._cancel_extend()
                return

        # ── left-click ────────────────────────────────────────────────────
        if btn == Qt.MouseButton.LeftButton:
            if self._hot_mode and self._hot_grip:
                # Second click while in hot mode → commit at snapped position
                sem = self._translator.translate_press(event, self._ctx)
                if sem and sem.point:
                    self._commit_grip_and_finish(sem.point)
                return

            # Skip grip detection when already in an endpoint mode
            if self._extend_grip is None and self._add_pts_grip is None:
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
        btn = event.button()

        # ── right-click release in idle state → endpoint context menu ─────
        if btn == Qt.MouseButton.RightButton:
            if (not self._hot_mode and self._hot_grip is None
                    and self._extend_grip is None and self._add_pts_grip is None):
                raw = self._translator._canvas_point(event, self._ctx)
                eg  = self._endpoint_grip_at(raw)
                if eg:
                    self._show_endpoint_menu(eg)
                    return

        # ── left-click release: drag commit or enter hot mode ─────────────
        if (btn == Qt.MouseButton.LeftButton
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

    # ── hover (all modes) ────────────────────────────────────────────────
    def _on_hover(self, sem: SemanticEvent):
        if self._extend_grip is not None and sem.point:
            self._update_extend_preview(sem)
            return

        if self._add_pts_grip is not None and sem.point:
            self._update_add_pts_preview(sem)
            return

        if self._hot_mode and self._hot_grip and sem.point:
            self._last_snap_pt = sem.point
            self._move_grip_preview(self._hot_grip, sem.point)
            self._update_extension_guide(sem.snap_type, sem.point)

    # ── keyboard events ───────────────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):
        if self._extend_grip is not None:
            self._handle_extend_event(sem)
            return

        if self._add_pts_grip is not None:
            self._handle_add_pts_event(sem)
            return

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

    def _on_undo_step(self):
        if self._add_pts_grip and self._add_pts_new:
            self._add_pts_new.pop()
            self._update_add_pts_committed()
            return
        self._do_cancel()

    # ── endpoint context menu ─────────────────────────────────────────────
    def _endpoint_grip_at(self, pt: QgsPointXY) -> 'Grip | None':
        """Return the closest endpoint grip within tolerance, or None."""
        tol = self._px_to_mu(GRIP_TOLERANCE_PX)
        best, best_d = None, tol
        for g in self._grips:
            if not g.is_endpoint:
                continue
            d = math.hypot(g.pt.x() - pt.x(), g.pt.y() - pt.y())
            if d < best_d:
                best_d, best = d, g
        return best

    def _show_endpoint_menu(self, grip: 'Grip'):
        menu = QMenu(self.canvas())
        act_extend = menu.addAction("Extend line")
        act_add    = menu.addAction("Add points")
        chosen = menu.exec_(QCursor.pos())
        if chosen == act_extend:
            self._start_extend(grip)
        elif chosen == act_add:
            self._start_add_points(grip)

    # ── extend mode ───────────────────────────────────────────────────────
    def _start_extend(self, grip: 'Grip'):
        """Enter extend mode: one new vertex appended in the outward bearing direction."""
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        feat = layer.getFeature(grip.fid)
        if not feat.isValid():
            return
        pts = [QgsPointXY(v.x(), v.y()) for v in feat.geometry().vertices()]
        if len(pts) < 2:
            return

        if grip.vertex_idx == 0:
            ep, adj = pts[0], pts[1]
        else:
            ep, adj = pts[-1], pts[-2]
        dx = ep.x() - adj.x()
        dy = ep.y() - adj.y()
        dist = math.hypot(dx, dy)
        if dist < 1e-10:
            return

        self._extend_grip     = grip
        self._extend_dir      = (dx / dist, dy / dist)
        self._extend_orig_pts = pts
        self._request_input("value", "Extension distance / click new endpoint:")

    def _handle_extend_event(self, sem: SemanticEvent):
        if sem.type == EventType.VALUE_ENTERED:
            try:
                dist = float(sem.value)
            except (TypeError, ValueError):
                return
            dx, dy = self._extend_dir
            grip   = self._extend_grip
            ep     = self._extend_orig_pts[0] if grip.vertex_idx == 0 else self._extend_orig_pts[-1]
            self._commit_extend(QgsPointXY(ep.x() + dist * dx, ep.y() + dist * dy))

        elif sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED) and sem.point:
            self._commit_extend(sem.point)

        elif sem.type == EventType.CONFIRM and self._last_snap_pt:
            self._commit_extend(self._last_snap_pt)

    def _update_extend_preview(self, sem: SemanticEvent):
        self._last_snap_pt = sem.point
        grip    = self._extend_grip
        orig    = self._extend_orig_pts
        preview = ([sem.point] + orig) if grip.vertex_idx == 0 else (orig + [sem.point])
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_PREVIEW, 1
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        for i, p in enumerate(preview):
            self._preview_rb.addPoint(p, i == len(preview) - 1)
        self._update_snap_marker(sem)

    def _commit_extend(self, new_pt: QgsPointXY):
        grip  = self._extend_grip
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        pts = list(self._extend_orig_pts)
        if grip.vertex_idx == 0:
            pts = [new_pt] + pts
        else:
            pts = pts + [new_pt]
        new_geom = QgsGeometry.fromPolylineXY(pts)
        if not layer.isEditable():
            layer.startEditing()
        layer.changeGeometry(grip.fid, new_geom)
        self._cancel_extend()
        self._rebuild_grips()

    def _cancel_extend(self):
        self._extend_grip     = None
        self._extend_dir      = None
        self._extend_orig_pts = None
        self._last_snap_pt    = None
        self._clear_rubber_bands()
        self._preview_rb      = None
        self._ext_guide_rb    = None

    # ── add-points mode ───────────────────────────────────────────────────
    def _start_add_points(self, grip: 'Grip'):
        """Enter add-points mode: append multiple vertices from the endpoint."""
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        feat = layer.getFeature(grip.fid)
        if not feat.isValid():
            return
        pts = [QgsPointXY(v.x(), v.y()) for v in feat.geometry().vertices()]
        if len(pts) < 2:
            return

        # Always append to the end; if start vertex chosen, reverse first
        if grip.vertex_idx == 0:
            pts = list(reversed(pts))

        self._add_pts_grip     = grip
        self._add_pts_orig_pts = pts
        self._add_pts_new      = []
        self._request_input("polar", "Specify next point [Enter=commit]:")

    def _handle_add_pts_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED) and sem.point:
            self._add_pts_new.append(sem.point)
            self._last_snap_pt = sem.point
            self._update_add_pts_committed()
            n = len(self._add_pts_new)
            self._request_input("polar", f"Next point [{n} added | Enter=commit]:")

        elif sem.type == EventType.VALUE_ENTERED:
            pt = self._try_extension_distance(sem.value)
            if pt:
                self._add_pts_new.append(pt)
                self._last_snap_pt = pt
                self._update_add_pts_committed()

        elif sem.type == EventType.CONFIRM:
            if self._add_pts_new:
                self._commit_add_pts()
            else:
                self._cancel_add_pts()

    def _update_add_pts_preview(self, sem: SemanticEvent):
        self._last_snap_pt = sem.point
        anchor = self._add_pts_new[-1] if self._add_pts_new else self._add_pts_orig_pts[-1]
        if self._preview_rb is None:
            self._preview_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_PREVIEW, 1
            )
        self._preview_rb.reset(QgsWkbTypes.LineGeometry)
        self._preview_rb.addPoint(anchor, False)
        self._preview_rb.addPoint(sem.point, True)
        self._update_snap_marker(sem)
        self._update_extension_guide(sem.snap_type, sem.point)

    def _update_add_pts_committed(self):
        if not self._add_pts_new:
            return
        if self._add_pts_rb is None:
            self._add_pts_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_DRAW, _style.RB_WIDTH + 1
            )
            self._add_pts_rb.setLineStyle(Qt.PenStyle.SolidLine)
        self._add_pts_rb.reset(QgsWkbTypes.LineGeometry)
        chain = [self._add_pts_orig_pts[-1]] + self._add_pts_new
        for i, p in enumerate(chain):
            self._add_pts_rb.addPoint(p, i == len(chain) - 1)

    def _commit_add_pts(self):
        grip  = self._add_pts_grip
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        pts      = list(self._add_pts_orig_pts) + self._add_pts_new
        new_geom = QgsGeometry.fromPolylineXY(pts)
        if not layer.isEditable():
            layer.startEditing()
        layer.changeGeometry(grip.fid, new_geom)
        self._cancel_add_pts()
        self._rebuild_grips()

    def _cancel_add_pts(self):
        self._add_pts_grip     = None
        self._add_pts_orig_pts = None
        self._add_pts_new      = []
        self._add_pts_rb       = None
        self._last_snap_pt     = None
        self._clear_rubber_bands()
        self._preview_rb       = None
        self._ext_guide_rb     = None

    # ── grip geometry ──────────────────────────────────────────────────────
    def _grip_at(self, pt: QgsPointXY) -> 'Grip | None':
        tol = self._px_to_mu(GRIP_TOLERANCE_PX)
        best, best_d = None, tol
        for g in self._grips:
            d = math.hypot(g.pt.x() - pt.x(), g.pt.y() - pt.y())
            if d < best_d:
                best_d = d
                best   = g
        return best

    def _snapshot_drag_geom(self, grip: 'Grip'):
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer:
            feat = layer.getFeature(grip.fid)
            if feat.isValid():
                self._drag_geoms[(grip.layer_id, grip.fid)] = feat.geometry().asWkt()

    def _move_grip_preview(self, grip: 'Grip', new_pt: QgsPointXY):
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

    def _commit_grip_move(self, grip: 'Grip', new_pt: QgsPointXY):
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

    def _fix_topology(self, grip: 'Grip', new_pt: QgsPointXY):
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
        self._hot_grip        = None
        self._hot_mode        = False
        self._drag_start_px   = None
        self._last_snap_pt    = None
        self._drag_geoms.clear()
        self._preview_rb      = None
        self._extend_grip     = None
        self._extend_dir      = None
        self._extend_orig_pts = None
        self._add_pts_grip     = None
        self._add_pts_orig_pts = None
        self._add_pts_new      = []
        self._add_pts_rb       = None


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
