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
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor


_SelVtx = namedtuple('_SelVtx', ['layer', 'fid', 'vidx', 'point'])

_S_IDLE    = 0
_S_GRIPPED = 1
_S_MOVING  = 2
_S_FEATURE = 3   # feature highlighted by edge click; no vertex gripped yet

_HIT_PX = 10

_C_HOVER      = QColor(255, 210, 0, 240)    # yellow – hover circle
_C_GRIP_HOT   = QColor(0, 60, 220, 255)     # deep blue – hot (gripped) vertex
_C_MOVE       = QColor(255, 130, 0, 220)    # orange – live-preview in MOVING
_C_MOVE_FILL  = QColor(255, 130, 0, 30)     # faint orange fill
_C_FEATURE    = QColor(0, 200, 80, 220)     # green – edge-selected feature outline
_C_FEAT_FILL  = QColor(0, 200, 80, 20)      # faint green fill
_C_FEAT_VTX   = QColor(0, 180, 60, 200)     # green – vertex markers on selected feature


class VertexSelector(QgsMapTool):
    """Always-on vertex grip editor — the permanent default map tool."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None   # injected by UgsurvMaptool.set_default_tool()

        self._state   = _S_IDLE
        self._gripped = None        # _SelVtx – the hot grip

        # IDLE: yellow hover circle near cursor
        self._hover_marker = self._make_marker(_C_HOVER, QgsVertexMarker.ICON_CIRCLE, 14)
        self._hover_marker.setVisible(False)

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
        self._rm(self._feature_band)
        self._feature_band = None
        self._sel_layer = None
        self._sel_fid   = None

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _enter_idle(self):
        self._state   = _S_IDLE
        self._gripped = None
        self._clear_grip_markers()
        self._clear_bands()
        self._clear_feature()
        if self._hover_marker is not None:
            self._hover_marker.setVisible(False)

    def _enter_feature(self, layer, fid):
        """Highlight the feature whose edge was clicked."""
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

        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if not geom.isEmpty():
            gt = QgsWkbTypes.geometryType(geom.wkbType())
            self._feature_band = self._make_band(gt, _C_FEATURE, _C_FEAT_FILL, width=2)
            self._feature_band.setToGeometry(geom, layer)

        self._log(
            f"\nFeature {fid} of '{layer.name()}' selected"
            f"  →  click a vertex to grip it"
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

        self._log("\nMove  →  move cursor to new position and click  |  Esc or RMB to cancel")

    def _commit_move(self, map_pt):
        """Move the gripped vertex to map_pt and stay gripped at new position."""
        sv = self._gripped
        if not sv.layer.isEditable():
            sv.layer.startEditing()
        sv.layer.moveVertex(map_pt.x(), map_pt.y(), sv.fid, sv.vidx)
        sv.layer.triggerRepaint()

        self._log(
            f"\nMoved  ({sv.point.x():.3f}, {sv.point.y():.3f})"
            f" → ({map_pt.x():.3f}, {map_pt.y():.3f})"
        )
        # Re-grip at new position (re-reads updated geometry from layer)
        new_sv = _SelVtx(sv.layer, sv.fid, sv.vidx, map_pt)
        self._enter_gripped(new_sv)

    def _cancel_move(self):
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
        self._enter_idle()

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

    def _log(self, msg):
        self.terminal_dock.commandOutputText += msg
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        # Recreate hover marker if it was removed when a drawing tool took over
        if self._hover_marker is None:
            self._hover_marker = self._make_marker(_C_HOVER, QgsVertexMarker.ICON_CIRCLE, 14)
            self._hover_marker.setVisible(False)

    def deactivate(self):
        """Called by UgsurvMaptool._evict() when a drawing tool takes over.
        Clean up all visual elements. Do NOT call _maptool.clear_tool() —
        this tool IS the default; the maptool handles its own reversion.
        """
        self._enter_idle()
        self._rm(self._hover_marker)
        self._hover_marker = None   # recreated in activate() when we come back
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())

        if self._state == _S_MOVING:
            # Update live rubber-band: replace gripped vertex with cursor
            sv   = self._gripped
            feat = sv.layer.getFeature(sv.fid)
            if not feat.geometry().isEmpty():
                geom_copy = QgsGeometry(feat.geometry())
                geom_copy.moveVertex(map_pt.x(), map_pt.y(), sv.vidx)
                self._move_band.setToGeometry(geom_copy, sv.layer)
            return

        if self._state == _S_GRIPPED:
            return  # grip markers already drawn; no extra hover feedback needed

        # IDLE or FEATURE — show yellow hover circle on the nearest vertex
        sv = self._find_vertex_near(map_pt)
        if self._hover_marker is None:
            return
        if sv:
            self._hover_marker.setCenter(sv.point)
            self._hover_marker.setVisible(True)
        else:
            self._hover_marker.setVisible(False)

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())

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
            self._commit_move(map_pt)
            return

        sv = self._find_vertex_near(map_pt)

        if self._state == _S_GRIPPED:
            if self._same_grip(sv):
                self._enter_moving()        # second click on same grip → move
            elif sv:
                self._enter_gripped(sv)     # different vertex → switch grip
            else:
                self._enter_idle()          # empty space → deselect
            return

        if self._state == _S_FEATURE:
            if sv:
                self._enter_gripped(sv)     # vertex clicked → grip it
            else:
                edge = self._find_edge_near(map_pt)
                if edge:
                    lyr, fid = edge
                    if id(lyr) != id(self._sel_layer) or fid != self._sel_fid:
                        self._enter_feature(lyr, fid)   # different feature edge
                    # same feature edge clicked again → do nothing
                else:
                    self._enter_idle()
            return

        # IDLE
        if sv:
            self._enter_gripped(sv)
        else:
            edge = self._find_edge_near(map_pt)
            if edge:
                self._enter_feature(*edge)

    def canvasReleaseEvent(self, event):
        pass   # all logic handled in press

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
