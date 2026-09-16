# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateTransform,
    QgsMapLayerProxyModel,
)
from qgis.gui import QgsMapToolIdentifyFeature, QgsRubberBand, QgsMapLayerComboBox
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
            except Exception:  # nosec B110
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

    def _execute_revert(self):  # noqa: C901
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


class RevertGeometryDock(QDockWidget):
    """
    Dock that reverts QGIS-selected features on the chosen layer back to their
    original_geometry WKT.  Also deletes any is_new=True sibling features that
    share the same original_geometry value (split pieces).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Revert Geometry')
        self._connected_layer = None
        self._reverting = False

        root   = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        H = 26

        lyr_row = QHBoxLayout()
        lbl = QLabel('Layer:')
        lbl.setFixedWidth(60)
        self._cmb_layer = QgsMapLayerComboBox()
        self._cmb_layer.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self._cmb_layer.setFixedHeight(H)
        lyr_row.addWidget(lbl)
        lyr_row.addWidget(self._cmb_layer)
        layout.addLayout(lyr_row)

        self._status = QLabel('No layer selected.')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._revert_btn = QPushButton('Revert Selected')
        self._revert_btn.setFixedHeight(H + 4)
        self._revert_btn.clicked.connect(self._revert_selected)
        btn_row.addWidget(self._revert_btn)
        layout.addLayout(btn_row)

        self.setWidget(root)

        self._cmb_layer.layerChanged.connect(self._on_layer_changed)
        self._on_layer_changed(self._cmb_layer.currentLayer())

    # ------------------------------------------------------------------ #

    def _on_layer_changed(self, layer):
        if self._connected_layer is not None:
            try:
                self._connected_layer.selectionChanged.disconnect(self._update_status)
            except Exception:
                pass
        self._connected_layer = layer
        if layer is not None:
            layer.selectionChanged.connect(self._update_status)
        self._update_status()

    def _update_status(self):
        if self._reverting:
            return
        layer = self._cmb_layer.currentLayer()
        if layer is None:
            self._status.setStyleSheet('')
            self._status.setText('No layer selected.')
            return
        n = layer.selectedFeatureCount()
        if n == 0:
            self._status.setStyleSheet('color: gray;')
            self._status.setText('No features selected on this layer.')
        else:
            self._status.setStyleSheet('color: #2288cc;')
            self._status.setText(
                f'{n} feature(s) selected — click Revert to restore original geometry.'
            )

    def _set_status(self, msg, color='green'):
        self._status.setStyleSheet(f'color: {color};')
        self._status.setText(msg)

    # ------------------------------------------------------------------ #

    def _revert_selected(self):  # noqa: C901
        layer = self._cmb_layer.currentLayer()
        if layer is None:
            self._set_status('No layer selected.', 'red')
            return

        fields = layer.fields()
        if fields.indexOf('original_geometry') == -1:
            self._set_status("Layer has no 'original_geometry' column.", 'red')
            return

        selected_features = list(layer.selectedFeatures())
        if not selected_features:
            self._set_status('No features selected on this layer.', 'red')
            return

        has_is_new = fields.indexOf('is_new') != -1

        target_wkts = set()
        for feat in selected_features:
            wkt = feat.attribute('original_geometry')
            if wkt:
                target_wkts.add(wkt)

        if not target_wkts:
            self._set_status('Selected features have no original_geometry to revert.', 'red')
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

        self._reverting = True
        try:
            layer.startEditing()
            layer.beginEditCommand('Revert geometry')

            reverted       = 0
            deleted        = 0
            already_same   = 0
            for wkt, family in families.items():
                parent_fid = family['parent']
                child_fids = family['children']

                if parent_fid is not None:
                    original_geom = QgsGeometry.fromWkt(wkt)
                    if original_geom.isNull():
                        continue
                    if original_geom.equals(layer.getFeature(parent_fid).geometry()):
                        already_same += 1
                        continue
                    layer.changeGeometry(parent_fid, original_geom)
                    reverted += 1

                if child_fids:
                    layer.deleteFeatures(child_fids)
                    deleted += len(child_fids)

            layer.endEditCommand()
            layer.triggerRepaint()
        finally:
            self._reverting = False

        if reverted == 0 and deleted == 0:
            self._set_status(
                f'All {already_same} selected feature(s) already have their original geometry.',
                '#888888',
            )
            return

        msg = f'Reverted {reverted} feature(s)'
        if deleted:
            msg += f', removed {deleted} split piece(s)'
        if already_same:
            msg += f' ({already_same} already at original geometry)'
        msg += '.  [NOT committed — Save Layer to keep changes]'
        self._set_status(msg, 'green')
