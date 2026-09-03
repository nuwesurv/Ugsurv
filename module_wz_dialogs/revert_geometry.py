# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateTransform,
)
from qgis.gui import QgsMapToolIdentifyFeature, QgsRubberBand
from ..core import style as _style


class RevertMapTool(QgsMapToolIdentifyFeature):
    """
    Map tool that reverts selected polygon features back to their
    original_geometry WKT without committing the edit session.

    Usage
    -----
    - Left-click  : pick a feature from the active layer (accumulates).
    - Right-click : execute revert on all accumulated features, then reset.
    - Esc / Enter : cancel and deactivate.
    """

    def __init__(self, canvas, iface, cmd_dock):
        super().__init__(canvas)
        self.iface     = iface
        self.canvas    = canvas
        self._cmd_dock = cmd_dock   # CommandLineWidget
        self._maptool  = None

        self._active_layer  = None
        self._selected_fids = []    # ordered list of fids for _execute_revert
        self._picked_fids   = set() # set of fids for O(1) duplicate check

        self._rubber_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self._rubber_band.setColor(_style.RB_IDENTIFY_RED)
        self._rubber_band.setWidth(_style.RB_IDENTIFY_WIDTH)
        self._rubber_band.setLineStyle(_style.RB_LINE_STYLE)
        self._rubber_band.setFillColor(_style.RB_IDENTIFY_RED_FILL)

    # ------------------------------------------------------------------ #
    #  Tool lifecycle                                                      #
    # ------------------------------------------------------------------ #

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._log('REVERT active — left-click features to accumulate, right-click to confirm.')

    def deactivate(self):
        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            try:
                self._cmd_dock._input.setFocus()
            except Exception:
                pass
        self._clear_state()
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return,
                           Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.deactivate()

    # ------------------------------------------------------------------ #
    #  Canvas events                                                       #
    # ------------------------------------------------------------------ #

    def canvasPressEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.RightButton:
                self._execute_revert()
                self._cmd_dock.log('─' * 40, '#555555')
                return

            if event.button() == Qt.MouseButton.LeftButton:
                active_layer = self.iface.activeLayer()
                if not isinstance(active_layer, QgsVectorLayer):
                    self._log('No active vector layer in the Layers panel.')
                    return

                if self._active_layer and self._active_layer != active_layer:
                    self._clear_state()

                results = self.identify(
                    event.x(), event.y(),
                    [active_layer],
                    QgsMapToolIdentifyFeature.IdentifyMode.TopDownAll,
                )
                if not results:
                    self._log('No feature found at click position.')
                    return

                feature    = results[0].mFeature
                feat_layer = results[0].mLayer
                fid        = feature.id()

                if fid in self._picked_fids:
                    return

                self._active_layer = active_layer
                self._selected_fids.append(fid)
                self._picked_fids.add(fid)

                geom        = QgsGeometry(feature.geometry())
                project_crs = QgsProject.instance().crs()
                feat_crs    = feat_layer.crs()
                if feat_crs != project_crs:
                    xform = QgsCoordinateTransform(
                        feat_crs, project_crs, QgsProject.instance()
                    )
                    geom.transform(xform)
                self._rubber_band.addGeometry(geom, None)
                self._rubber_band.show()

                self._log(
                    f'Selected feature {fid} on "{active_layer.name()}"  '
                    f'({len(self._selected_fids)} total) — right-click to revert.'
                )

        except Exception as e:
            self._log(f'Error: {e}')

    # ------------------------------------------------------------------ #
    #  Revert algorithm                                                    #
    # ------------------------------------------------------------------ #

    def _execute_revert(self):
        layer = self._active_layer
        if not isinstance(layer, QgsVectorLayer) or not self._selected_fids:
            self._log('Nothing to revert — select at least one feature first.')
            return

        fields = layer.fields()
        if fields.indexOf('original_geometry') == -1:
            self._log("Layer has no 'original_geometry' column.")
            return
        has_is_new = fields.indexOf('is_new') != -1

        selected_features = [layer.getFeature(fid) for fid in self._selected_fids]

        target_wkts = set()
        for feat in selected_features:
            wkt = feat.attribute('original_geometry')
            if wkt:
                target_wkts.add(wkt)

        if not target_wkts:
            self._log('Selected features have no original_geometry to revert.')
            self._clear_state()
            return

        families = {wkt: {'parent': None, 'children': []} for wkt in target_wkts}
        for feat in layer.getFeatures():
            wkt = feat.attribute('original_geometry')
            if wkt not in families:
                continue
            fid    = feat.id()
            is_new = feat.attribute('is_new') if has_is_new else False
            if is_new:
                families[wkt]['children'].append(fid)
            else:
                families[wkt]['parent'] = fid

        layer.startEditing()
        layer.beginEditCommand('Revert geometry')

        reverted = 0
        deleted  = 0
        for wkt, family in families.items():
            parent_fid = family['parent']
            child_fids = family['children']

            if parent_fid is not None:
                geom = QgsGeometry.fromWkt(wkt)
                if not geom.isNull():
                    layer.changeGeometry(parent_fid, geom)
                    reverted += 1

            if child_fids:
                layer.deleteFeatures(child_fids)
                deleted += len(child_fids)

        layer.endEditCommand()
        layer.triggerRepaint()

        self._log(
            f'Reverted {reverted} feature(s), removed {deleted} split piece(s). '
            '[NOT committed — Save Layer to keep changes]'
        )

        self._clear_state(keep_layer=True)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _clear_state(self, keep_layer=False):
        self._rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self._selected_fids.clear()
        self._picked_fids.clear()
        if not keep_layer:
            self._active_layer = None

    def _log(self, msg):
        self._cmd_dock.log(msg, '#aaddff')
