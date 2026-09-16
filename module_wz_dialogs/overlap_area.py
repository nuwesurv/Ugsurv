# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.core import (
    QgsProject,
    QgsGeometry,
    QgsCoordinateTransform,
    QgsWkbTypes,
    QgsField,
)
from qgis.gui import QgsMapToolIdentifyFeature, QgsRubberBand
from ..core import style as _style

_COL_PCT  = 'overlap_pct'
_COL_AREA = 'overlap_area'


class CalcOverlapAreaTool(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, iface, cmd_dock):
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self._cmd_dock = cmd_dock
        self._picked_fids = set()

        self._target_feat = None
        self._target_layer = None
        self._target_geom = None   # in project CRS
        self._comp_geoms = []

        self.rubber_band1 = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band1.setColor(_style.RB_IDENTIFY_RED)
        self.rubber_band1.setWidth(_style.RB_IDENTIFY_WIDTH)
        self.rubber_band1.setLineStyle(_style.RB_LINE_STYLE)
        self.rubber_band1.setFillColor(_style.RB_IDENTIFY_RED_FILL)

        self.rubber_band2 = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band2.setColor(_style.RB_IDENTIFY_BLUE)
        self.rubber_band2.setWidth(_style.RB_IDENTIFY_WIDTH)
        self.rubber_band2.setLineStyle(_style.RB_LINE_STYLE)
        self.rubber_band2.setFillColor(_style.RB_IDENTIFY_BLUE_FILL)

    def _log(self, msg, color='#aaddff'):
        self._cmd_dock.log(msg, color)

    def _show_polygon(self, geom, rubber_band):
        rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        rubber_band.addGeometry(geom, None)
        rubber_band.show()

    def _clear_state(self):
        self.rubber_band1.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band2.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self._picked_fids.clear()
        self._target_feat = None
        self._target_layer = None
        self._target_geom = None
        self._comp_geoms.clear()

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._log('Click target feature (red), then comparison features (blue). Right-click to calculate.')

    def deactivate(self):
        self._clear_state()
        try:
            self._cmd_dock._input.setFocus()
        except Exception:
            pass
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.deactivate()

    def canvasMoveEvent(self, event):
        pass

    def canvasPressEvent(self, event):  # noqa: C901
        try:
            if event.button() == Qt.MouseButton.RightButton:
                self._calculate()
                self._clear_state()
                self._cmd_dock.log('─' * 40, '#555555')
                return

            if event.button() != Qt.MouseButton.LeftButton:
                return

            active_layer = self.iface.activeLayer()
            if not active_layer:
                self._log('No active layer selected in the Layers panel.')
                return

            results = self.identify(
                event.x(), event.y(),
                [active_layer],
                QgsMapToolIdentifyFeature.IdentifyMode.TopDownAll,
            )
            if not results:
                self._log('No feature detected.')
                return

            feature   = results[0].mFeature
            feat_layer = results[0].mLayer

            fid_key = (feat_layer.id(), feature.id())
            if fid_key in self._picked_fids:
                return
            self._picked_fids.add(fid_key)

            geom = QgsGeometry(feature.geometry())
            project_crs = QgsProject.instance().crs()
            feat_crs = feat_layer.crs()
            if feat_crs != project_crs:
                xform = QgsCoordinateTransform(feat_crs, project_crs, QgsProject.instance())
                geom.transform(xform)

            if self._target_geom is None:
                self._target_feat  = feature
                self._target_layer = feat_layer
                self._target_geom  = geom
                self._show_polygon(geom, self.rubber_band1)
                self._log(f'Target: {feat_layer.name()} fid={feature.id()}')
            else:
                self._comp_geoms.append(geom)
                merged = QgsGeometry.unaryUnion(self._comp_geoms)
                self._show_polygon(merged, self.rubber_band2)
                self._log(f'Comparison #{len(self._comp_geoms)}: {feat_layer.name()} fid={feature.id()}')

        except Exception as e:
            self._log(f'Error: {e}')

    def _calculate(self):
        if self._target_geom is None:
            self._log('No target feature selected.')
            return
        if not self._comp_geoms:
            self._log('No comparison features selected.')
            return

        try:
            from shapely.wkt import loads as wkt_loads
            from shapely.ops import unary_union

            target_sh  = wkt_loads(self._target_geom.asWkt())
            comp_sh    = [wkt_loads(g.asWkt()) for g in self._comp_geoms]
            comp_union = unary_union(comp_sh) if len(comp_sh) > 1 else comp_sh[0]

            target_area = target_sh.area
            if target_area <= 0:
                self._log('Target feature has zero area.')
                return

            intersection = target_sh.intersection(comp_union)
            ovl_area = intersection.area if not intersection.is_empty else 0.0
            ovl_pct  = min(100.0, 100.0 * ovl_area / target_area)
            ovl_area = round(ovl_area, 4)
            ovl_pct  = round(ovl_pct,  4)

        except Exception as e:
            self._log(f'Calculation error: {e}')
            return

        layer = self._target_layer
        fid   = self._target_feat.id()

        provider = layer.dataProvider()
        for col_name in (_COL_PCT, _COL_AREA):
            if provider.fields().indexOf(col_name) == -1:
                provider.addAttributes([QgsField(col_name, QVariant.Double, 'double', 14, 4)])
                layer.updateFields()

        idx_pct  = layer.fields().indexOf(_COL_PCT)
        idx_area = layer.fields().indexOf(_COL_AREA)

        if idx_pct == -1 or idx_area == -1:
            self._log('Could not create output fields.')
            return

        provider.changeAttributeValues({fid: {idx_pct: ovl_pct, idx_area: ovl_area}})
        layer.updateFields()
        layer.triggerRepaint()

        self._log(
            f'Done. overlap_pct={ovl_pct}%  overlap_area={ovl_area} sq units'
            f'  → written to {layer.name()} fid={fid}',
            '#44ff88',
        )


