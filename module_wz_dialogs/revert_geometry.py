# -*- coding: utf-8 -*-
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel, QgsGeometry, QgsVectorLayer


class RevertGeometryDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Revert Geometry')

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        row = QHBoxLayout()
        lbl = QLabel('Layer:')
        lbl.setFixedWidth(60)
        self.cmb_layer = QgsMapLayerComboBox()
        self.cmb_layer.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        row.addWidget(lbl)
        row.addWidget(self.cmb_layer)
        layout.addLayout(row)

        self.btn_revert = QPushButton('Revert selected')
        layout.addWidget(self.btn_revert)

        self.status = QLabel('Select features, then click Revert.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        layout.addStretch()
        self.setWidget(root)

        self.btn_revert.clicked.connect(self._revert)

    def _set_status(self, msg, color='green'):
        self.status.setStyleSheet(f'color: {color};')
        self.status.setText(msg)

    def _revert(self):
        layer = self.cmb_layer.currentLayer()
        if not layer or not isinstance(layer, QgsVectorLayer):
            self._set_status('No valid vector layer selected.', 'red')
            return

        fields = layer.fields()
        if fields.indexOf('original_geometry') == -1:
            self._set_status("Layer has no 'original_geometry' column.", 'red')
            return
        has_is_new = fields.indexOf('is_new') != -1

        selected = layer.selectedFeatures()
        if not selected:
            self._set_status('No features selected.', 'red')
            return

        # Collect the original_geometry WKT values from every selected feature.
        # We work at the family level — all pieces sharing the same WKT belong
        # to one split group and are handled together.
        target_wkts = set()
        for feat in selected:
            wkt = feat.attribute('original_geometry')
            if wkt:
                target_wkts.add(wkt)

        if not target_wkts:
            self._set_status('Selected features have no original_geometry to revert.', 'red')
            return

        # Walk every feature in the layer and classify into families.
        # families: wkt -> {'parent': fid, 'children': [fid, ...]}
        families = {wkt: {'parent': None, 'children': []} for wkt in target_wkts}

        for feat in layer.getFeatures():
            wkt = feat.attribute('original_geometry')
            if wkt not in families:
                continue
            fid = feat.id()
            is_new = feat.attribute('is_new') if has_is_new else False
            if is_new:
                families[wkt]['children'].append(fid)
            else:
                families[wkt]['parent'] = fid

        layer.startEditing()
        reverted = 0
        deleted = 0

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

        # layer.commitChanges()
        self._set_status(
            f'Reverted {reverted} feature(s), removed {deleted} split piece(s).', 'green'
        )
