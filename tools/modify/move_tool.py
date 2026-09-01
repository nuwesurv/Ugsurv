# -*- coding: utf-8 -*-
"""
AutoCAD-style MOVE tool.

Workflow
────────
1. Click features to build a selection set.
   Shift+click removes a feature.  Enter/RMB (with selection) → proceed.
2. Click base point.
3. Click destination  or  type "@dx,dy" / "x,y" + Enter → move.

Esc at any phase → cancel / exit.
"""

import contextlib

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType
from ...core import style as _style

_C_HIGHLIGHT = _style.RB_SOURCE
_C_HL_FILL   = _style.RB_SOURCE_FILL
_C_PREVIEW   = _style.RB_PREVIEW
_C_PREV_FILL = _style.RB_PREVIEW_FILL

_ST_SELECT = 0
_ST_BASE   = 1
_ST_PLACE  = 2

_HIT_PX = 10

_HINT = {
    _ST_SELECT: "Select features",
    _ST_BASE:   "Click base point",
    _ST_PLACE:  'Click destination  or  type "@dx,dy"',
}

_HINT_STYLE = (
    "QLabel{background:rgba(20,20,20,210);color:#f0f0f0;"
    "border:1px solid rgba(255,255,255,80);border-radius:4px;"
    "padding:3px 8px;font-size:9pt;}"
)


