# -*- coding: utf-8 -*-
"""
AutoCAD-style EXTEND tool.

Workflow
────────
Phase 1  (_ST_SEARCH)
    Hover near any line.  The preview rubber band shows the extension gradient —
    a straight continuation of the last segment from the nearest endpoint.
    Left-click → lock onto that endpoint.

Phase 2  (_ST_LOCKED)
    The endpoint and gradient are frozen.  The cursor can roam freely; the
    preview rubber band stays along the locked gradient at all times.
    Moving the cursor forward along the gradient stretches the preview and
    updates the live distance value in the dynamic-input widget.
    Left-click      → apply extension at the current cursor-projected distance.
    Type dist+Enter → apply extension at the typed distance.
    Esc / RMB       → release lock, return to Phase 1 (_ST_SEARCH).
"""

import contextlib
import math

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import (
    QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType, SnapType
from ...core import style as _style

_SNAP_ICONS = {
    SnapType.VERTEX:        (_style.SNAP_ICON['endpoint'],     _style._CC_COLOR),
    SnapType.POINT:         (_style.SNAP_ICON['point'],        _style._CC_COLOR),
    SnapType.MIDPOINT:      (_style.SNAP_ICON['midpoint'],     _style._CC_COLOR),
    SnapType.CENTER:        (_style.SNAP_ICON['center'],       _style._CC_COLOR),
    SnapType.INTERSECTION:  (_style.SNAP_ICON['intersection'], _style._CC_COLOR),
    SnapType.PERPENDICULAR: (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.EXTENSION:     (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.NEAREST:       (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.SELF:          (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    SnapType.GRID:          (QgsVertexMarker.ICON_CROSS,       _style.SNAP_GRID_COLOR),
}

# ── state constants ────────────────────────────────────────────────────────────
_ST_SEARCH = 0   # hovering to find an endpoint to lock
_ST_LOCKED = 1   # endpoint locked, cursor constrained to gradient

_HIT_PX = 10


class ExtendTool(QgsMapTool):

    inputModeChanged = pyqtSignal(str, str)
    promptChanged    = pyqtSignal(str)

    # ── construction ───────────────────────────────────────────────────────────

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state = _ST_SEARCH

        # Rubber bands
        self._hover_band   = self._make_band(_style.RB_HOVER,    dashed=False)
        self._preview_band = self._make_band(_style.RB_PREVIEW,  dashed=True)
        self._hover_band.setVisible(False)
        self._preview_band.setVisible(False)

        # Hovered endpoint (updated every mouse-move in _ST_SEARCH)
        self._hover_layer  = None
        self._hover_fid    = None
        self._hover_ep_idx = None        # 0 = start vertex, -1 = end vertex
        self._hover_ep     = None        # QgsPointXY
        self._hover_dir    = (0.0, 1.0)  # unit vector away from the line

        # Locked endpoint/gradient (_ST_LOCKED)
        self._locked_layer  = None
        self._locked_fid    = None
        self._locked_ep_idx = None
        self._locked_ep     = None        # QgsPointXY; None = not locked
        self._locked_dir    = (0.0, 1.0)

        self._modified_layers = set()
        self._snap_marker = None

    # ── logging ────────────────────────────────────────────────────────────────

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    # ── rubber-band helpers ────────────────────────────────────────────────────

    def _make_band(self, color, dashed=False):
        band = QgsRubberBand(self._canvas, QgsWkbTypes.GeometryType.LineGeometry)
        band.setColor(color)
        band.setWidth(_style.RB_WIDTH)
        band.setLineStyle(_style.RB_LINE_STYLE)
        return band

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    # ── geometry helpers ───────────────────────────────────────────────────────

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

    def _line_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.isSpatial()
            and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.GeometryType.LineGeometry
        ]

    def _find_line_near(self, map_pt):
        """Return (layer, feat) for the closest line within hit tolerance, or (None, None)."""
        tol  = self._hit_tol()
        rect = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                            map_pt.x()+tol, map_pt.y()+tol)
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        best_layer, best_feat, best_d = None, None, float('inf')
        for lyr in self._line_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                d = feat.geometry().distance(pt_geom)
                if d < best_d:
                    best_d, best_layer, best_feat = d, lyr, feat
        return (best_layer, best_feat) if best_d <= tol else (None, None)

    def _ep_and_direction(self, line_geom, map_pt):
        """
        Return (ep_idx, ep, dx, dy).
        ep      — the endpoint nearest to map_pt (QgsPointXY).
        (dx,dy) — unit vector pointing AWAY from the line (the extension direction).
        ep_idx  — 0 if it is the start vertex, -1 if the end vertex.
        Returns (None, None, 0, 1) if geometry is degenerate.
        """
        pts = line_geom.asPolyline()
        if len(pts) < 2:
            return None, None, 0.0, 1.0

        if map_pt.distance(pts[0]) <= map_pt.distance(pts[-1]):
            ep_idx, ep, adj = 0, pts[0], pts[1]
        else:
            ep_idx, ep, adj = -1, pts[-1], pts[-2]

        dx = ep.x() - adj.x()
        dy = ep.y() - adj.y()
        length = math.sqrt(dx * dx + dy * dy)
        if length < 1e-10:
            return None, None, 0.0, 1.0
        return ep_idx, ep, dx / length, dy / length

    def _project_distance(self, ep, dx, dy, map_pt):
        """Scalar projection of map_pt onto the ray from ep in direction (dx,dy), clamped ≥ 0."""
        return max(0.0, (map_pt.x() - ep.x()) * dx + (map_pt.y() - ep.y()) * dy)

    # ── locking / unlocking ────────────────────────────────────────────────────

    def _lock(self):
        """Lock onto the currently-hovered endpoint."""
        self._locked_layer  = self._hover_layer
        self._locked_fid    = self._hover_fid
        self._locked_ep_idx = self._hover_ep_idx
        self._locked_ep     = self._hover_ep
        self._locked_dir    = self._hover_dir
        self._state         = _ST_LOCKED
        self._log("  Gradient locked", "#88ccff")
        self.promptChanged.emit("Click  or  type extension distance:")

    def _unlock(self):
        """Release the lock and return to _ST_SEARCH."""
        self._locked_layer  = None
        self._locked_fid    = None
        self._locked_ep_idx = None
        self._locked_ep     = None
        self._locked_dir    = (0.0, 1.0)
        self._state         = _ST_SEARCH
        self._preview_band.setVisible(False)
        self._log("  Lock released — hover another line end")
        self.promptChanged.emit("Click a line near its endpoint:")

    # ── extension commit ───────────────────────────────────────────────────────

    def _commit(self, layer, fid, ep_idx, ep, ext_pt, dist):
        """Write the extended geometry to the layer."""
        feat = layer.getFeature(fid)
        if not feat.isValid():
            self._log("  Feature no longer valid", "#ff8844")
            return
        pts = list(feat.geometry().asPolyline())
        if ep_idx == -1:
            pts.append(ext_pt)
        else:
            pts.insert(0, ext_pt)
        new_geom = QgsGeometry.fromPolylineXY(pts)
        if not layer.isEditable():
            layer.startEditing()
        layer.changeGeometry(fid, new_geom)
        layer.triggerRepaint()
        self._modified_layers.add(layer)
        self._log(f"  Extended {dist:.3f} m on '{layer.name()}'", "#88ff88")

    def _apply_at_cursor(self, map_pt):
        """Apply extension at the cursor position projected onto the locked gradient."""
        ep = self._locked_ep
        dx, dy = self._locked_dir
        t = self._project_distance(ep, dx, dy, map_pt)
        if t < 1e-6:
            self._log("  Cursor is behind the endpoint — move forward along the gradient")
            return
        ext_pt = QgsPointXY(ep.x() + t * dx, ep.y() + t * dy)
        self._commit(self._locked_layer, self._locked_fid,
                     self._locked_ep_idx, ep, ext_pt, t)
        self._finish()

    def _apply_at_distance(self, dist):
        """Apply extension at an explicitly typed distance along the locked gradient."""
        ep = self._locked_ep
        dx, dy = self._locked_dir
        ext_pt = QgsPointXY(ep.x() + dist * dx, ep.y() + dist * dy)
        self._commit(self._locked_layer, self._locked_fid,
                     self._locked_ep_idx, ep, ext_pt, dist)
        self._finish()

    # ── preview ────────────────────────────────────────────────────────────────

    def _update_preview(self, map_pt):
        self._hover_band.setVisible(False)

        if self._state == _ST_LOCKED:
            # Gradient is frozen — project cursor onto the locked direction only
            ep     = self._locked_ep
            dx, dy = self._locked_dir
            t      = self._project_distance(ep, dx, dy, map_pt)
            if t > 1e-6:
                self._preview_band.setToGeometry(
                    QgsGeometry.fromPolylineXY([ep, QgsPointXY(ep.x()+t*dx, ep.y()+t*dy)]),
                    self._locked_layer,
                )
                self._preview_band.setVisible(True)
                self.promptChanged.emit(f"Extension distance <{t:.3f}m>:")
                dyn = getattr(self._ctx, 'dyn_widget', None)
                if dyn:
                    dyn.set_live_value(t)
            else:
                self._preview_band.setVisible(False)
            return

        # _ST_SEARCH — find nearest line and show direction-constrained preview
        self._hover_ep = None
        layer, feat = self._find_line_near(map_pt)
        if feat is None or feat.geometry().isMultipart():
            self._preview_band.setVisible(False)
            return

        ep_idx, ep, dx, dy = self._ep_and_direction(feat.geometry(), map_pt)
        if ep is None:
            self._preview_band.setVisible(False)
            return

        self._hover_layer  = layer
        self._hover_fid    = feat.id()
        self._hover_ep_idx = ep_idx
        self._hover_ep     = ep
        self._hover_dir    = (dx, dy)

        # Show a minimum stub so the extension direction is always visible on hover
        t        = self._project_distance(ep, dx, dy, map_pt)
        stub_len = self._hit_tol() * 3
        show_t   = max(t, stub_len)
        self._preview_band.setToGeometry(
            QgsGeometry.fromPolylineXY([ep, QgsPointXY(ep.x()+show_t*dx, ep.y()+show_t*dy)]),
            layer,
        )
        self._preview_band.setVisible(True)
        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn:
            dyn.set_live_value(t)

    # ── typed-input dispatch ───────────────────────────────────────────────────

    def _dispatch(self, sem):
        if sem is None:
            return
        if getattr(sem, 'type', None) != EventType.VALUE_ENTERED:
            return
        if sem.value is None:
            return

        # Which endpoint to use
        if self._state == _ST_LOCKED:
            ep, ep_idx = self._locked_ep, self._locked_ep_idx
            dx, dy     = self._locked_dir
            layer, fid = self._locked_layer, self._locked_fid
        else:
            ep, ep_idx = self._hover_ep, self._hover_ep_idx
            dx, dy     = self._hover_dir
            layer, fid = self._hover_layer, self._hover_fid

        if ep is None or layer is None:
            self._log("  Hover a line endpoint first", "#ff8844")
            return

        try:
            dist = float(sem.value)
        except (TypeError, ValueError):
            return
        if dist <= 0:
            self._log("  Distance must be positive", "#ff8844")
            return

        ext_pt = QgsPointXY(ep.x() + dist * dx, ep.y() + dist * dy)
        self._commit(layer, fid, ep_idx, ep, ext_pt, dist)
        self._finish()

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    def _finish(self):
        """Clear all rubber bands immediately, then return to the home tool."""
        self._preview_band.setVisible(False)
        self._hover_band.setVisible(False)
        self._go_home()

    def _snapped(self, raw_pt):
        engine = getattr(self._ctx, 'snap_engine', None)
        if engine:
            result = engine.resolve(raw_pt, self._canvas)
            if result:
                self._show_snap_marker(result.point, result.snap_type)
                return result.point
        self._hide_snap_marker()
        return raw_pt

    def _show_snap_marker(self, pt, snap_type):
        icon, color = _SNAP_ICONS.get(snap_type, (QgsVertexMarker.ICON_BOX, _style._CC_COLOR))
        if self._snap_marker is None:
            self._snap_marker = QgsVertexMarker(self._canvas)
            self._snap_marker.setIconSize(_style.SNAP_ICON_SIZE)
            self._snap_marker.setPenWidth(_style.SNAP_PEN_WIDTH)
        self._snap_marker.setIconType(icon)
        self._snap_marker.setColor(color)
        self._snap_marker.setCenter(pt)
        self._snap_marker.show()

    def _hide_snap_marker(self):
        if self._snap_marker:
            self._snap_marker.hide()

    def _clear_snap_marker(self):
        if self._snap_marker is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(self._snap_marker)
            self._snap_marker = None

    def _lock_from_pending(self, pending: dict) -> bool:
        """Lock directly onto an endpoint passed from grip/select tool. Returns True on success."""
        layer = QgsProject.instance().mapLayer(pending['layer_id'])
        if not layer:
            return False
        feat = layer.getFeature(pending['fid'])
        if not feat.isValid():
            return False
        pts = feat.geometry().asPolyline()
        if len(pts) < 2:
            return False
        if pending['at_start']:
            ep, adj, ep_idx = pts[0], pts[1], 0
        else:
            ep, adj, ep_idx = pts[-1], pts[-2], -1
        dx   = ep.x() - adj.x()
        dy   = ep.y() - adj.y()
        dist = math.hypot(dx, dy)
        if dist < 1e-10:
            return False
        self._locked_layer  = layer
        self._locked_fid    = feat.id()
        self._locked_ep_idx = ep_idx
        self._locked_ep     = ep
        self._locked_dir    = (dx / dist, dy / dist)
        self._state         = _ST_LOCKED
        self._log(f"EXTEND  ──  locked on '{layer.name()}' endpoint", "#aaddff")
        self._last_input_mode   = "value"
        self._last_input_prompt = "Click or type extension distance:"
        self.inputModeChanged.emit("value", "Click or type extension distance:")
        return True

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        pending = getattr(self._ctx, 'extend_pending', None)
        if pending:
            self._ctx.extend_pending = None
            if self._lock_from_pending(pending):
                return
        self._log("EXTEND  ──  click a line near its endpoint", "#aaddff")
        self._last_input_mode   = "no_value"
        self._last_input_prompt = "Click a line near its endpoint:"
        self.inputModeChanged.emit("no_value", "Click a line near its endpoint:")

    def deactivate(self):
        self._clear_snap_marker()
        self._preview_band.setVisible(False)
        self._hover_band.setVisible(False)
        self._hover_ep  = None
        self._locked_ep = None
        self._state     = _ST_SEARCH
        self._modified_layers.clear()
        self.inputModeChanged.emit("", "")
        super().deactivate()

    # ── Qt event handlers ──────────────────────────────────────────────────────

    def canvasMoveEvent(self, event):
        self._update_preview(self._snapped(self.toMapCoordinates(event.pos())))

    def canvasPressEvent(self, event):
        map_pt = self._snapped(self.toMapCoordinates(event.pos()))
        btn    = event.button()

        if btn == Qt.MouseButton.RightButton:
            if self._state == _ST_LOCKED:
                self._unlock()
            else:
                self._finish()
            return

        if btn != Qt.MouseButton.LeftButton:
            return

        if self._state == _ST_SEARCH:
            layer, feat = self._find_line_near(map_pt)
            if feat is None or feat.geometry().isMultipart():
                self._log("  No line endpoint found near cursor")
                return
            ep_idx, ep, dx, dy = self._ep_and_direction(feat.geometry(), map_pt)
            if ep is None:
                self._log("  No line endpoint found near cursor")
                return
            self._hover_layer  = layer
            self._hover_fid    = feat.id()
            self._hover_ep_idx = ep_idx
            self._hover_ep     = ep
            self._hover_dir    = (dx, dy)
            self._lock()

        elif self._state == _ST_LOCKED:
            self._apply_at_cursor(map_pt)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._state == _ST_LOCKED:
                self._unlock()
            else:
                self._finish()
        # In _ST_SEARCH / _ST_LOCKED, Enter with a typed value is handled by
        # _dispatch(VALUE_ENTERED) via the dynamic-input widget; nothing extra needed here.
