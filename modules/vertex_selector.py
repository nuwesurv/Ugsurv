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
    QgsCurvePolygon,
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
from .snapSettingConfig import snapSettingConfig


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

        self._state   = _S_IDLE
        self._gripped = None        # _SelVtx – the hot grip

        snapSettingConfig()

        # IDLE: yellow hover circle near cursor
        self._hover_marker = self._make_marker(_C_HOVER, QgsVertexMarker.ICON_CIRCLE, 14)
        self._hover_marker.setVisible(False)

        # MOVING: cyan snap indicator
        self._snap_marker = self._make_marker(QColor(0, 180, 255, 220), QgsVertexMarker.ICON_CIRCLE, 10)
        self._snap_marker.setVisible(False)

        # GRIPPED: one marker per vertex of the gripped feature
        self._grip_markers = []

        # GRIPPED: rubber band showing the full feature geometry
        self._geom_band = None

        # MOVING: live rubber band updated every mouse move
        self._move_band = None

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
        self._state   = _S_IDLE
        self._gripped = None
        self._clear_grip_markers()
        self._clear_bands()
        self._clear_feature()
        self._clear_extra_selection()
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
            m = self._make_marker(_C_FEAT_VTX, QgsVertexMarker.ICON_CIRCLE, 8)
            m.setCenter(vpt)
            self._feature_vtx_markers.append(m)

        # "+" markers at the midpoint of every segment
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
                12 if hot else 8,
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

        self._log(
            f"\nGripped vertex {sv.vidx + 1}/{len(verts)}"
            f" of '{sv.layer.name()}' feature {sv.fid}"
            f"  →  click grip again to move, Del to delete"
        )

    def _enter_moving(self):
        """Activate live rubber-band move mode."""
        self._state = _S_MOVING
        if self._geom_band:
            self._geom_band.setVisible(False)   # hide static outline; move band takes over
        self._rm(self._move_band)               # discard any extend-preview band
        self._move_band = None

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

        if sv.layer.name() == "circles" and not geom.isEmpty():
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

        if sv.layer.name() == "circles":
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
        self._log(
            f"\nMoved  ({sv.point.x():.3f}, {sv.point.y():.3f})"
            f" → ({map_pt.x():.3f}, {map_pt.y():.3f})"
        )
        new_sv = _SelVtx(sv.layer, sv.fid, sv.vidx, map_pt)
        self._enter_gripped(new_sv)

    def _circle_center_from_geom(self, geom):
        """Return the center QgsPointXY of a 5-point circle geometry (E/S/W/N/E)."""
        verts = self._geom_verts(geom)
        if len(verts) < 3:
            return None
        v0, v2 = verts[0][1], verts[2][1]   # East and West are equidistant from center
        return QgsPointXY((v0.x() + v2.x()) / 2, (v0.y() + v2.y()) / 2)

    def _build_circle_geom(self, center, radius):
        """Rebuild a 5-point QgsCurvePolygon circle from center + radius."""
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
        cp = QgsCurvePolygon()
        cp.setExteriorRing(cs)
        return QgsGeometry(cp)

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

    def _cancel_move(self):
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._snap_marker.setVisible(False)
        self._rm(self._move_band)
        self._move_band = None
        if self._geom_band:
            self._geom_band.setVisible(True)
        self._state = _S_GRIPPED
        self._log("\nMove cancelled")

    def _delete_gripped(self):
        sv = self._gripped
        if not sv.layer.isEditable():
            sv.layer.startEditing()
        feat = sv.layer.getFeature(sv.fid)
        geom = QgsGeometry(feat.geometry())
        geom.deleteVertex(sv.vidx)
        sv.layer.changeGeometry(sv.fid, geom)
        sv.layer.triggerRepaint()
        self._log(f"\nDeleted vertex {sv.vidx + 1} of feature {sv.fid} on '{sv.layer.name()}'")
        self._update_closed_attrs(sv)
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
        """Insert a new vertex at map_pt on the selected feature's nearest segment."""
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
        self._log(f"\nInserted vertex at ({map_pt.x():.3f}, {map_pt.y():.3f})")
        self._enter_feature(lyr, fid)   # refresh green highlight with new vertex

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

    def _snap_point(self, screen_pt, raw_pt):
        """Return snapped point using QGIS native snapping, or raw_pt if no snap.

        Uses canvas.snappingUtils() so CRS transformations and project snapping
        config are handled automatically — unlike a manual getFeatures(rect) query
        which requires the rect to be in the layer's own CRS.
        """
        match = self.canvas.snappingUtils().snapToMap(screen_pt)
        if match.isValid():
            snapped = match.point()
            self._snap_marker.setCenter(snapped)
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

    def _find_vertex_near(self, map_pt):
        tol  = self._hit_tol()
        best, best_d = None, tol
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        for lyr in self._vector_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                for vidx, vpt in self._geom_verts(feat.geometry()):
                    d = map_pt.distance(vpt)
                    if d < best_d:
                        best_d = d
                        best = _SelVtx(lyr, feat.id(), vidx, vpt)
        return best

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
        if self._state == _S_GRIPPED and self._gripped_can_extend():
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
            self._hover_marker = self._make_marker(_C_HOVER, QgsVertexMarker.ICON_CIRCLE, 14)
            self._hover_marker.setVisible(False)
        if self._snap_marker is None:
            self._snap_marker = self._make_marker(QColor(0, 180, 255, 220), QgsVertexMarker.ICON_CIRCLE, 10)
            self._snap_marker.setVisible(False)

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
        super().deactivate()

    def canvasMoveEvent(self, event):
        raw_pt = self.toMapCoordinates(event.pos())

        if self._state == _S_MOVING:
            map_pt = self._snap_point(event.pos(), raw_pt)
            # Update live rubber-band: replace gripped vertex with cursor
            sv   = self._gripped
            feat = sv.layer.getFeature(sv.fid)
            if not feat.geometry().isEmpty():
                if sv.layer.name() == "circles":
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

        sv = self._find_vertex_near(map_pt)

        if self._state == _S_GRIPPED:
            if self._same_grip(sv):
                self._enter_moving()        # second click on same grip → move
            elif sv and self._gripped_can_extend() and self._is_opposite_endpoint(sv):
                # Clicked the opposite endpoint of the same feature → close the line
                if self._move_band is not None:
                    self._move_band.reset(QgsWkbTypes.LineGeometry)
                self._snap_marker.setVisible(False)
                self._extend_line(sv.point)
            elif sv and id(sv.layer) == id(self._gripped.layer) and sv.fid == self._gripped.fid:
                self._enter_gripped(sv)     # different vertex of same feature → switch grip
            elif sv and not self._gripped_can_extend():
                pass   # neighbour vertex while gripped on mid-vertex — stay gripped
            elif self._gripped_can_extend():
                # Gripped on endpoint: extend, landing precisely on a neighbour vertex if hit
                commit_pt = sv.point if sv is not None else self._snap_point(event.pos(), raw_pt)
                self._snap_marker.setVisible(False)
                if self._move_band is not None:
                    self._move_band.reset(QgsWkbTypes.LineGeometry)
                self._extend_line(commit_pt)
            else:
                self._enter_idle()          # mid-vertex gripped + empty space → deselect
            return

        if self._state == _S_FEATURE:
            mpt = self._find_midpoint_near(map_pt)
            if mpt is not None:
                self._insert_vertex_on_segment(mpt)  # "+" clicked → insert at exact midpoint
                return
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            if sv and id(sv.layer) == id(self._sel_layer) and sv.fid == self._sel_fid:
                if shift:
                    # Shift+click on active feature → deselect it entirely
                    self._enter_idle()
                else:
                    self._enter_gripped(sv)          # vertex on selected feature → grip it
            else:
                sel_feat = self._sel_layer.getFeature(self._sel_fid)
                sel_geom = sel_feat.geometry()
                tol      = self._hit_tol()
                check_pt = sv.point if sv is not None else map_pt
                if (not sel_geom.isEmpty()
                        and sel_geom.distance(QgsGeometry.fromPointXY(check_pt)) <= tol):
                    if shift:
                        self._enter_idle()           # Shift+click active feature edge → deselect
                    else:
                        self._insert_vertex_on_segment(check_pt)
                else:
                    # Click is not on the active feature — check for another feature
                    edge = self._find_edge_near(map_pt)
                    if edge:
                        other_lyr, other_fid = edge
                        if shift:
                            # Shift+click another feature → remove it from selection
                            if not self._remove_from_extra(other_lyr, other_fid):
                                # Not in extra: check if it's somehow the active one
                                if (id(other_lyr) == id(self._sel_layer)
                                        and other_fid == self._sel_fid):
                                    self._enter_idle()
                        else:
                            # Plain click another feature → add to multi-selection
                            self._enter_feature(other_lyr, other_fid)
                    else:
                        self._enter_idle()           # click empty space → clear all
            return

        # IDLE
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if sv:
            if shift:
                self._remove_from_extra(sv.layer, sv.fid)
            else:
                self._enter_gripped(sv)
        else:
            edge = self._find_edge_near(map_pt)
            if edge:
                other_lyr, other_fid = edge
                if shift:
                    self._remove_from_extra(other_lyr, other_fid)
                else:
                    self._enter_feature(*edge)

        self.terminal_dock.command.setFocus()

    def canvasReleaseEvent(self, event):
        pass   # all logic handled in press

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