class MoveTool(QgsMapTool):
    """Move features — AutoCAD-style multi-select → base point → destination."""

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state        = _ST_SELECT
        self._sel_features = []   # [(layer, fid, geom_copy), ...]
        self._sel_bands    = []
        self._prev_bands   = []
        self._base_pt: QgsPointXY | None = None
        self._last_input_ref: QgsPointXY | None = None

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()

    # ── logging / hint ────────────────────────────────────────────────────

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _show_hint(self, screen_pos):
        text = _HINT.get(self._state, "")
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self._canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self._canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    # ── helpers ───────────────────────────────────────────────────────────

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

    def _find_feature_near(self, map_pt):
        tol     = self._hit_tol()
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        rect    = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                               map_pt.x()+tol, map_pt.y()+tol)
        best, best_d = None, tol
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial():
                continue
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                d = geom.distance(pt_geom)
                if d < best_d:
                    best_d = d
                    best = (lyr, feat.id(), QgsGeometry(geom))
        return best

    def _make_band(self, geom_type, color, fill_color, width=_style.RB_WIDTH, dashed=False):
        band = QgsRubberBand(self._canvas, geom_type)
        band.setColor(color)
        band.setFillColor(fill_color)
        band.setWidth(width)
        band.setLineStyle(_style.RB_LINE_STYLE)
        return band

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    # ── selection ─────────────────────────────────────────────────────────

    def _sel_key(self, layer, fid):
        return (id(layer), fid)

    def _existing_keys(self):
        return [self._sel_key(lyr, f) for lyr, f, _ in self._sel_features]

    def _add_to_selection(self, layer, fid, geom):
        if self._sel_key(layer, fid) in self._existing_keys():
            return False
        self._sel_features.append((layer, fid, QgsGeometry(geom)))
        gt = QgsWkbTypes.geometryType(geom.wkbType())
        hl = self._make_band(gt, _C_HIGHLIGHT, _C_HL_FILL, width=2)
        hl.setToGeometry(geom, layer)
        self._sel_bands.append(hl)
        prev = self._make_band(gt, _C_PREVIEW, _C_PREV_FILL, width=2, dashed=True)
        prev.setVisible(False)
        self._prev_bands.append(prev)
        return True

    def _remove_from_selection(self, layer, fid):
        key  = self._sel_key(layer, fid)
        keys = self._existing_keys()
        if key not in keys:
            return False
        idx = keys.index(key)
        self._sel_features.pop(idx)
        self._rm(self._sel_bands.pop(idx))
        self._rm(self._prev_bands.pop(idx))
        return True

    def _clear_selection(self):
        for b in self._sel_bands:
            self._rm(b)
        for b in self._prev_bands:
            self._rm(b)
        self._sel_features = []
        self._sel_bands    = []
        self._prev_bands   = []

    # ── state transitions ─────────────────────────────────────────────────

    def _enter_base(self):
        for b in self._prev_bands:
            b.setVisible(False)
        self._state = _ST_BASE
        n = len(self._sel_features)
        self._log(f"  {n} feature(s) selected  →  click base point", "#88ccff")

    def _enter_place(self, base_pt: QgsPointXY):
        self._base_pt = base_pt
        self._last_input_ref = base_pt
        self._state   = _ST_PLACE
        for layer, fid, geom in self._sel_features:
            if not layer.isEditable():
                layer.startEditing()
        for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
            band.setToGeometry(geom, layer)
            band.setVisible(True)
        self._log('  Click destination  or  type "@dx,dy"', "#88ccff")

    def _update_preview(self, map_pt: QgsPointXY):
        if self._state != _ST_PLACE or not self._base_pt:
            return
        dx = map_pt.x() - self._base_pt.x()
        dy = map_pt.y() - self._base_pt.y()
        for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
            moved = QgsGeometry(geom)
            moved.translate(dx, dy)
            band.setToGeometry(moved, layer)

    def _apply_move(self, dest_pt: QgsPointXY):
        if not self._base_pt:
            return
        crs_mgr = getattr(self._ctx, 'crs_manager', None)
        if crs_mgr:
            base_l = crs_mgr.project_to_layer(self._base_pt)
            dest_l = crs_mgr.project_to_layer(dest_pt)
            dx = dest_l.x() - base_l.x()
            dy = dest_l.y() - base_l.y()
        else:
            dx = dest_pt.x() - self._base_pt.x()
            dy = dest_pt.y() - self._base_pt.y()
        modified = set()
        for layer, fid, geom in self._sel_features:
            new_geom = QgsGeometry(geom)
            new_geom.translate(dx, dy)
            if not layer.isEditable():
                layer.startEditing()
            layer.changeGeometry(fid, new_geom)
            modified.add(layer)
        for lyr in modified:
            lyr.triggerRepaint()
        n = len(self._sel_features)
        self._log(f"  Moved {n} feature(s)  Δ({dx:.3f}, {dy:.3f})", "#88ff88")
        self._go_home()

    def _reset(self):
        self._clear_selection()
        self._state   = _ST_SELECT
        self._base_pt = None
        self._last_input_ref = None

    # ── typed input routing ───────────────────────────────────────────────

    def _dispatch(self, sem):
        if self._state == _ST_PLACE and self._base_pt:
            if sem.type == EventType.COORDINATE_ENTERED and sem.point:
                self._apply_move(sem.point)
            elif sem.type == EventType.VALUE_ENTERED and sem.value is not None:
                dest = QgsPointXY(self._base_pt.x() + float(sem.value),
                                  self._base_pt.y())
                self._apply_move(dest)

    # ── QgsMapTool interface ──────────────────────────────────────────────

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        self._log("MOVE  ──  select features to move", "#aaddff")

    def deactivate(self):
        self._clear_selection()
        self._hint.hide()
        self._state   = _ST_SELECT
        self._base_pt = None
        self._last_input_ref = None
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        if self._state == _ST_PLACE:
            self._update_preview(map_pt)
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        shift  = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if event.button() == Qt.MouseButton.RightButton:
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._hint.hide()
                    self._go_home()
            elif self._state == _ST_PLACE:
                self._apply_move(map_pt)
            else:
                self._reset()
                self._log("  Move cancelled", "#ffaaaa")
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self._state == _ST_SELECT:
            result = self._find_feature_near(map_pt)
            if result:
                layer, fid, geom = result
                if shift:
                    if self._remove_from_selection(layer, fid):
                        self._log(f"  Deselected  ({len(self._sel_features)} selected)")
                else:
                    if self._add_to_selection(layer, fid, geom):
                        self._log(f"  Selected '{layer.name()}' fid {fid}"
                                  f"  ({len(self._sel_features)} selected)")
                    else:
                        self._log(f"  Already selected  ({len(self._sel_features)} selected)")
            else:
                self._log("  No feature found near click")

        elif self._state == _ST_BASE:
            self._enter_place(map_pt)

        elif self._state == _ST_PLACE:
            self._apply_move(map_pt)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._state != _ST_SELECT:
                self._reset()
                self._log("  Move cancelled", "#ffaaaa")
            else:
                self._hint.hide()
                self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._state == _ST_SELECT:
                if self._sel_features:
                    self._enter_base()
                else:
                    self._go_home()
