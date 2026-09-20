# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)
from qgis.core import (
    QgsGeometry,
    QgsFeature,
    QgsMapLayerProxyModel,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox


class MergeLinesDock(QDockWidget):
    """
    Dock that merges the geometries of all selected line features on the
    chosen layer into a single new feature.  The original features are
    deleted and replaced by one feature whose geometry is the union of
    all selected geometries.  Attributes are copied from the first
    selected feature.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Merge Lines')
        self._connected_layer = None
        self._merging = False

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        H = 26

        lyr_row = QHBoxLayout()
        lbl = QLabel('Layer:')
        lbl.setFixedWidth(60)
        self._cmb_layer = QgsMapLayerComboBox()
        self._cmb_layer.setFilters(QgsMapLayerProxyModel.Filter.LineLayer)
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
        self._merge_btn = QPushButton('Merge Selected')
        self._merge_btn.setFixedHeight(H + 4)
        self._merge_btn.clicked.connect(self._merge_selected)
        btn_row.addWidget(self._merge_btn)
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
        if self._merging:
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
        elif n == 1:
            self._status.setStyleSheet('color: gray;')
            self._status.setText('Select at least 2 line features to merge.')
        else:
            self._status.setStyleSheet('color: #2288cc;')
            self._status.setText(
                f'{n} feature(s) selected — click Merge to combine into one.'
            )

    def _set_status(self, msg, color='green'):
        self._status.setStyleSheet(f'color: {color};')
        self._status.setText(msg)

    # ------------------------------------------------------------------ #

    def _merge_selected(self):
        layer = self._cmb_layer.currentLayer()
        if layer is None:
            self._set_status('No layer selected.', 'red')
            return

        if not QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.GeometryType.LineGeometry:
            self._set_status('Selected layer is not a line layer.', 'red')
            return

        selected = list(layer.selectedFeatures())
        if len(selected) < 2:
            self._set_status('Select at least 2 features to merge.', 'red')
            return

        geoms = [f.geometry() for f in selected]
        merged = QgsGeometry.unaryUnion(geoms)
        if merged is None or merged.isNull():
            self._set_status('Merge failed: could not compute union.', 'red')
            return

        merged = merged.mergeLines()
        if merged is None or merged.isNull():
            self._set_status('Merge failed: result is empty.', 'red')
            return

        source_fids = [f.id() for f in selected]
        new_feat = QgsFeature(selected[0])
        new_feat.setGeometry(merged)

        self._merging = True
        try:
            layer.startEditing()
            layer.beginEditCommand('Merge lines')

            if not layer.addFeature(new_feat):
                layer.destroyEditCommand()
                self._set_status('Merge failed: could not add new feature.', 'red')
                return

            if not layer.deleteFeatures(source_fids):
                layer.destroyEditCommand()
                self._set_status('Merge failed: could not delete source features.', 'red')
                return

            layer.endEditCommand()
            layer.triggerRepaint()
        finally:
            self._merging = False

        geom_type = QgsWkbTypes.displayString(merged.wkbType())
        self._set_status(
            f'Merged {len(selected)} features into 1 ({geom_type}).'
            '  [NOT committed — Save Layer to keep changes]',
            'green',
        )
