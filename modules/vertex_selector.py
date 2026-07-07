"""
Always-on vertex grip editor (the default map tool state).

Workflow
────────
1. Hover near any vertex     → yellow hover circle appears
2. Click near an edge        → feature highlighted in green;
                               all vertices shown as green circles
3. Click a vertex            → it becomes the "grip" (blue box);
                               all other vertices of that feature shown
                               as hollow blue squares;
                               the full geometry is outlined in blue
4. Click the grip again      → enter MOVE mode
5. Move cursor               → live rubber-band shows the geometry
                               dynamically re-shaped with vertex at cursor
6. Click to place            → vertex moves; grip stays at new position
   Right-click / Escape      → cancel move, stay gripped
   Escape (gripped, not moving) → clear grip back to IDLE
   Delete / Backspace        → delete gripped vertex

State machine
─────────────
  IDLE ──[click edge]──► FEATURE ──[click vertex]──► GRIPPED ──[click same grip]──► MOVING
    │                       │                           │                               │
    └──[click vertex]───────┘                    [click elsewhere]                 [Esc / RMB]
                                                      IDLE ◄──[commit click]────────────┘
"""
from collections import namedtuple

from qgis.core import (
    QgsCircularString,
    QgsGeometry,
    QgsPoint,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QLabel
from .dynamic_input import DynamicInput
from .snap_config import snapSettingConfig


_SelVtx = namedtuple('_SelVtx', ['layer', 'fid', 'vidx', 'point'])

_S_IDLE    = 0
_S_GRIPPED = 1
_S_MOVING  = 2
_S_FEATURE = 3   # feature highlighted by edge click; no vertex gripped yet

_HIT_PX = 10

_C_HOVER       = QColor(255, 210,   0, 240)   # yellow – hover circle
_C_GRIP_HOT    = QColor(  0,  60, 220, 255)   # deep blue – hot (gripped) vertex
_C_MOVE        = QColor(255, 130,   0, 220)   # orange – live-preview in MOVING
_C_MOVE_FILL   = QColor(255, 130,   0,  30)   # faint orange fill
_C_FEATURE     = QColor(  0, 200,  80, 220)   # green – active selected feature outline
_C_FEAT_FILL   = QColor(  0, 200,  80,  20)   # faint green fill
_C_FEAT_VTX    = QColor(  0, 180,  60, 200)   # green – vertex markers on selected feature
_C_MID_MARKER  = QColor( 60, 180,  40, 220)   # medium green – segment midpoint "+" button
_C_SEL_EXTRA   = QColor(  0, 180, 220, 200)   # teal – secondary selected features
_C_SEL_EX_FILL = QColor(  0, 180, 220,  20)   # faint teal fill
_C_DRAG_BORDER = QColor(  0, 120, 255, 200)   # blue  – window (L→R) drag border
_C_DRAG_FILL   = QColor(  0, 120, 255,  25)   # faint blue fill
_C_CROSS_BORDER= QColor(  0, 200,  80, 200)   # green – crossing (R→L) drag border
_C_CROSS_FILL  = QColor(  0, 200,  80,  20)   # faint green fill

_DRAG_PX = 5   # pixels the mouse must move before a press is treated as a drag

_HINT_STYLE = (
    "QLabel {"
    "  background-color: rgba(20, 20, 20, 210);"
    "  color: #f0f0f0;"
    "  border: 1px solid rgba(255, 255, 255, 80);"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "  font-size: 9pt;"
    "}"
)

_HINT = {
    _S_FEATURE: "Click a vertex to grip",
    _S_GRIPPED: "Click grip again to move",
    _S_MOVING:  "Click to place vertex",
}


class VertexSelector(QgsMapTool):
    """Always-on vertex grip editor — the permanent default map tool."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None   # injected by UgsurvMaptool.set_default_tool()

        self._state          = _S_IDLE
        self._gripped        = None   # _SelVtx – the hot grip
        self._moving_center  = False  # True when _S_MOVING translates a whole circle

        snapSettingConfig()

        # IDLE: yellow hover circle near cursor
        self._hover_marker = self._make_marker(_C_HOVER, QgsVertexMarker.ICON_CIRCLE, 9)
        self._hover_marker.setVisible(False)

        # MOVING: cyan snap indicator
        self._snap_marker = self._make_marker(QColor(66, 135, 245), QgsVertexMarker.ICON_CIRCLE, 10)
        self._snap_marker.setVisible(False)

        # IDLE/FEATURE/MOVING: circle-center snap indicator ("+" cross)
        self._center_marker = self._make_marker(QColor(66, 135, 245), QgsVertexMarker.ICON_CROSS, 14)
        self._center_marker.setPenWidth(2)
        self._center_marker.setVisible(False)

        # GRIPPED: one marker per vertex of the gripped feature
        self._grip_markers = []

        # GRIPPED: rubber band showing the full feature geometry
        self._geom_band = None

        # MOVING: live rubber band updated every mouse move
        self._move_band        = None
        self._move_extra_bands = []   # rubber bands for shared vertices on extra features
        self._move_extra_data  = []   # [(layer, fid, vidx)] parallel to above

        # FEATURE: edge-selected feature highlight
        self._sel_layer = None
        self._sel_fid   = None
        self._feature_band = None
        self._feature_vtx_markers = []
        self._mid_markers = []   # "+" cross markers at segment midpoints
        self._mid_points  = []   # QgsPointXY for each midpoint (parallel to _mid_markers)

        # Multi-selection: secondary selected features (not the current active one)
        self._sel_extra_bands = []   # QgsRubberBand per secondary feature
        self._sel_extra_items = []   # (layer, fid) parallel to above

        # Drag-to-select state
        self._drag_start       = None   # QPoint screen pos where drag started
        self._drag_start_state = None   # _S_IDLE or _S_FEATURE at drag start
        self._is_dragging      = False
        self._drag_band        = None   # QgsRubberBand rectangle

        # Segment drag state (edge click + drag in _S_FEATURE)
        self._seg_press_pos = None   # QPoint screen pos where edge press started
        self._seg_vidx1     = None   # first vertex index of the dragged segment
        self._seg_vidx2     = None   # second vertex index
        self._seg_orig_geom = None   # geometry copy before drag starts
        self._seg_dragging  = False
        self._seg_band      = None   # orange dashed preview band

        # Vertex press-drag state (vertex click + drag in _S_FEATURE → instant move)
        self._vtx_press_pos = None   # QPoint screen pos where vertex was pressed
        self._vtx_dragging  = False  # True once drag threshold exceeded

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        # MOVING (circle only): floating radius input
        self._dinput = DynamicInput(canvas, terminal_dock, [{"key": "radius", "label": "New radius"}])
        self._dinput.on_cancel = self._cancel_move

    # ------------------------------------------------------------------
    # Marker / rubber-band factories
    # ------------------------------------------------------------------

    def _make_marker(self, color, icon, size):
        m = QgsVertexMarker(self.canvas)
        m.setColor(color)
        m.setIconType(icon)
        m.setIconSize(size)
        m.setPenWidth(2)
        return m

    def _make_band(self, geom_type, color, fill_color, width=2, dashed=False):
        band = QgsRubberBand(self.canvas, geom_type)
        band.setColor(color)
        band.setFillColor(fill_color)
        band.setWidth(width)
        if dashed:
            band.setLineStyle(Qt.DashLine)
        return band

    # ------------------------------------------------------------------
    # Circle radius input helpers (DynamicInput-backed)
    # ------------------------------------------------------------------

    def _on_radius_committed(self, values: dict):
        """Called when user presses Enter / Space in the floating input widget."""
        self.terminal_dock.clear_input_handler()
        self._apply_new_circle_radius(values["radius"])

    def _on_radius_terminal(self, text: str):
        """Called when user types a value in the terminal and presses Enter."""
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._apply_new_circle_radius(text.strip())

    def _apply_new_circle_radius(self, radius_text: str):
        """Validate, apply the new radius, and return to GRIPPED state."""
        try:
            radius = float(radius_text)
            if radius <= 0:
                raise ValueError
        except ValueError:
            self._log(f"\nInvalid radius '{radius_text}' — enter a positive number")
            return

        sv = self._gripped
        feat = sv.layer.getFeature(sv.fid)
        center = self._circle_center_from_geom(feat.geometry())
        if center is None:
            return

        new_geom = self._build_circle_geom(center, radius)
        if not sv.layer.isEditable():
            sv.layer.startEditing()
        sv.layer.changeGeometry(sv.fid, new_geom)
        radius_idx = sv.layer.fields().indexOf("radius")
        if radius_idx >= 0:
            sv.layer.changeAttributeValue(sv.fid, radius_idx, round(radius, 3))
        sv.layer.triggerRepaint()
        self._log(f"\nRadius set to {radius:.3f}")

        new_verts = self._feature_verts(sv.layer, sv.fid)
        new_pt = new_verts[sv.vidx][1] if sv.vidx < len(new_verts) else sv.point
        self._enter_gripped(_SelVtx(sv.layer, sv.fid, sv.vidx, new_pt))

    # ------------------------------------------------------------------
    # Cleanup helpers
    # ------------------------------------------------------------------

    def _rm(self, item):
        if item is not None:
            try:
                self.canvas.scene().removeItem(item)
            except Exception:
                pass

    def _clear_grip_markers(self):
        for m in self._grip_markers:
            self._rm(m)
        self._grip_markers = []

    def _clear_bands(self):
        self._rm(self._geom_band)
        self._geom_band = None
        self._rm(self._move_band)
        self._move_band = None
        for b in self._move_extra_bands:
            self._rm(b)
        self._move_extra_bands = []
        self._move_extra_data  = []

    def _clear_drag(self):
        self._rm(self._drag_band)
        self._drag_band        = None
        self._drag_start       = None
        self._drag_start_state = None
        self._is_dragging      = False

    def _end_seg_drag(self):
        self._rm(self._seg_band)
        self._seg_band      = None
        self._seg_press_pos = None
        self._seg_vidx1     = None
        self._seg_vidx2     = None
        self._seg_orig_geom = None
        self._seg_dragging  = False

    def _clear_feature(self):
        for m in self._feature_vtx_markers:
            self._rm(m)
        self._feature_vtx_markers = []
        for m in self._mid_markers:
            self._rm(m)
        self._mid_markers = []
        self._mid_points  = []
        self._rm(self._feature_band)
        self._feature_band = None
        self._sel_layer = None
        self._sel_fid   = None

    def _clear_extra_selection(self):
        for b in self._sel_extra_bands:
            self._rm(b)
        self._sel_extra_bands = []
        self._sel_extra_items = []

    def _add_to_extra_selection(self, layer, fid):
        key = (id(layer), fid)
        if any((id(l), f) == key for l, f in self._sel_extra_items):
            return
        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if geom.isEmpty():
            return
        gt = QgsWkbTypes.geometryType(geom.wkbType())
        band = self._make_band(gt, _C_SEL_EXTRA, _C_SEL_EX_FILL, width=2, dashed=True)
        band.setToGeometry(geom, layer)
        self._sel_extra_bands.append(band)
        self._sel_extra_items.append((layer, fid))

    def _remove_from_extra(self, layer, fid):
        key  = (id(layer), fid)
        keys = [(id(l), f) for l, f in self._sel_extra_items]
        if key not in keys:
            return False
        idx = keys.index(key)
        self._rm(self._sel_extra_bands.pop(idx))
        self._sel_extra_items.pop(idx)
        return True

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _enter_idle(self):
        self._state          = _S_IDLE
        self._gripped        = None
        self._moving_center  = False
        self._clear_grip_markers()
        self._clear_bands()
        self._clear_feature()
        self._clear_extra_selection()
        self._clear_drag()
        self._end_seg_drag()
        self._vtx_press_pos = None
        self._vtx_dragging  = False
        if self._hover_marker is not None:
            self._hover_marker.setVisible(False)

    def _enter_feature(self, layer, fid):
        """Highlight the feature whose edge was clicked.

        The previously active feature (if different) is demoted to the
        secondary selection set so it stays highlighted as a teal band.
        """
        # Promote the current active to secondary selection before switching
        if self._sel_layer is not None:
            if not (id(layer) == id(self._sel_layer) and fid == self._sel_fid):
                self._add_to_extra_selection(self._sel_layer, self._sel_fid)

        # If the incoming feature was already in secondary selection, promote it
        self._remove_from_extra(layer, fid)

        self._clear_grip_markers()
        self._clear_bands()
        self._clear_feature()
        if self._hover_marker is not None:
            self._hover_marker.setVisible(False)

        self._state     = _S_FEATURE
        self._sel_layer = layer
        self._sel_fid   = fid

        verts = self._feature_verts(layer, fid)
        for _, vpt in verts:
            m = self._make_marker(_C_FEAT_VTX, QgsVertexMarker.ICON_CIRCLE, 6)
            m.setCenter(vpt)
            self._feature_vtx_markers.append(m)

        # "+" markers at segment midpoints — skip for circles (chords ≠ arc)
        if layer.name() != "_circles":
            for i in range(len(verts) - 1):
                pt1, pt2 = verts[i][1], verts[i + 1][1]
                mid = QgsPointXY((pt1.x() + pt2.x()) / 2, (pt1.y() + pt2.y()) / 2)
                m = self._make_marker(_C_MID_MARKER, QgsVertexMarker.ICON_CROSS, 10)
                m.setCenter(mid)
                m.setPenWidth(2)
                self._mid_markers.append(m)
                self._mid_points.append(mid)

        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if not geom.isEmpty():
            gt = QgsWkbTypes.geometryType(geom.wkbType())
            self._feature_band = self._make_band(gt, _C_FEATURE, _C_FEAT_FILL, width=2)
            self._feature_band.setToGeometry(geom, layer)

        n_total = 1 + len(self._sel_extra_items)
        extra_msg = f"  ({n_total} selected total)" if n_total > 1 else ""
        self._log(
            f"\nFeature {fid} of '{layer.name()}' selected{extra_msg}"
            f"  →  click a vertex to grip it  |  click '+' to insert"
            f"  |  Shift+click to deselect"
        )

    def _enter_gripped(self, sv):
        """Grip the given vertex: show all feature vertices + geometry outline."""
        self._clear_grip_markers()
        self._clear_bands()
        self._clear_feature()
        if self._hover_marker is not None:
            self._hover_marker.setVisible(False)

        self._state   = _S_GRIPPED
        self._gripped = sv

        verts = self._feature_verts(sv.layer, sv.fid)
        for vidx, vpt in verts:
            hot = (vidx == sv.vidx)
            m = self._make_marker(
                _C_GRIP_HOT if hot else _C_FEAT_VTX,
                QgsVertexMarker.ICON_BOX if hot else QgsVertexMarker.ICON_CIRCLE,
                8 if hot else 6,
            )
            m.setCenter(vpt)
            self._grip_markers.append(m)

        # Full geometry outline
        feat = sv.layer.getFeature(sv.fid)
        geom = feat.geometry()
        if not geom.isEmpty():
            gt = QgsWkbTypes.geometryType(geom.wkbType())
            self._geom_band = self._make_band(gt, _C_FEATURE, _C_FEAT_FILL, width=2)
            self._geom_band.setToGeometry(geom, sv.layer)

        # Keep "+" markers at segment midpoints — skip for circles (chords ≠ arc)
        self._sel_layer = sv.layer
        self._sel_fid   = sv.fid
        if sv.layer.name() != "_circles":
            for i in range(len(verts) - 1):
                pt1, pt2 = verts[i][1], verts[i + 1][1]
                mid = QgsPointXY((pt1.x() + pt2.x()) / 2, (pt1.y() + pt2.y()) / 2)
                m = self._make_marker(_C_MID_MARKER, QgsVertexMarker.ICON_CROSS, 10)
                m.setCenter(mid)
                m.setPenWidth(2)
                self._mid_markers.append(m)
                self._mid_points.append(mid)

        self._log(
            f"\nGripped vertex {sv.vidx + 1}/{len(verts)}"
            f" of '{sv.layer.name()}' feature {sv.fid}"
            f"  →  click grip again to move, Del to delete"
        )

    def _shared_vertices(self, orig_pt, tol=1e-9):
        """Return [(layer, fid, vidx)] for vertices on extra-selected features
        that are coincident with orig_pt — these move together with the grip."""
        result = []
        for lyr, fid in self._sel_extra_items:
            feat = lyr.getFeature(fid)
            if not feat.isValid():
                continue
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            for vidx, vpt in self._geom_verts(geom):
                if abs(vpt.x() - orig_pt.x()) < tol and abs(vpt.y() - orig_pt.y()) < tol:
                    result.append((lyr, fid, vidx))
                    break   # one match per feature is enough
        return result

    def _enter_moving(self):
        """Activate live rubber-band move mode."""
        self._state = _S_MOVING
        if self._geom_band:
            self._geom_band.setVisible(False)   # hide static outline; move band takes over
        self._rm(self._move_band)               # discard any extend-preview band
        self._move_band = None
        for b in self._move_extra_bands:
            self._rm(b)
        self._move_extra_bands = []
        self._move_extra_data  = []

        sv   = self._gripped
        feat = sv.layer.getFeature(sv.fid)
        geom = feat.geometry()
        gt   = (
            QgsWkbTypes.geometryType(geom.wkbType())
            if not geom.isEmpty()
            else QgsWkbTypes.LineGeometry
        )
        self._move_band = self._make_band(gt, _C_MOVE, _C_MOVE_FILL, width=2)
        self._move_band.setToGeometry(geom, sv.layer)

        # Prepare preview bands for shared vertices on extra selected features
        for lyr, fid, vidx in self._shared_vertices(sv.point):
            xfeat = lyr.getFeature(fid)
            xgeom = xfeat.geometry()
            if not xgeom.isEmpty():
                xgt  = QgsWkbTypes.geometryType(xgeom.wkbType())
                band = self._make_band(xgt, _C_MOVE, _C_MOVE_FILL, width=2)
                band.setToGeometry(xgeom, lyr)
                self._move_extra_bands.append(band)
                self._move_extra_data.append((lyr, fid, vidx))

        if sv.layer.name() == "_circles" and not geom.isEmpty():
            center = self._circle_center_from_geom(geom)
            if center:
                current_radius = center.distance(sv.point)
                cp = self.canvas.getCoordinateTransform().transform(sv.point)
                self.terminal_dock.request_input("new radius: ", self._on_radius_terminal)
                self._dinput.on_commit = self._on_radius_committed
                self._dinput.update(cp.x(), cp.y(), {"radius": f"{current_radius:.3f}"})
                self._dinput.show(cp.x(), cp.y())
            self._log("\nDrag or type new radius + Enter  |  Esc or RMB to cancel")
        else:
            self._log("\nMove  →  move cursor to new position and click  |  Esc or RMB to cancel")

    def _commit_move(self, map_pt):
        """Move the gripped vertex to map_pt and stay gripped at new position."""
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        sv = self._gripped
        if not sv.layer.isEditable():
            sv.layer.startEditing()

        if sv.layer.name() == "_circles" and self._moving_center:
            self._moving_center = False
            self._commit_circle_center_move(sv, map_pt)
            return
        elif sv.layer.name() == "_circles":
            self._commit_circle_vertex_move(sv, map_pt)
        else:
            feat = sv.layer.getFeature(sv.fid)
            geom = feat.geometry()
            sv.layer.moveVertex(map_pt.x(), map_pt.y(), sv.fid, sv.vidx)
            if not geom.isEmpty() and self._is_closed_polyline(geom):
                verts = self._geom_verts(geom)
                first_idx, last_idx = verts[0][0], verts[-1][0]
                if sv.vidx == first_idx:
                    sv.layer.moveVertex(map_pt.x(), map_pt.y(), sv.fid, last_idx)
                elif sv.vidx == last_idx:
                    sv.layer.moveVertex(map_pt.x(), map_pt.y(), sv.fid, first_idx)
            self._update_closed_attrs(sv)

        sv.layer.triggerRepaint()

        # Move coincident vertices on all other selected features
        for lyr, fid, vidx in self._move_extra_data:
            if not lyr.isEditable():
                lyr.startEditing()
            lyr.moveVertex(map_pt.x(), map_pt.y(), fid, vidx)
            # Sync closed-polyline duplicate endpoint if needed
            extra_feat = lyr.getFeature(fid)
            extra_geom = extra_feat.geometry()
            if not extra_geom.isEmpty() and self._is_closed_polyline(extra_geom):
                xverts = self._geom_verts(extra_geom)
                first_i, last_i = xverts[0][0], xverts[-1][0]
                if vidx == first_i:
                    lyr.moveVertex(map_pt.x(), map_pt.y(), fid, last_i)
                elif vidx == last_i:
                    lyr.moveVertex(map_pt.x(), map_pt.y(), fid, first_i)
            lyr.triggerRepaint()

        self._log(
            f"\nMoved  ({sv.point.x():.3f}, {sv.point.y():.3f})"
            f" → ({map_pt.x():.3f}, {map_pt.y():.3f})"
            + (f"  [{len(self._move_extra_data)} shared]" if self._move_extra_data else "")
        )
        new_sv = _SelVtx(sv.layer, sv.fid, sv.vidx, map_pt)
        self._enter_gripped(new_sv)

    def _circle_center_from_geom(self, geom):
        """Return the center QgsPointXY of a circle geometry."""
        if geom is None or geom.isNull() or geom.isEmpty():
            return None
        bbox = geom.boundingBox()
        if bbox.isEmpty():
            return None
        c = bbox.center()
        return QgsPointXY(c.x(), c.y())

    def _build_circle_geom(self, center, radius):
        """Rebuild a 5-point closed QgsCircularString (polyline) from center + radius."""
        cx, cy = center.x(), center.y()
        arc_pts = [
            QgsPoint(cx + radius, cy),
            QgsPoint(cx,          cy - radius),
            QgsPoint(cx - radius, cy),
            QgsPoint(cx,          cy + radius),
            QgsPoint(cx + radius, cy),
        ]
        cs = QgsCircularString()
        cs.setPoints(arc_pts)
        return QgsGeometry(cs)

    def _circle_geom_for_drag(self, geom, drag_pt):
        """Return a circle QgsGeometry resized so the dragged point lies on the circumference."""
        center = self._circle_center_from_geom(geom)
        if center is None:
            return QgsGeometry(geom)
        new_radius = center.distance(drag_pt)
        if new_radius < 1e-9:
            return QgsGeometry(geom)
        return self._build_circle_geom(center, new_radius)

    def _commit_circle_vertex_move(self, sv, map_pt):
        """Resize the circle so map_pt lies on its circumference, keeping center fixed."""
        feat = sv.layer.getFeature(sv.fid)
        geom = feat.geometry()
        center = self._circle_center_from_geom(geom)
        if center is None:
            sv.layer.moveVertex(map_pt.x(), map_pt.y(), sv.fid, sv.vidx)
            return
        new_radius = center.distance(map_pt)
        if new_radius < 1e-9:
            return
        new_geom = self._build_circle_geom(center, new_radius)
        sv.layer.changeGeometry(sv.fid, new_geom)
        radius_idx = sv.layer.fields().indexOf("radius")
        if radius_idx >= 0:
            sv.layer.changeAttributeValue(sv.fid, radius_idx, round(new_radius, 3))

    def _find_circle_center_feature(self, map_pt):
        """Return (layer, fid, center_pt) of the nearest _circles center within hit tolerance, or None."""
        tol  = self._hit_tol()
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        best_lyr, best_fid, best_center, best_dist = None, None, None, tol
        for lyr in self._vector_layers():
            if lyr.name() != "_circles":
                continue
            for feat in lyr.getFeatures(rect):
                center = self._circle_center_from_geom(feat.geometry())
                if center is None:
                    continue
                dist = map_pt.distance(center)
                if dist < best_dist:
                    best_dist   = dist
                    best_lyr    = lyr
                    best_fid    = feat.id()
                    best_center = center
        if best_lyr is None:
            return None
        return (best_lyr, best_fid, best_center)

    def _enter_moving_center(self, layer, fid):
        """Enter whole-circle translate mode: the center tracks the cursor."""
        self._clear_grip_markers()
        self._clear_bands()
        self._clear_feature()
        if self._hover_marker is not None:
            self._hover_marker.setVisible(False)

        verts = self._feature_verts(layer, fid)
        if not verts:
            return
        # Use vertex 0 (East point) as sentinel grip so _commit_move can read the radius
        sv = _SelVtx(layer, fid, verts[0][0], verts[0][1])
        self._gripped       = sv
        self._sel_layer     = layer
        self._sel_fid       = fid
        self._moving_center = True
        self._state         = _S_MOVING

        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if not geom.isEmpty():
            gt = QgsWkbTypes.geometryType(geom.wkbType())
            self._move_band = self._make_band(gt, _C_MOVE, _C_MOVE_FILL, width=2)
            self._move_band.setToGeometry(geom, layer)

        self._log("\nMoving circle — click to place new center  |  Esc / RMB to cancel")

    def _commit_circle_center_move(self, sv, new_center):
        """Translate the circle so its center is at new_center, preserving radius."""
        feat = sv.layer.getFeature(sv.fid)
        geom = feat.geometry()
        orig_center = self._circle_center_from_geom(geom)
        if orig_center is None:
            return
        radius = orig_center.distance(sv.point)   # sv.point = original East vertex
        new_geom = self._build_circle_geom(new_center, radius)
        sv.layer.changeGeometry(sv.fid, new_geom)
        radius_idx = sv.layer.fields().indexOf("radius")
        if radius_idx >= 0:
            sv.layer.changeAttributeValue(sv.fid, radius_idx, round(radius, 3))
        sv.layer.triggerRepaint()
        self._log(
            f"\nCircle moved  center ({orig_center.x():.3f}, {orig_center.y():.3f})"
            f" → ({new_center.x():.3f}, {new_center.y():.3f})"
        )
        new_east = QgsPointXY(new_center.x() + radius, new_center.y())
        self._enter_gripped(_SelVtx(sv.layer, sv.fid, sv.vidx, new_east))

    def _cancel_move(self):
        self._moving_center = False
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._snap_marker.setVisible(False)
        self._rm(self._move_band)
        self._move_band = None
        for b in self._move_extra_bands:
            self._rm(b)
        self._move_extra_bands = []
        self._move_extra_data  = []
        if self._geom_band:
            self._geom_band.setVisible(True)
        self._state = _S_GRIPPED
        self._log("\nMove cancelled")

    def _delete_gripped(self):
        sv = self._gripped

        # Find shared vertices on extra-selected features before modifying anything
        shared = self._shared_vertices(sv.point)

        if not sv.layer.isEditable():
            sv.layer.startEditing()
        feat = sv.layer.getFeature(sv.fid)
        geom = QgsGeometry(feat.geometry())
        geom.deleteVertex(sv.vidx)
        sv.layer.changeGeometry(sv.fid, geom)
        sv.layer.triggerRepaint()
        self._update_closed_attrs(sv)

        # Delete coincident vertex on each extra-selected feature
        for lyr, fid, vidx in shared:
            if not lyr.isEditable():
                lyr.startEditing()
            xfeat = lyr.getFeature(fid)
            xgeom = QgsGeometry(xfeat.geometry())
            xgeom.deleteVertex(vidx)
            lyr.changeGeometry(fid, xgeom)
            lyr.triggerRepaint()
            self._update_closed_attrs(_SelVtx(lyr, fid, vidx, sv.point))

        n_shared = len(shared)
        self._log(
            f"\nDeleted vertex {sv.vidx + 1} of '{sv.layer.name()}'"
            + (f"  [{n_shared} shared]" if n_shared else "")
        )

        # Stay on the feature so the user can keep editing without losing the selection
        updated = sv.layer.getFeature(sv.fid)
        if updated.isValid() and not updated.geometry().isEmpty():
            self._enter_feature(sv.layer, sv.fid)
        else:
            self._enter_idle()

    def _delete_feature(self):
        all_sel = self.get_selected_features()
        if not all_sel:
            return
        modified = set()
        for lyr, fid in all_sel:
            if not lyr.isEditable():
                lyr.startEditing()
            lyr.deleteFeature(fid)
            modified.add(lyr)
        for lyr in modified:
            lyr.triggerRepaint()
        n = len(all_sel)
        if n == 1:
            lyr, fid = all_sel[0]
            self._log(f"\nDeleted feature {fid} of '{lyr.name()}'")
        else:
            self._log(f"\nDeleted {n} features")
        self._enter_idle()

    def _insert_vertex_on_segment(self, map_pt):
        """Insert a new vertex at map_pt, then immediately enter move mode on it."""
        lyr, fid = self._sel_layer, self._sel_fid
        feat = lyr.getFeature(fid)
        geom = feat.geometry()
        if geom.isEmpty():
            return
        if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
            return
        _, _, vertex_after, _ = geom.closestSegmentWithContext(map_pt)
        verts = self._geom_verts(geom)
        pts = [vpt for _, vpt in verts]
        pts.insert(vertex_after, map_pt)
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.changeGeometry(fid, QgsGeometry.fromPolylineXY(pts))
        lyr.triggerRepaint()
        self._log(f"\nInserted vertex — drag or click to place  |  Esc / RMB to cancel")
        # Grip the new vertex and immediately start moving it
        new_sv = _SelVtx(lyr, fid, vertex_after, map_pt)
        self._enter_gripped(new_sv)
        self._enter_moving()

    def _is_opposite_endpoint(self, sv):
        """True if sv is the opposite endpoint of the same feature as the gripped vertex."""
        g = self._gripped
        if g is None or sv is None:
            return False
        if id(sv.layer) != id(g.layer) or sv.fid != g.fid:
            return False
        verts = self._feature_verts(g.layer, g.fid)
        if len(verts) < 2:
            return False
        first_idx, last_idx = verts[0][0], verts[-1][0]
        return (g.vidx == first_idx and sv.vidx == last_idx) or \
               (g.vidx == last_idx and sv.vidx == first_idx)

    def _gripped_is_endpoint(self):
        """True when the gripped vertex is the first or last vertex of a polyline."""
        sv = self._gripped
        if sv is None:
            return False
        feat = sv.layer.getFeature(sv.fid)
        geom = feat.geometry()
        if geom.isEmpty():
            return False
        if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
            return False
        verts = self._geom_verts(geom)
        if len(verts) < 2:
            return False
        return sv.vidx == verts[0][0] or sv.vidx == verts[-1][0]

    def _gripped_can_extend(self):
        """True only when the gripped endpoint belongs to an open (non-closed) polyline."""
        if not self._gripped_is_endpoint():
            return False
        sv = self._gripped
        feat = sv.layer.getFeature(sv.fid)
        return not self._is_closed_polyline(feat.geometry())

    def _extend_line(self, map_pt):
        """Append a new vertex at map_pt from the gripped endpoint, then stay gripped there."""
        sv = self._gripped
        feat = sv.layer.getFeature(sv.fid)
        geom = feat.geometry()
        verts = self._geom_verts(geom)
        pts = [vpt for _, vpt in verts]
        if sv.vidx == verts[0][0]:
            pts.insert(0, map_pt)
            new_vidx = 0
        else:
            pts.append(map_pt)
            new_vidx = len(pts) - 1
        if not sv.layer.isEditable():
            sv.layer.startEditing()
        sv.layer.changeGeometry(sv.fid, QgsGeometry.fromPolylineXY(pts))
        sv.layer.triggerRepaint()
        self._log(f"\nExtended line to ({map_pt.x():.3f}, {map_pt.y():.3f})")
        new_sv = _SelVtx(sv.layer, sv.fid, new_vidx, map_pt)
        self._update_closed_attrs(new_sv)
        self._enter_gripped(new_sv)

    # ------------------------------------------------------------------
    # Snap helper
    # ------------------------------------------------------------------

    def _find_circle_center_snap(self, map_pt):
        """Return the center of the nearest _circles feature if cursor is within 20 px of it."""
        center_tol = 20 * self.canvas.mapUnitsPerPixel()
        best_center, best_dist = None, center_tol
        for lyr in self._vector_layers():
            if lyr.name() != "_circles":
                continue
            for feat in lyr.getFeatures():   # no spatial filter — avoids edit-buffer index gaps
                geom = feat.geometry()
                if geom.isNull() or geom.isEmpty():
                    continue
                center = self._circle_center_from_geom(geom)
                if center is None:
                    continue
                dist = map_pt.distance(center)
                if dist < best_dist:
                    best_dist = dist
                    best_center = center
        return best_center

    def _update_center_marker(self, map_pt):
        """Show/hide the circle-center snap indicator based on cursor proximity."""
        if self._state == _S_MOVING or self._center_marker is None:
            if self._center_marker:
                self._center_marker.setVisible(False)
            return
        center = self._find_circle_center_snap(map_pt)
        if center:
            self._center_marker.setCenter(center)
            self._center_marker.setVisible(True)
        else:
            self._center_marker.setVisible(False)

    def _snap_point(self, screen_pt, raw_pt):
        """Return snapped point using QGIS native snapping, or raw_pt if no snap.

        Circle centers are checked first and take priority over normal snap.
        """
        center = self._find_circle_center_snap(raw_pt)
        if center and self._snap_marker:
            self._snap_marker.setCenter(center)
            self._snap_marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self._snap_marker.setVisible(True)
            return center

        match = self.canvas.snappingUtils().snapToMap(screen_pt)
        if match.isValid():
            snapped = match.point()
            self._snap_marker.setCenter(snapped)
            icon_map = {
                QgsPointLocator.Vertex:          QgsVertexMarker.ICON_CIRCLE,
                QgsPointLocator.Edge:            QgsVertexMarker.ICON_DOUBLE_TRIANGLE,
                QgsPointLocator.Area:            QgsVertexMarker.ICON_RHOMBUS,
                QgsPointLocator.MiddleOfSegment: QgsVertexMarker.ICON_TRIANGLE,
            }
            self._snap_marker.setIconType(icon_map.get(match.type(), QgsVertexMarker.ICON_X))
            self._snap_marker.setVisible(True)
            return snapped
        self._snap_marker.setVisible(False)
        return raw_pt

    # ------------------------------------------------------------------
    # Vertex / feature search
    # ------------------------------------------------------------------

    def _hit_tol(self):
        return _HIT_PX * self.canvas.mapUnitsPerPixel()

    def _vector_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial()
        ]

    def _geom_verts(self, geom):
        result, idx = [], 0
        it = geom.vertices()
        while it.hasNext():
            pt = it.next()
            result.append((idx, QgsPointXY(pt.x(), pt.y())))
            idx += 1
        return result

    def _feature_verts(self, layer, fid):
        feat = layer.getFeature(fid)
        if not feat.isValid() or feat.geometry().isEmpty():
            return []
        return self._geom_verts(feat.geometry())

    def _is_closed_polyline(self, geom):
        """True if geom is a linestring whose first and last vertices coincide."""
        if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
            return False
        verts = self._geom_verts(geom)
        if len(verts) < 4:
            return False
        return (abs(verts[0][1].x() - verts[-1][1].x()) < 1e-9 and
                abs(verts[0][1].y() - verts[-1][1].y()) < 1e-9)

    def _update_closed_attrs(self, sv):
        """Recompute closed / area attributes after any geometry change on a polylines feature."""
        if sv.layer.name() != "polylines":
            return
        feat = sv.layer.getFeature(sv.fid)
        geom = feat.geometry()
        if geom.isEmpty():
            return
        is_closed = self._is_closed_polyline(geom)
        closed_idx = sv.layer.fields().indexOf("closed")
        if closed_idx >= 0:
            sv.layer.changeAttributeValue(sv.fid, closed_idx, is_closed)
        area_sqm_idx   = sv.layer.fields().indexOf("area_sqm")
        area_acres_idx = sv.layer.fields().indexOf("area_acres")
        if is_closed:
            pts = [vpt for _, vpt in self._geom_verts(geom)]
            poly_geom  = QgsGeometry.fromPolygonXY([pts])
            area_sqm   = poly_geom.area()
            area_acres = area_sqm * 0.000247105
            if area_sqm_idx >= 0:
                sv.layer.changeAttributeValue(sv.fid, area_sqm_idx, round(area_sqm, 3))
            if area_acres_idx >= 0:
                sv.layer.changeAttributeValue(sv.fid, area_acres_idx, round(area_acres, 6))
            self._log(f"\nArea: {area_sqm:.3f} sqm  ({area_acres:.4f} acres)")
        else:
            if area_sqm_idx >= 0:
                sv.layer.changeAttributeValue(sv.fid, area_sqm_idx, 0.0)
            if area_acres_idx >= 0:
                sv.layer.changeAttributeValue(sv.fid, area_acres_idx, 0.0)

    def _find_vertex_near(self, map_pt, prefer=None):
        """Return the nearest _SelVtx within hit tolerance.

        prefer=(layer, fid): if a vertex from that feature is within tolerance
        it is returned unconditionally, even when a vertex from another feature
        is geometrically closer (handles shared/coincident boundary vertices).
        """
        tol  = self._hit_tol()
        best, best_d = None, tol
        preferred, preferred_d = None, tol
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        for lyr in self._vector_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                is_pref = (prefer is not None
                           and id(lyr) == id(prefer[0])
                           and feat.id() == prefer[1])
                for vidx, vpt in self._geom_verts(feat.geometry()):
                    d = map_pt.distance(vpt)
                    if is_pref:
                        if d < preferred_d:
                            preferred_d = d
                            preferred = _SelVtx(lyr, feat.id(), vidx, vpt)
                    else:
                        if d < best_d:
                            best_d = d
                            best = _SelVtx(lyr, feat.id(), vidx, vpt)
        return preferred if preferred is not None else best

    def _find_edge_near(self, map_pt):
        """Return (layer, fid) of the nearest feature whose edge is within tol of map_pt.
        Only called when no vertex hit was found, so vertex proximity is not re-checked.
        """
        tol = self._hit_tol()
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        best, best_d = None, tol
        for lyr in self._vector_layers():
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                gt = QgsWkbTypes.geometryType(geom.wkbType())
                if gt == QgsWkbTypes.PointGeometry:
                    continue
                d = geom.distance(pt_geom)
                if d < best_d:
                    best_d = d
                    best = (lyr, feat.id())
        return best

    def _find_midpoint_near(self, map_pt):
        """Return the pre-calculated midpoint QgsPointXY if map_pt is within hit tolerance."""
        tol = self._hit_tol()
        for mpt in self._mid_points:
            if map_pt.distance(mpt) <= tol:
                return mpt
        return None

    def _same_grip(self, sv):
        """True if sv refers to the same layer/feature/vertex as the current grip."""
        g = self._gripped
        return (
            sv is not None and g is not None
            and id(sv.layer) == id(g.layer)
            and sv.fid == g.fid
            and sv.vidx == g.vidx
        )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Segment drag helpers
    # ------------------------------------------------------------------

    def _find_nearest_segment(self, map_pt, geom):
        """Return (vidx1, vidx2) of the segment in geom nearest to map_pt."""
        try:
            _, _, after_vertex, _ = geom.closestSegmentWithContext(
                QgsPoint(map_pt.x(), map_pt.y())
            )
        except Exception:
            return None
        if after_vertex is None or after_vertex <= 0:
            return None
        return (after_vertex - 1, after_vertex)

    def _update_seg_band(self, dx, dy):
        orig = self._seg_orig_geom
        verts = self._geom_verts(orig)
        if not verts:
            return
        preview = QgsGeometry(orig)
        n = len(verts)
        v1 = verts[self._seg_vidx1][1]
        v2 = verts[self._seg_vidx2][1]
        preview.moveVertex(v1.x() + dx, v1.y() + dy, self._seg_vidx1)
        preview.moveVertex(v2.x() + dx, v2.y() + dy, self._seg_vidx2)
        # Sync duplicate first/last vertex of closed polylines
        if self._is_closed_polyline(orig):
            if self._seg_vidx1 == 0:
                preview.moveVertex(v1.x() + dx, v1.y() + dy, n - 1)
            elif self._seg_vidx1 == n - 1:
                preview.moveVertex(v1.x() + dx, v1.y() + dy, 0)
            if self._seg_vidx2 == 0:
                preview.moveVertex(v2.x() + dx, v2.y() + dy, n - 1)
            elif self._seg_vidx2 == n - 1:
                preview.moveVertex(v2.x() + dx, v2.y() + dy, 0)
        if self._seg_band is None:
            gt = QgsWkbTypes.geometryType(preview.wkbType())
            self._seg_band = self._make_band(gt, _C_MOVE, _C_MOVE_FILL, width=2, dashed=True)
        self._seg_band.setToGeometry(preview, self._sel_layer)

    def _commit_seg_drag(self, vidx1, vidx2, dx, dy):
        lyr, fid = self._sel_layer, self._sel_fid
        feat  = lyr.getFeature(fid)
        geom  = feat.geometry()
        verts = self._geom_verts(geom)
        if not verts:
            return
        v1 = verts[vidx1][1]
        v2 = verts[vidx2][1]
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.moveVertex(v1.x() + dx, v1.y() + dy, fid, vidx1)
        lyr.moveVertex(v2.x() + dx, v2.y() + dy, fid, vidx2)
        # Sync duplicate first/last vertex of closed polylines
        n = len(verts)
        updated = lyr.getFeature(fid).geometry()
        if self._is_closed_polyline(updated):
            if vidx1 == 0:
                lyr.moveVertex(v1.x() + dx, v1.y() + dy, fid, n - 1)
            elif vidx1 == n - 1:
                lyr.moveVertex(v1.x() + dx, v1.y() + dy, fid, 0)
            if vidx2 == 0:
                lyr.moveVertex(v2.x() + dx, v2.y() + dy, fid, n - 1)
            elif vidx2 == n - 1:
                lyr.moveVertex(v2.x() + dx, v2.y() + dy, fid, 0)
        lyr.triggerRepaint()
        self._log(f"\nMoved segment  Δ({dx:.3f}, {dy:.3f})")
        sv_dummy = _SelVtx(lyr, fid, vidx1, QgsPointXY(v1.x() + dx, v1.y() + dy))
        self._update_closed_attrs(sv_dummy)
        self._enter_feature(lyr, fid)

    # ------------------------------------------------------------------
    # Drag-to-select helpers
    # ------------------------------------------------------------------

    def _update_drag_band(self, screen_pos):
        p1 = self.toMapCoordinates(self._drag_start)
        p2 = self.toMapCoordinates(screen_pos)
        crossing = screen_pos.x() < self._drag_start.x()   # R→L = crossing
        border = _C_CROSS_BORDER if crossing else _C_DRAG_BORDER
        fill   = _C_CROSS_FILL   if crossing else _C_DRAG_FILL
        if self._drag_band is None:
            self._drag_band = self._make_band(QgsWkbTypes.PolygonGeometry, border, fill)
        self._drag_band.setColor(border)
        self._drag_band.setFillColor(fill)
        self._drag_band.setLineStyle(Qt.DashLine if crossing else Qt.SolidLine)
        rect = QgsRectangle(p1.x(), p1.y(), p2.x(), p2.y())
        self._drag_band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def _select_in_rect(self, rect, crossing):
        """Select features. crossing=True keeps features that merely intersect;
        crossing=False (window/L→R) keeps only features fully inside the rect."""
        rect_geom = QgsGeometry.fromRect(rect)
        found = []
        for lyr in self._vector_layers():
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                if crossing:
                    if geom.intersects(rect_geom):
                        found.append((lyr, feat.id()))
                else:
                    if rect_geom.contains(geom):
                        found.append((lyr, feat.id()))
        if not found:
            self._log("\nNo features in selection rectangle")
            return
        for lyr, fid in found:
            self._enter_feature(lyr, fid)
        n = len(self.get_selected_features())
        mode = "crossing" if crossing else "window"
        self._log(f"\n{n} feature(s) selected ({mode})")

    def _grip_vertices_in_rect(self, rect):
        """When a feature is active, grip the vertex of that feature inside rect."""
        lyr = self._sel_layer or (self._gripped.layer if self._gripped else None)
        fid = self._sel_fid   or (self._gripped.fid   if self._gripped else None)
        if lyr is None:
            return
        verts = self._feature_verts(lyr, fid)
        inside = [_SelVtx(lyr, fid, vidx, vpt)
                  for vidx, vpt in verts if rect.contains(vpt)]
        if not inside:
            self._log("\nNo vertices in selection rectangle")
            return
        cx = (rect.xMinimum() + rect.xMaximum()) / 2
        cy = (rect.yMinimum() + rect.yMaximum()) / 2
        center = QgsPointXY(cx, cy)
        sv = min(inside, key=lambda s: s.point.distance(center))
        self._enter_gripped(sv)
        self._log(f"\nVertex {sv.vidx + 1} selected via rectangle")

    # ------------------------------------------------------------------

    def get_selected_feature(self):
        """Return (layer, fid) if a feature is currently highlighted or gripped, else None."""
        if self._state == _S_FEATURE:
            return self._sel_layer, self._sel_fid
        if self._state in (_S_GRIPPED, _S_MOVING):
            return self._gripped.layer, self._gripped.fid
        return None

    def get_selected_features(self):
        """Return all selected features as a list of (layer, fid) — active first, then extras."""
        result = []
        active = self.get_selected_feature()
        if active and active[0] is not None:
            result.append(active)
        result.extend(self._sel_extra_items)
        return result

    def _show_hint(self, screen_pos):
        if self._state == _S_MOVING and self._moving_center:
            text = "Click to place circle center"
        elif self._state == _S_GRIPPED and self._gripped_can_extend():
            text = "Click grip to move  |  click to extend line"
        else:
            text = _HINT.get(self._state, "")
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self.canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self.canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    def _log(self, msg):
        self.terminal_dock.commandOutputText += msg
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()
        # Recreate markers if they were removed when a drawing tool took over
        if self._hover_marker is None:
            self._hover_marker = self._make_marker(_C_HOVER, QgsVertexMarker.ICON_CIRCLE, 9)
            self._hover_marker.setVisible(False)
        if self._snap_marker is None:
            self._snap_marker = self._make_marker(QColor(66, 135, 245), QgsVertexMarker.ICON_CIRCLE, 10)
            self._snap_marker.setVisible(False)
        if self._center_marker is None:
            self._center_marker = self._make_marker(QColor(66, 135, 245), QgsVertexMarker.ICON_CROSS, 14)
            self._center_marker.setPenWidth(2)
            self._center_marker.setVisible(False)

    def deactivate(self):
        """Called by UgsurvMaptool._evict() when a drawing tool takes over.
        Clean up all visual elements. Do NOT call _maptool.clear_tool() —
        this tool IS the default; the maptool handles its own reversion.
        """
        self._enter_idle()
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._hint.hide()
        self._rm(self._hover_marker)
        self._hover_marker = None   # recreated in activate() when we come back
        self._snap_marker.setVisible(False)
        self._rm(self._snap_marker)
        self._snap_marker = None
        if self._center_marker is not None:
            self._center_marker.setVisible(False)
            self._rm(self._center_marker)
            self._center_marker = None
        super().deactivate()

    def canvasMoveEvent(self, event):
        raw_pt = self.toMapCoordinates(event.pos())
        self._update_center_marker(raw_pt)

        # Segment drag: left button held + started from edge click
        if self._seg_press_pos is not None and (event.buttons() & Qt.LeftButton):
            delta = event.pos() - self._seg_press_pos
            if not self._seg_dragging and (abs(delta.x()) > _DRAG_PX or abs(delta.y()) > _DRAG_PX):
                self._seg_dragging = True
            if self._seg_dragging:
                start_map = self.toMapCoordinates(self._seg_press_pos)
                cur_map   = self.toMapCoordinates(event.pos())
                self._update_seg_band(cur_map.x() - start_map.x(), cur_map.y() - start_map.y())
                if self._hover_marker is not None:
                    self._hover_marker.setVisible(False)
                return

        # Drag-to-select: left button held + started from empty space
        if self._drag_start is not None and (event.buttons() & Qt.LeftButton):
            delta = event.pos() - self._drag_start
            if not self._is_dragging and (abs(delta.x()) > _DRAG_PX or abs(delta.y()) > _DRAG_PX):
                self._is_dragging = True
            if self._is_dragging:
                self._update_drag_band(event.pos())
                if self._hover_marker is not None:
                    self._hover_marker.setVisible(False)
                return

        # Vertex press-drag: left button held after clicking a vertex in _S_FEATURE
        if self._vtx_press_pos is not None and (event.buttons() & Qt.LeftButton):
            delta = event.pos() - self._vtx_press_pos
            if not self._vtx_dragging and (abs(delta.x()) > _DRAG_PX or abs(delta.y()) > _DRAG_PX):
                self._vtx_dragging = True
                # Silently enter moving mode (no dialog, no log noise)
                self._state = _S_MOVING
                if self._geom_band:
                    self._geom_band.setVisible(False)
                self._rm(self._move_band)
                self._move_band = None
                sv   = self._gripped
                feat = sv.layer.getFeature(sv.fid)
                geom = feat.geometry()
                gt   = (QgsWkbTypes.geometryType(geom.wkbType())
                        if not geom.isEmpty() else QgsWkbTypes.LineGeometry)
                self._move_band = self._make_band(gt, _C_MOVE, _C_MOVE_FILL, width=2)
                self._move_band.setToGeometry(geom, sv.layer)
                if self._hover_marker is not None:
                    self._hover_marker.setVisible(False)
            if not self._vtx_dragging:
                return  # within threshold — wait
            # _vtx_dragging is True → fall through to _S_MOVING handler below

        if self._state == _S_MOVING:
            map_pt = self._snap_point(event.pos(), raw_pt)
            # Update live rubber-band: replace gripped vertex with cursor
            sv   = self._gripped
            feat = sv.layer.getFeature(sv.fid)
            if not feat.geometry().isEmpty():
                if sv.layer.name() == "_circles" and self._moving_center:
                    # Translate: rebuild circle at cursor position, same radius
                    orig_center = self._circle_center_from_geom(feat.geometry())
                    if orig_center:
                        radius  = orig_center.distance(sv.point)
                        preview = self._build_circle_geom(map_pt, radius)
                        self._move_band.setToGeometry(preview, sv.layer)
                    self._show_hint(event.pos())
                    return
                elif sv.layer.name() == "_circles":
                    preview = self._circle_geom_for_drag(feat.geometry(), map_pt)
                    # Keep input box near cursor; update placeholder with live radius
                    center = self._circle_center_from_geom(feat.geometry())
                    if center:
                        cp = self.canvas.getCoordinateTransform().transform(map_pt)
                        self._dinput.update(cp.x(), cp.y(), {"radius": f"{center.distance(map_pt):.3f}"})
                else:
                    geom = feat.geometry()
                    preview = QgsGeometry(geom)
                    preview.moveVertex(map_pt.x(), map_pt.y(), sv.vidx)
                    if self._is_closed_polyline(geom):
                        verts = self._geom_verts(geom)
                        first_idx, last_idx = verts[0][0], verts[-1][0]
                        if sv.vidx == first_idx:
                            preview.moveVertex(map_pt.x(), map_pt.y(), last_idx)
                        elif sv.vidx == last_idx:
                            preview.moveVertex(map_pt.x(), map_pt.y(), first_idx)
                self._move_band.setToGeometry(preview, sv.layer)
            # Update preview for shared vertices on extra selected features
            for i, (xlyr, xfid, xvidx) in enumerate(self._move_extra_data):
                xfeat = xlyr.getFeature(xfid)
                xgeom = xfeat.geometry()
                if not xgeom.isEmpty() and i < len(self._move_extra_bands):
                    xprev = QgsGeometry(xgeom)
                    xprev.moveVertex(map_pt.x(), map_pt.y(), xvidx)
                    if self._is_closed_polyline(xgeom):
                        xverts = self._geom_verts(xgeom)
                        xi0, xi1 = xverts[0][0], xverts[-1][0]
                        if xvidx == xi0:
                            xprev.moveVertex(map_pt.x(), map_pt.y(), xi1)
                        elif xvidx == xi1:
                            xprev.moveVertex(map_pt.x(), map_pt.y(), xi0)
                    self._move_extra_bands[i].setToGeometry(xprev, xlyr)
            self._show_hint(event.pos())
            return

        self._snap_marker.setVisible(False)
        map_pt = raw_pt

        if self._state == _S_GRIPPED:
            self._show_hint(event.pos())
            if self._gripped_can_extend():
                map_pt = self._snap_point(event.pos(), raw_pt)
                sv = self._gripped
                if self._move_band is None:
                    self._move_band = self._make_band(
                        QgsWkbTypes.LineGeometry, _C_MOVE, _C_MOVE_FILL, width=2, dashed=True
                    )
                self._move_band.reset(QgsWkbTypes.LineGeometry)
                self._move_band.addPoint(sv.point)
                self._move_band.addPoint(map_pt)
            else:
                self._snap_marker.setVisible(False)
                if self._move_band is not None:
                    self._move_band.reset(QgsWkbTypes.LineGeometry)
            return

        # IDLE or FEATURE — show yellow hover circle on the nearest vertex
        sv = self._find_vertex_near(map_pt)
        if self._hover_marker is None:
            return
        if sv:
            self._hover_marker.setCenter(sv.point)
            self._hover_marker.setVisible(True)
        else:
            self._hover_marker.setVisible(False)
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        raw_pt = self.toMapCoordinates(event.pos())

        # Right-click: cancel move (if moving) or clear grip / feature selection
        if event.button() == Qt.RightButton:
            if self._state == _S_MOVING:
                self._cancel_move()
            elif self._state in (_S_GRIPPED, _S_FEATURE):
                self._enter_idle()
            return

        if event.button() != Qt.LeftButton:
            return

        if self._state == _S_MOVING:
            commit_pt = self._snap_point(event.pos(), raw_pt)
            self._snap_marker.setVisible(False)
            self._commit_move(commit_pt)
            self.terminal_dock.command.setFocus()
            return

        map_pt = raw_pt

        # Circle-center click: enter whole-circle translate mode (optional — only
        # when cursor is near the center cross, not a cardinal vertex).
        if not (event.modifiers() & Qt.ShiftModifier):
            center_hit = self._find_circle_center_feature(map_pt)
            if center_hit:
                self._enter_moving_center(center_hit[0], center_hit[1])
                return

        # Prefer the active feature's vertices to avoid accidentally grabbing
        # a neighbour feature that shares a coincident boundary vertex.
        if self._state == _S_GRIPPED:
            sv = self._find_vertex_near(map_pt, prefer=(self._gripped.layer, self._gripped.fid))
        elif self._state == _S_FEATURE:
            sv = self._find_vertex_near(map_pt, prefer=(self._sel_layer, self._sel_fid))
        else:
            sv = self._find_vertex_near(map_pt)

        if self._state == _S_GRIPPED:
            if sv and self._gripped_can_extend() and self._is_opposite_endpoint(sv):
                # Clicked the opposite endpoint of the same feature → close the line
                if self._move_band is not None:
                    self._move_band.reset(QgsWkbTypes.LineGeometry)
                self._snap_marker.setVisible(False)
                self._extend_line(sv.point)
            elif sv:
                # Any vertex clicked (same or different feature) → grip it and move immediately
                self._enter_gripped(sv)
                self._enter_moving()
                self._vtx_press_pos = event.pos()
            else:
                mpt = self._find_midpoint_near(map_pt)
                if mpt is not None:
                    self._insert_vertex_on_segment(mpt)
                elif self._gripped_can_extend():
                    # Gripped on endpoint, clicked empty space → extend the line
                    commit_pt = self._snap_point(event.pos(), raw_pt)
                    self._snap_marker.setVisible(False)
                    if self._move_band is not None:
                        self._move_band.reset(QgsWkbTypes.LineGeometry)
                    self._extend_line(commit_pt)
                else:
                    # Empty space — start drag so user can box-select a vertex
                    self._drag_start       = event.pos()
                    self._drag_start_state = _S_GRIPPED
            return

        if self._state == _S_FEATURE:
            shift = bool(event.modifiers() & Qt.ShiftModifier)

            # 1. Vertex of active feature — highest priority.
            #    Checked before '+' so stacked/close vertices are always grippable.
            if sv and id(sv.layer) == id(self._sel_layer) and sv.fid == self._sel_fid:
                if shift:
                    self._enter_idle()
                else:
                    self._enter_gripped(sv)
                    self._enter_moving()               # skip grip step — go straight to move
                    self._vtx_press_pos = event.pos()  # also support press-drag
                return

            # 2. '+' midpoint marker — only reachable when no active-feature vertex was hit.
            mpt = self._find_midpoint_near(map_pt)
            if mpt is not None:
                self._insert_vertex_on_segment(mpt)
                return

            # 3. Edge of active feature, another feature, or empty space.
            sel_feat = self._sel_layer.getFeature(self._sel_fid)
            sel_geom = sel_feat.geometry()
            tol      = self._hit_tol()
            check_pt = sv.point if sv is not None else map_pt
            if (not sel_geom.isEmpty()
                    and sel_geom.distance(QgsGeometry.fromPointXY(check_pt)) <= tol):
                if shift:
                    self._enter_idle()           # Shift+click active feature edge → deselect
                else:
                    # Record potential segment drag; drag = move segment, plain click = nothing
                    seg = self._find_nearest_segment(map_pt, sel_geom)
                    if seg is not None:
                        self._seg_press_pos = event.pos()
                        self._seg_vidx1, self._seg_vidx2 = seg
                        self._seg_orig_geom = QgsGeometry(sel_geom)
            else:
                edge = self._find_edge_near(map_pt)
                if edge:
                    other_lyr, other_fid = edge
                    if shift:
                        if not self._remove_from_extra(other_lyr, other_fid):
                            if (id(other_lyr) == id(self._sel_layer)
                                    and other_fid == self._sel_fid):
                                self._enter_idle()
                    else:
                        self._enter_feature(other_lyr, other_fid)
                else:
                    self._drag_start       = event.pos()
                    self._drag_start_state = _S_FEATURE
            return

        # IDLE
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if sv:
            if shift:
                self._remove_from_extra(sv.layer, sv.fid)
            else:
                self._enter_gripped(sv)
                self._vtx_press_pos = event.pos()  # track for immediate drag
        else:
            edge = self._find_edge_near(map_pt)
            if edge:
                other_lyr, other_fid = edge
                if shift:
                    self._remove_from_extra(other_lyr, other_fid)
                else:
                    self._enter_feature(*edge)
            else:
                # Empty space — start drag-to-select
                self._drag_start       = event.pos()
                self._drag_start_state = _S_IDLE

        self.terminal_dock.command.setFocus()

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return

        # Vertex press-drag release
        if self._vtx_press_pos is not None:
            was_dragging        = self._vtx_dragging
            self._vtx_press_pos = None
            self._vtx_dragging  = False
            if was_dragging:
                raw_pt    = self.toMapCoordinates(event.pos())
                commit_pt = self._snap_point(event.pos(), raw_pt)
                self._snap_marker.setVisible(False)
                self._commit_move(commit_pt)
                self.terminal_dock.command.setFocus()
            # plain click: already gripped, stay there
            return

        # Segment drag release
        if self._seg_press_pos is not None:
            was_dragging = self._seg_dragging
            press_pos    = self._seg_press_pos
            vidx1        = self._seg_vidx1
            vidx2        = self._seg_vidx2
            self._end_seg_drag()
            if was_dragging:
                start_map = self.toMapCoordinates(press_pos)
                cur_map   = self.toMapCoordinates(event.pos())
                self._commit_seg_drag(
                    vidx1, vidx2,
                    cur_map.x() - start_map.x(),
                    cur_map.y() - start_map.y(),
                )
            # plain click on edge: do nothing — stay in FEATURE state as-is
            return

        # Rect drag-to-select release
        if self._drag_start is None:
            return
        start_pos    = self._drag_start
        start_state  = self._drag_start_state
        was_dragging = self._is_dragging
        self._clear_drag()

        if was_dragging:
            p1       = self.toMapCoordinates(start_pos)
            p2       = self.toMapCoordinates(event.pos())
            crossing = event.pos().x() < start_pos.x()   # R→L = crossing
            rect = QgsRectangle(
                min(p1.x(), p2.x()), min(p1.y(), p2.y()),
                max(p1.x(), p2.x()), max(p1.y(), p2.y()),
            )
            if start_state in (_S_FEATURE, _S_GRIPPED):
                self._grip_vertices_in_rect(rect)
            else:
                self._select_in_rect(rect, crossing)
        elif start_state in (_S_FEATURE, _S_GRIPPED):
            self._enter_idle()   # plain click on empty space → clear

    def mouseDoubleClickEvent(self, event):
        self.terminal_dock.command.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            if self._state == _S_MOVING:
                self._cancel_move()
            elif self._state in (_S_GRIPPED, _S_FEATURE):
                self._enter_idle()
        elif key in (Qt.Key_Delete, Qt.Key_Backspace):
            if self._state in (_S_GRIPPED, _S_MOVING):
                self._delete_gripped()
            elif self._state == _S_FEATURE:
                self._delete_feature()