class CalcOverlapAreaTool2(CalcOverlapAreaTool):
    """Same as CalcOverlapAreaTool but uses the target feature's original_geometry
    field (WKT) instead of its current geometry for the overlap calculation."""

    _ORIG_FIELD = 'original_geometry'

    def activate(self):
        super().activate()
        self._log(f'COA2: target uses {self._ORIG_FIELD} field. Click target (red), comparison features (blue), right-click to calculate.')

    def canvasPressEvent(self, event):  # noqa: C901
        try:
            if event.button() == Qt.MouseButton.RightButton:
                self._calculate()
                self._clear_state()
                self._cmd_dock.log('─' * 40, '#555555')
                return

            if event.button() != Qt.MouseButton.LeftButton:
                return

            active_layer = self.iface.activeLayer()
            if not active_layer:
                self._log('No active layer selected in the Layers panel.')
                return

            results = self.identify(
                event.x(), event.y(),
                [active_layer],
                QgsMapToolIdentifyFeature.IdentifyMode.TopDownAll,
            )
            if not results:
                self._log('No feature detected.')
                return

            feature    = results[0].mFeature
            feat_layer = results[0].mLayer

            fid_key = (feat_layer.id(), feature.id())
            if fid_key in self._picked_fids:
                return
            self._picked_fids.add(fid_key)

            project_crs = QgsProject.instance().crs()
            feat_crs    = feat_layer.crs()

            if self._target_geom is None:
                # Target: read original_geometry field
                orig_wkt = feature[self._ORIG_FIELD]
                if not orig_wkt:
                    self._picked_fids.discard(fid_key)
                    self._log(f'Target feature has no value in "{self._ORIG_FIELD}" field.')
                    return
                geom = QgsGeometry.fromWkt(str(orig_wkt))
                if geom is None or geom.isEmpty():
                    self._picked_fids.discard(fid_key)
                    self._log(f'"{self._ORIG_FIELD}" field contains invalid WKT.')
                    return
                if feat_crs != project_crs:
                    xform = QgsCoordinateTransform(feat_crs, project_crs, QgsProject.instance())
                    geom.transform(xform)
                self._target_feat  = feature
                self._target_layer = feat_layer
                self._target_geom  = geom
                self._show_polygon(geom, self.rubber_band1)
                self._log(f'Target (original_geometry): {feat_layer.name()} fid={feature.id()}')
            else:
                # Comparison: use current geometry
                geom = QgsGeometry(feature.geometry())
                if feat_crs != project_crs:
                    xform = QgsCoordinateTransform(feat_crs, project_crs, QgsProject.instance())
                    geom.transform(xform)
                self._comp_geoms.append(geom)
                merged = QgsGeometry.unaryUnion(self._comp_geoms)
                self._show_polygon(merged, self.rubber_band2)
                self._log(f'Comparison #{len(self._comp_geoms)}: {feat_layer.name()} fid={feature.id()}')

        except Exception as e:
            self._log(f'Error: {e}')
