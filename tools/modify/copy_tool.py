# -*- coding: utf-8 -*-
"""
AutoCAD-style COPY tool.

Workflow
────────
1. Click features / rasters to build a selection set.
   Shift+click removes an item.  Enter/RMB (with selection) → proceed.
2. Click base point.
3. Click destination(s) to create copies — stays active for multiple copies.
   Enter/RMB in this phase → exit.

Esc at any phase → cancel / exit.
"""

import contextlib
import math
import os

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes, QgsRasterLayer,
)

from ...core.events import EventType, SnapType
from ...core import style as _style
from ...core.circle_utils import is_circle, circle_params, set_circle_attrs_on_feature

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

_C_HIGHLIGHT = _style.RB_SOURCE
_C_HL_FILL   = _style.RB_SOURCE_FILL
_C_PREVIEW   = _style.RB_PREVIEW
_C_PREV_FILL = _style.RB_PREVIEW_FILL

_ST_SELECT = 0
_ST_BASE   = 1
_ST_PLACE  = 2

_HIT_PX = 10


class CopyTool(QgsMapTool):
    """Copy features and rasters — multi-select → base point → destination(s)."""

    inputModeChanged = pyqtSignal(str, str)
    promptChanged    = pyqtSignal(str)

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state        = _ST_SELECT
        self._sel_features = []
        self._sel_bands    = []
        self._prev_bands   = []
        self._sel_rasters  = []   # [QgsRasterLayer, ...]
        self._raster_bands = []   # parallel highlight bands
        self._raster_prevs = []   # parallel preview bands
        self._base_pt: QgsPointXY | None = None
        self._last_input_ref: QgsPointXY | None = None
        self._snap_marker = None

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

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

    def _find_raster_at(self, map_pt):
        """Return the topmost file-based QgsRasterLayer whose extent contains map_pt."""
        root = QgsProject.instance().layerTreeRoot()
        for lyr in root.layerOrder():
            if not isinstance(lyr, QgsRasterLayer) or not lyr.isValid():
                continue
            if lyr.providerType() != 'gdal':
                continue
            if lyr.extent().contains(map_pt):
                return lyr
        return None

    def _raster_extent_geom(self, lyr):
        ext = lyr.extent()
        pts = [
            QgsPointXY(ext.xMinimum(), ext.yMinimum()),
            QgsPointXY(ext.xMaximum(), ext.yMinimum()),
            QgsPointXY(ext.xMaximum(), ext.yMaximum()),
            QgsPointXY(ext.xMinimum(), ext.yMaximum()),
        ]
        return QgsGeometry.fromPolygonXY([pts])

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
            import contextlib
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(self._snap_marker)
            self._snap_marker = None

    # ── vector selection ──────────────────────────────────────────────────

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

    # ── raster selection ──────────────────────────────────────────────────

    def _add_raster_to_selection(self, lyr):
        if any(id(r) == id(lyr) for r in self._sel_rasters):
            return False
        self._sel_rasters.append(lyr)
        hl = self._make_band(QgsWkbTypes.PolygonGeometry, _C_HIGHLIGHT, _C_HL_FILL, width=2)
        hl.setToGeometry(self._raster_extent_geom(lyr), None)
        self._raster_bands.append(hl)
        prev = self._make_band(QgsWkbTypes.PolygonGeometry, _C_PREVIEW, _C_PREV_FILL, width=2)
        prev.setVisible(False)
        self._raster_prevs.append(prev)
        return True

    def _remove_raster_from_selection(self, lyr):
        ids = [id(r) for r in self._sel_rasters]
        if id(lyr) not in ids:
            return False
        idx = ids.index(id(lyr))
        self._sel_rasters.pop(idx)
        self._rm(self._raster_bands.pop(idx))
        self._rm(self._raster_prevs.pop(idx))
        return True

    def _clear_selection(self):
        for b in (self._sel_bands + self._prev_bands
                  + self._raster_bands + self._raster_prevs):
            self._rm(b)
        self._sel_features = []
        self._sel_bands    = []
        self._prev_bands   = []
        self._sel_rasters  = []
        self._raster_bands = []
        self._raster_prevs = []

    # ── state transitions ─────────────────────────────────────────────────

    def _sel_count(self):
        parts = []
        n_v = len(self._sel_features)
        n_r = len(self._sel_rasters)
        if n_v:
            parts.append(f"{n_v} feature(s)")
        if n_r:
            parts.append(f"{n_r} raster(s)")
        return " + ".join(parts) if parts else "0 items"

    def _has_selection(self):
        return bool(self._sel_features or self._sel_rasters)

    def _enter_base(self):
        for b in self._prev_bands + self._raster_prevs:
            b.setVisible(False)
        self._state = _ST_BASE
        self._log(f"  {self._sel_count()} selected  →  click base point", "#88ccff")
        self.promptChanged.emit("Click base point:")

    def _enter_place(self, base_pt: QgsPointXY):
        self._base_pt        = base_pt
        self._last_input_ref = base_pt
        self._state          = _ST_PLACE
        for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
            band.setToGeometry(geom, layer)
            band.setVisible(True)
        for lyr, band in zip(self._sel_rasters, self._raster_prevs):
            band.setToGeometry(self._raster_extent_geom(lyr), None)
            band.setVisible(True)
        self._log("  Click destination(s)  or  type \"@dx,dy\"", "#88ccff")
        self.promptChanged.emit('Click destination or type "@dx,dy":')

    def _update_preview(self, map_pt: QgsPointXY):
        if self._state == _ST_PLACE and self._base_pt:
            dx = map_pt.x() - self._base_pt.x()
            dy = map_pt.y() - self._base_pt.y()
            dist = math.hypot(dx, dy)
            for (layer, fid, geom), band in zip(self._sel_features, self._prev_bands):
                moved = QgsGeometry(geom)
                moved.translate(dx, dy)
                band.setToGeometry(moved, layer)
            for lyr, band in zip(self._sel_rasters, self._raster_prevs):
                ext = lyr.extent()
                pts = [
                    QgsPointXY(ext.xMinimum()+dx, ext.yMinimum()+dy),
                    QgsPointXY(ext.xMaximum()+dx, ext.yMinimum()+dy),
                    QgsPointXY(ext.xMaximum()+dx, ext.yMaximum()+dy),
                    QgsPointXY(ext.xMinimum()+dx, ext.yMaximum()+dy),
                ]
                band.setToGeometry(QgsGeometry.fromPolygonXY([pts]), None)
            self.promptChanged.emit(f"Copy distance <{dist:.3f}m>:")
            dyn = getattr(self._ctx, 'dyn_widget', None)
            if dyn:
                dyn.set_live_value(dist)

    def _apply_raster_copy(self, lyr, dx, dy):
        """Create a shifted copy of the raster file and add it to the project."""
        from osgeo import gdal
        src = lyr.source()
        base, ext = os.path.splitext(src)
        dest = base + '_copy' + ext
        i = 2
        while os.path.exists(dest):
            dest = base + f'_copy{i}' + ext
            i += 1
        ds = gdal.Open(src)
        if ds is None:
            self._log(f"  Cannot open '{lyr.name()}'", "#ff8844")
            return False
        gt = list(ds.GetGeoTransform())
        gt[0] += dx
        gt[3] += dy
        ds_new = gdal.Translate(dest, ds)
        if ds_new is None:
            self._log(f"  Failed to create copy of '{lyr.name()}'", "#ff8844")
            ds = None
            return False
        ds_new.SetGeoTransform(gt)
        ds_new.SetProjection(ds.GetProjection())
        ds_new = None
        ds     = None
        copy_name = os.path.splitext(os.path.basename(dest))[0]
        new_lyr = QgsRasterLayer(dest, copy_name)
        if new_lyr.isValid():
            QgsProject.instance().addMapLayer(new_lyr)
        return True

    def _apply_copy(self, dest_pt: QgsPointXY):
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
            src_feat = layer.getFeature(fid)
            new_geom = QgsGeometry(geom)
            new_geom.translate(dx, dy)
            new_feat = QgsFeature(layer.fields())
            new_feat.setAttributes(src_feat.attributes())
            new_feat.setGeometry(new_geom)
            if is_circle(geom):
                new_center, new_radius = circle_params(new_geom)
                set_circle_attrs_on_feature(new_feat, new_center, new_radius)
            if not layer.isEditable():
                layer.startEditing()
            layer.addFeature(new_feat)
            modified.add(layer)
        for lyr in modified:
            lyr.triggerRepaint()
        n_r = sum(1 for lyr in self._sel_rasters
                  if self._apply_raster_copy(lyr, dx, dy))
        parts = []
        if self._sel_features:
            parts.append(f"{len(self._sel_features)} feature(s)")
        if n_r:
            parts.append(f"{n_r} raster(s)")
        self._log(f"  Copied {' + '.join(parts)}  Δ({dx:.3f}, {dy:.3f})", "#88ff88")

    def _reset(self):
        self._clear_selection()
        self._state          = _ST_SELECT
        self._base_pt        = None
        self._last_input_ref = None

    def _dispatch(self, sem):
        if self._state == _ST_PLACE and self._base_pt:
            if sem.type == EventType.COORDINATE_ENTERED and sem.point:
                self._apply_copy(sem.point)
            elif sem.type == EventType.VALUE_ENTERED and sem.value is not None:
                dest = QgsPointXY(self._base_pt.x() + float(sem.value),
                                  self._base_pt.y())
                self._apply_copy(dest)

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        preselected = getattr(self._ctx, 'selected_raster', None)
        if preselected is not None and preselected.isValid():
            self._add_raster_to_selection(preselected)
            self._ctx.selected_raster = None
        sel = getattr(self._ctx, 'selection_model', None)
        if sel and not sel.is_empty():
            for lid, fid in sel:
                layer = QgsProject.instance().mapLayer(lid)
                if not isinstance(layer, QgsVectorLayer):
                    continue
                feat = layer.getFeature(fid)
                if feat.isValid() and not feat.geometry().isEmpty():
                    self._add_to_selection(layer, fid, feat.geometry())
        if self._has_selection():
            self._state = _ST_BASE
            self._log(f"COPY  ──  {self._sel_count()} selected  →  click base point", "#aaddff")
            self._last_input_mode   = "value"
            self._last_input_prompt = "Click base point:"
            self.inputModeChanged.emit("value", "Click base point:")
        else:
            self._log("COPY  ──  select features / rasters to copy", "#aaddff")
            self._last_input_mode   = "value"
            self._last_input_prompt = "Select features to copy:"
            self.inputModeChanged.emit("value", "Select features to copy:")

    def deactivate(self):
        self._clear_selection()
        self._clear_snap_marker()
        self.inputModeChanged.emit("", "")
        self._state          = _ST_SELECT
        self._base_pt        = None
        self._last_input_ref = None
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self._snapped(self.toMapCoordinates(event.pos()))
        self._update_preview(map_pt)

    def canvasPressEvent(self, event):
        map_pt = self._snapped(self.toMapCoordinates(event.pos()))
        shift  = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if event.button() == Qt.MouseButton.RightButton:
            if self._state == _ST_SELECT:
                if self._has_selection():
                    self._enter_base()
                else:
                    self._go_home()
            elif self._state == _ST_PLACE:
                self._clear_selection()
                self._go_home()
            else:
                self._reset()
                self._log("  Copy cancelled", "#ffaaaa")
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self._state == _ST_SELECT:
            result = self._find_feature_near(map_pt)
            if result:
                layer, fid, geom = result
                if shift:
                    if self._remove_from_selection(layer, fid):
                        self._log(f"  Deselected  ({self._sel_count()} selected)")
                else:
                    if self._add_to_selection(layer, fid, geom):
                        self._log(f"  Selected '{layer.name()}' fid {fid}"
                                  f"  ({self._sel_count()} selected)")
                    else:
                        self._log(f"  Already selected")
            else:
                raster = self._find_raster_at(map_pt)
                if raster:
                    if shift:
                        if self._remove_raster_from_selection(raster):
                            self._log(f"  Raster deselected  ({self._sel_count()} selected)")
                    else:
                        if self._add_raster_to_selection(raster):
                            self._log(f"  Raster '{raster.name()}' selected"
                                      f"  ({self._sel_count()} selected)")
                        else:
                            self._log(f"  Already selected")
                else:
                    self._log("  No feature or raster found near click")

        elif self._state == _ST_BASE:
            self._enter_place(map_pt)

        elif self._state == _ST_PLACE:
            self._apply_copy(map_pt)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._state != _ST_SELECT:
                self._log("  Copy cancelled", "#ffaaaa")
            self._reset()
            self._go_home()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._state == _ST_SELECT:
                if self._has_selection():
                    self._enter_base()
                else:
                    self._go_home()
            elif self._state == _ST_PLACE:
                self._clear_selection()
                self._go_home()
