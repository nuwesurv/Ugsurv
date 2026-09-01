# -*- coding: utf-8 -*-
"""
SelectTool — unified select / grip-move / edge-vertex-add tool.

No selection:
  Left-click           → pick one feature (deselects others)
  Shift-click          → toggle feature in/out of selection
  Drag left→right      → window selection  (fully enclosed)
  Drag right→left      → crossing selection (touch counts)

Selection active (blue grip boxes + green + marks visible):
  Click a blue box     → arm the grip (turns orange, amber overlay hides);
                         move cursor freely; rubber-band shows edges live;
                         click again to confirm new vertex position
  Click a green +      → insert vertex at midpoint; move cursor; click to place
  Click empty space    → deselect all
  Esc (grip armed)     → cancel vertex move, keep selection, overlay restores
  Esc (no grip armed)  → deselect all
"""

import math
from dataclasses import dataclass

from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsPointXY, QgsRectangle, QgsGeometry, QgsWkbTypes,
    QgsProject, QgsFeatureRequest,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style


GRIP_TOL_PX = 8
MID_TOL_PX  = 10


@dataclass
class Grip:
    layer_id:   str
    fid:        int
    vertex_idx: int
    pt:         QgsPointXY
    marker:     object = None   # QgsVertexMarker


@dataclass
class MidGrip:
    layer_id:      str
    fid:           int
    insert_before: int          # new vertex will be inserted before this index
    pt:            QgsPointXY
    marker:        object = None  # QgsVertexMarker


class SelectTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._grips:         list[Grip]    = []
        self._midgrips:      list[MidGrip] = []
        self._hot_grip:      Grip | None   = None
        self._drag_geom_wkt: str | None    = None  # snapshot for current drag
        self._last_snap_pt:  QgsPointXY | None = None  # last snapped point during grip drag
        self._drag_start:    QgsPointXY | None = None
        self._drag_rb:       QgsRubberBand | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    def activate(self):
        super().activate()
        self._rebuild_grips()
        self._ctx.selection_model.selectionChanged.connect(self._rebuild_grips)

    def deactivate(self):
        try:
            self._ctx.selection_model.selectionChanged.disconnect(self._rebuild_grips)
        except Exception:
            pass
        self._clear_grips()
        super().deactivate()

    # ── canvas events ──────────────────────────────────────────────────────
    def canvasPressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().canvasPressEvent(event)
            return

        raw = self._translator._canvas_point(event, self._ctx)
        sem = self._translator.translate_press(event, self._ctx)
        snapped = sem.point  # honours snap + constraints

        # A grip is already armed: second click confirms the new position.
        if self._hot_grip is not None:
            # Use last snapped point from move if available, otherwise re-snap at click
            place_pt = self._last_snap_pt if self._last_snap_pt is not None else snapped
            self._commit_grip_move(self._hot_grip, place_pt)
            self._hot_grip      = None
            self._drag_geom_wkt = None
            self._last_snap_pt  = None
            self._clear_rubber_bands()
            self._show_overlay()
            self._rebuild_grips()
            return

        # Priority 1: vertex grip hit → arm it (first click, use raw for marker proximity)
        grip = self._grip_at(raw)
        if grip:
            self._hot_grip = grip
            if grip.marker is not None:
                grip.marker.setColor(_style.BASE_PT_COLOR)  # orange = armed
                grip.marker.setIconSize(9)
            layer = QgsProject.instance().mapLayer(grip.layer_id)
            if layer:
                feat = layer.getFeature(grip.fid)
                if feat.isValid():
                    self._drag_geom_wkt = feat.geometry().asWkt()
            self._hide_overlay()
            return

        # Priority 2: midpoint mark hit → insert vertex, arm for placement
        midgrip = self._midgrip_at(raw)
        if midgrip:
            self._setup_midpoint_drag(midgrip)
            return

        # Priority 3: regular click/drag for selection
        self._drag_start = raw
        sem = self._translator.translate_press(event, self._ctx)
        self._dispatch(sem)

    def canvasMoveEvent(self, event):
        if self._hot_grip:
            # Grip armed: rubber-band and snap marker both follow the snapped point
            sem = self._translator.translate_move(event, self._ctx)
            self._update_snap_marker(sem)
            self._move_grip_preview(self._hot_grip, sem.point)
            self._last_snap_pt = sem.point
            return
        if self._drag_start:
            raw = self._translator._canvas_point(event, self._ctx)
            self._update_drag_preview(self._drag_start, raw)
        super().canvasMoveEvent(event)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # Grip placement is confirmed by a second PRESS, not by release.
        # Release only finalises box-selection drags.
        if self._drag_start and self._hot_grip is None:
            end_pt = self._translator._canvas_point(event, self._ctx)
            self._finish_drag(self._drag_start, end_pt, int(event.modifiers()))
            self._drag_start = None
            if self._drag_rb:
                self._drag_rb.reset()
                self._drag_rb = None

    # ── semantic events ────────────────────────────────────────────────────
    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.POINT_PICKED:
            self._pick_at_point(sem.point, shift=False)
        elif sem.type == EventType.SHIFT_CLICK:
            self._pick_at_point(sem.point, shift=True)
        elif sem.type == EventType.DELETE:
            if self._hot_grip is not None:
                self._remove_armed_vertex()
            else:
                self._delete_selected()

    def _remove_armed_vertex(self):
        grip = self._hot_grip
        self._hot_grip      = None
        self._drag_geom_wkt = None
        self._last_snap_pt  = None
        self._clear_rubber_bands()

        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            self._show_overlay()
            return
        feat = layer.getFeature(grip.fid)
        if not feat.isValid():
            self._show_overlay()
            return
        geom  = feat.geometry()
        wtype = int(QgsWkbTypes.geometryType(geom.wkbType()))

        if wtype == 0:  # Point — delete the whole feature
            if not layer.isEditable():
                layer.startEditing()
            layer.deleteFeature(grip.fid)
            self._ctx.selection_model.remove(grip.layer_id, grip.fid)
            # selectionChanged fires → overlay rebuilds automatically
            self._rebuild_grips()
            return

        pts = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
        if wtype == 1 and len(pts) <= 2:
            self._show_overlay()
            return
        if wtype == 2 and len(pts) <= 4:
            self._show_overlay()
            return

        del pts[grip.vertex_idx]
        is_multi = QgsWkbTypes.isMultiType(geom.wkbType())

        if wtype == 1:
            new_geom = QgsGeometry.fromPolylineXY(pts)
            if is_multi:
                new_geom.convertToMultiType()
        else:
            new_geom = QgsGeometry.fromPolygonXY([pts])
            if is_multi:
                new_geom.convertToMultiType()

        if not layer.isEditable():
            layer.startEditing()
        layer.changeGeometry(grip.fid, new_geom)
        # Rebuild overlay AFTER changeGeometry so it reads the updated shape
        self._show_overlay()
        self._rebuild_grips()

    def _delete_selected(self):
        sel = self._ctx.selection_model
        if sel.is_empty():
            return
        count = len(sel)
        for lid, fid in list(sel):
            layer = QgsProject.instance().mapLayer(lid)
            if layer is None:
                continue
            if not layer.isEditable():
                layer.startEditing()
            layer.deleteFeature(fid)
        sel.clear()
        cmd_dock = getattr(self._ctx, 'cmd_dock', None)
        if cmd_dock:
            cmd_dock.log(f"Deleted {count} feature{'s' if count != 1 else ''}.", "#ff8888")

    def _on_hover(self, sem: SemanticEvent):
        pass

    # ── grip / midgrip building ────────────────────────────────────────────
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
            geom  = feat.geometry()
            wtype = int(QgsWkbTypes.geometryType(geom.wkbType()))
            verts = list(geom.vertices())

            # Blue box grips at every vertex
            for i, v in enumerate(verts):
                pt     = QgsPointXY(v.x(), v.y())
                marker = QgsVertexMarker(self.canvas())
                marker.setCenter(pt)
                marker.setColor(_style.GRIP_COLOR)
                marker.setIconSize(7)
                marker.setIconType(QgsVertexMarker.ICON_BOX)
                self._grips.append(Grip(lid, fid, i, pt, marker))

            # Green + marks at edge midpoints (lines and polygons only)
            if wtype in (1, 2):
                for i in range(len(verts) - 1):
                    va, vb = verts[i], verts[i + 1]
                    # skip degenerate closing segment (first == last for polygons)
                    if abs(va.x() - vb.x()) < 1e-10 and abs(va.y() - vb.y()) < 1e-10:
                        continue
                    mx, my = (va.x() + vb.x()) / 2, (va.y() + vb.y()) / 2
                    mpt    = QgsPointXY(mx, my)
                    marker = QgsVertexMarker(self.canvas())
                    marker.setCenter(mpt)
                    marker.setColor(_style.MIDGRIP_COLOR)
                    marker.setIconSize(6)
                    marker.setIconType(QgsVertexMarker.ICON_CROSS)
                    self._midgrips.append(MidGrip(lid, fid, i + 1, mpt, marker))

    def _clear_grips(self):
        for g in self._grips:
            try:
                self.canvas().scene().removeItem(g.marker)
            except Exception:
                pass
        for m in self._midgrips:
            try:
                self.canvas().scene().removeItem(m.marker)
            except Exception:
                pass
        self._grips.clear()
        self._midgrips.clear()

    # ── hit testing ────────────────────────────────────────────────────────
    def _grip_at(self, pt: QgsPointXY) -> Grip | None:
        tol = self._px_to_mu(GRIP_TOL_PX)
        best, best_d = None, tol
        for g in self._grips:
            d = math.hypot(g.pt.x() - pt.x(), g.pt.y() - pt.y())
            if d < best_d:
                best_d, best = d, g
        return best

    def _midgrip_at(self, pt: QgsPointXY) -> MidGrip | None:
        tol = self._px_to_mu(MID_TOL_PX)
        best, best_d = None, tol
        for m in self._midgrips:
            d = math.hypot(m.pt.x() - pt.x(), m.pt.y() - pt.y())
            if d < best_d:
                best_d, best = d, m
        return best

    # ── midpoint insert-then-drag ──────────────────────────────────────────
    def _setup_midpoint_drag(self, midgrip: MidGrip):
        """Insert a vertex at the midpoint and arm the grip for click-to-place."""
        layer = QgsProject.instance().mapLayer(midgrip.layer_id)
        if layer is None:
            return
        feat = layer.getFeature(midgrip.fid)
        if not feat.isValid():
            return
        geom = feat.geometry()
        geom.insertVertex(midgrip.pt.x(), midgrip.pt.y(), midgrip.insert_before)
        self._drag_geom_wkt = geom.asWkt()
        self._hot_grip = Grip(
            midgrip.layer_id, midgrip.fid, midgrip.insert_before, midgrip.pt, None
        )
        self._hide_overlay()
        self._clear_grips()   # hide markers while placing

    # ── grip preview / commit ──────────────────────────────────────────────
    def _move_grip_preview(self, grip: Grip, new_pt: QgsPointXY):
        """Rebuild the rubber-band preview with the vertex moved to new_pt."""
        self._clear_rubber_bands()
        if self._drag_geom_wkt is None:
            return
        geom  = QgsGeometry.fromWkt(self._drag_geom_wkt)
        moved = _replace_vertex(geom, grip.vertex_idx, new_pt)
        rb    = self._new_rubber_band(moved.type(), _style.PREVIEW_GRIP, 2)
        if int(QgsWkbTypes.geometryType(moved.wkbType())) == 0:  # point: make preview visible
            rb.setIconSize(12)
            rb.setIcon(QgsRubberBand.ICON_BOX)
        rb.setToGeometry(moved)

    def _commit_grip_move(self, grip: Grip, new_pt: QgsPointXY):
        if self._drag_geom_wkt is None:
            return
        layer = QgsProject.instance().mapLayer(grip.layer_id)
        if layer is None:
            return
        geom  = QgsGeometry.fromWkt(self._drag_geom_wkt)
        moved = _replace_vertex(geom, grip.vertex_idx, new_pt)
        if not layer.isEditable():
            layer.startEditing()
        layer.changeGeometry(grip.fid, moved)

    # ── selection helpers ──────────────────────────────────────────────────
    def _pick_at_point(self, pt: QgsPointXY, shift: bool):
        sel = self._ctx.selection_model
        if not shift:
            sel.clear()
        tol = (self._ctx.snap_engine._px_to_map_units(5, self._ctx.canvas)
               if self._ctx.snap_engine else 0.001)
        rect = QgsRectangle(pt.x() - tol, pt.y() - tol,
                            pt.x() + tol, pt.y() + tol)
        click_geom = QgsGeometry.fromPointXY(pt)
        best_lid, best_fid, best_d = None, None, float('inf')
        for layer_id, layer in self._all_geometry_layers():
            for feat in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                d = feat.geometry().distance(click_geom)
                if d < best_d:
                    best_d, best_lid, best_fid = d, layer_id, feat.id()
        if best_lid is not None:
            if shift:
                sel.toggle(best_lid, best_fid)
            else:
                sel.add(best_lid, best_fid)

    def _update_drag_preview(self, start: QgsPointXY, end: QgsPointXY):
        if self._drag_rb is None:
            self._drag_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            self._drag_rb.setWidth(1)
        is_window = end.x() >= start.x()
        self._drag_rb.setColor(_style.SELECT_WIN_BORDER if is_window
                               else _style.SELECT_CROSS_BORDER)
        self._drag_rb.setFillColor(_style.SELECT_WIN_FILL if is_window
                                   else _style.SELECT_CROSS_FILL)
        self._drag_rb.setToGeometry(QgsGeometry.fromRect(QgsRectangle(start, end)))

    def _finish_drag(self, start: QgsPointXY, end: QgsPointXY, modifiers: int):
        if abs(start.x() - end.x()) < 1e-6 and abs(start.y() - end.y()) < 1e-6:
            return   # micro-drag: single click already handled in press
        is_window = end.x() >= start.x()
        rect  = QgsRectangle(
            min(start.x(), end.x()), min(start.y(), end.y()),
            max(start.x(), end.x()), max(start.y(), end.y()),
        )
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        sel   = self._ctx.selection_model
        if not shift:
            sel.clear()
        hits = []
        for layer_id, layer in self._all_geometry_layers():
            for feat in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                geom = feat.geometry()
                if is_window:
                    if rect.contains(geom.boundingBox()):
                        hits.append((layer_id, feat.id()))
                else:
                    if geom.intersects(QgsGeometry.fromRect(rect)):
                        hits.append((layer_id, feat.id()))
        if hits:
            sel.add_batch(hits)

    def _all_geometry_layers(self):
        sm = self._ctx.storage_manager
        result = []
        for attr in ("points_layer", "lines_layer"):
            lyr = getattr(sm, attr, None)
            if lyr and lyr.isValid():
                result.append((lyr.id(), lyr))
        return result

    # ── overlay visibility helpers ─────────────────────────────────────────
    def _hide_overlay(self):
        overlay = getattr(self._ctx, 'selection_overlay', None)
        if overlay is not None:
            overlay.set_editing(True)

    def _show_overlay(self):
        overlay = getattr(self._ctx, 'selection_overlay', None)
        if overlay is not None:
            overlay.set_editing(False)

    # ── programmatic cancel (tool switch) — selection intentionally kept ────
    def cancel(self):
        """Called by ToolManager on tool switch.  Cleans up drag state only —
        selection is deliberately NOT cleared so the next tool can use it."""
        if self._hot_grip is not None:
            self._hot_grip      = None
            self._drag_geom_wkt = None
            self._last_snap_pt  = None
            self._clear_rubber_bands()
            self._show_overlay()
        if self._drag_rb:
            self._drag_rb.reset()
            self._drag_rb = None
        self._drag_start = None

    # ── Esc override — SelectTool IS the home tool, never calls go_home ────
    def _handle_esc(self):
        """Esc: disarm any active grip and clear selection. Tool stays active."""
        self._esc_count = 0
        if self._hot_grip is not None:
            self._hot_grip      = None
            self._drag_geom_wkt = None
            self._clear_rubber_bands()
        self._ctx.selection_model.clear()
        self._clear_grips()
        self._show_overlay()
        if self._drag_rb:
            try:
                self.canvas().scene().removeItem(self._drag_rb)
            except Exception:
                self._drag_rb.reset()
            self._drag_rb = None
        self._drag_start = None
        self.canvas().setCursor(self._get_cursor())

    # ── cancel hook (deactivation — selection must NOT be cleared) ─────────
    def _on_cancel_hook(self):
        if self._hot_grip is not None:
            self._hot_grip      = None
            self._drag_geom_wkt = None
            self._last_snap_pt  = None
            self._drag_start    = None
            self._show_overlay()
        if self._drag_rb:
            self._drag_rb.reset()
            self._drag_rb = None
        self._drag_start = None

    def _px_to_mu(self, px: int) -> float:
        try:
            return px * self.canvas().mapUnitsPerPixel()
        except Exception:
            return px * 0.001


# ── geometry helper ────────────────────────────────────────────────────────
def _replace_vertex(geom: QgsGeometry, idx: int, new_pt: QgsPointXY) -> QgsGeometry:
    """Return a copy of geom with vertex at idx replaced by new_pt.
    Preserves single vs multi type to match the layer's geometry type."""
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
