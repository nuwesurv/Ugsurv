# -*- coding: utf-8 -*-
import os
import xml.etree.ElementTree as ET

from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt.QtGui import QPixmap, QPainter, QIcon
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem,
    QInputDialog, QMessageBox, QSizePolicy, QFrame,
)
from qgis.core import (
    QgsProject, QgsWkbTypes, QgsVectorLayer,
    QgsMapLayerProxyModel,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.utils import iface as _iface

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STYLES_DIR = os.path.join(_PLUGIN_DIR, 'styles')
_ICON_SIZE  = QSize(28, 28)

_GEOM_STR = {
    QgsWkbTypes.GeometryType.PointGeometry:   'Point',
    QgsWkbTypes.GeometryType.LineGeometry:    'LineString',
    QgsWkbTypes.GeometryType.PolygonGeometry: 'Polygon',
}

# QML <layerGeometryType>: 0=Point, 1=Line, 2=Polygon
_GEOM_INT = {
    QgsWkbTypes.GeometryType.PointGeometry:   0,
    QgsWkbTypes.GeometryType.LineGeometry:    1,
    QgsWkbTypes.GeometryType.PolygonGeometry: 2,
}


def _qml_geom_int(qml_path):
    """Return the layerGeometryType integer from a QML file (0/1/2), or None."""
    try:
        root = ET.parse(qml_path).getroot()
        el = root.find('layerGeometryType')
        if el is not None:
            return int(el.text)
    except Exception:
        pass
    return None


def _symbol_pixmap(sym, size):
    """Render a symbol the same way QGIS's layer styling panel does — fills the box."""
    pix = QPixmap(size)
    pix.fill(Qt.GlobalColor.white)
    p = QPainter(pix)
    sym.drawPreviewIcon(p, size)
    p.end()
    return pix


def _symbol_for_qml(qml_path, geom_str):
    """Return a cloned symbol from a QML file via a throw-away memory layer."""
    try:
        tmp = QgsVectorLayer(f'{geom_str}?crs=EPSG:4326', '_styler_tmp', 'memory')
        tmp.loadNamedStyle(qml_path)
        renderer = tmp.renderer()
        if renderer and renderer.type() == 'singleSymbol':
            return renderer.symbol().clone()
    except Exception:
        pass
    return None


class MapStylerDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Map Styler')
        self.setMinimumWidth(280)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)

        os.makedirs(_STYLES_DIR, exist_ok=True)

        root = QWidget()
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(6, 6, 6, 6)
        vbox.setSpacing(4)

        # ── Layer picker ──────────────────────────────────────────────────────
        vbox.addWidget(QLabel('Layer:'))
        self._layer_combo = QgsMapLayerComboBox()
        self._layer_combo.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        self._layer_combo.layerChanged.connect(self._on_layer_changed)
        vbox.addWidget(self._layer_combo)

        # ── Preview  +  Style list  (side by side) ───────────────────────────
        side = QHBoxLayout()
        side.setSpacing(6)

        self._preview = QLabel()
        self._preview.setFixedSize(120, 120)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet('background: #ffffff; border: 1px solid #c8c8c8;')
        side.addWidget(self._preview, 0)

        self._style_list = QListWidget()
        self._style_list.setIconSize(_ICON_SIZE)
        self._style_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._style_list.currentItemChanged.connect(self._on_style_highlighted)
        self._style_list.itemClicked.connect(self._on_style_clicked)
        side.addWidget(self._style_list, 1)

        vbox.addLayout(side)

        # ── Save / Delete ─────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        vbox.addWidget(sep)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._save_btn = QPushButton('Save style')
        self._del_btn  = QPushButton('Delete style')
        self._save_btn.clicked.connect(self._save_style)
        self._del_btn.clicked.connect(self._delete_style)
        btn_row.addWidget(self._save_btn)
        btn_row.addWidget(self._del_btn)
        vbox.addLayout(btn_row)

        # ── Status ────────────────────────────────────────────────────────────
        self._status = QLabel('Select a layer then pick a style')
        self._status.setWordWrap(True)
        self._status.setStyleSheet('color: #666; font-size: 11px;')
        vbox.addWidget(self._status)

        self.setWidget(root)

        # Sync with the layers panel active layer
        _iface.currentLayerChanged.connect(self._on_active_layer_changed)
        active = _iface.activeLayer() or _iface.mapCanvas().currentLayer()
        if active:
            self._layer_combo.setLayer(active)
        self._populate_styles()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _current_geom_str(self):
        layer = self._layer_combo.currentLayer()
        return _GEOM_STR.get(layer.geometryType(), 'Point') if layer else 'Point'

    def _update_preview(self, qml_path):
        sym = _symbol_for_qml(qml_path, self._current_geom_str())
        if sym:
            self._preview.setPixmap(_symbol_pixmap(sym, self._preview.size()))
            self._preview.setText('')
        else:
            self._preview.clear()
            self._preview.setText('—')

    # ── population ────────────────────────────────────────────────────────────

    def _populate_styles(self):
        self._style_list.clear()
        self._preview.clear()
        if not os.path.isdir(_STYLES_DIR):
            self._status.setText('styles/ folder not found')
            return

        layer       = self._layer_combo.currentLayer()
        geom_str    = self._current_geom_str()
        target_geom = _GEOM_INT.get(layer.geometryType()) if layer else None

        qmls  = sorted(f for f in os.listdir(_STYLES_DIR) if f.lower().endswith('.qml'))
        shown = 0
        for qml in qmls:
            path     = os.path.join(_STYLES_DIR, qml)
            qml_geom = _qml_geom_int(path)
            if target_geom is not None and qml_geom is not None and qml_geom != target_geom:
                continue
            name = os.path.splitext(qml)[0]
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            sym = _symbol_for_qml(path, geom_str)
            if sym:
                item.setIcon(QIcon(_symbol_pixmap(sym, _ICON_SIZE)))
            self._style_list.addItem(item)
            shown += 1

        if not qmls:
            self._status.setText('No .qml files in styles/ — drop them there')
        elif shown == 0:
            self._status.setText('No styles match the selected layer geometry type')
        else:
            self._status.setText('Select a layer then pick a style')

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_active_layer_changed(self, layer):
        if layer:
            self._layer_combo.setLayer(layer)

    def closeEvent(self, event):
        try:
            _iface.currentLayerChanged.disconnect(self._on_active_layer_changed)
        except Exception:
            pass
        super().closeEvent(event)

    def _on_layer_changed(self, _layer):
        self._populate_styles()

    def _on_style_highlighted(self, current, _prev):
        if current is None:
            self._preview.clear()
            return
        self._update_preview(current.data(Qt.ItemDataRole.UserRole))

    def _on_style_clicked(self, item):
        layer = self._layer_combo.currentLayer()
        if layer is None:
            self._status.setText('No layer selected')
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        _, ok = layer.loadNamedStyle(path)
        layer.triggerRepaint()
        self._status.setText(
            f'Applied "{item.text()}" to {layer.name()}' if ok
            else 'Style applied (verify result on map)'
        )

    # ── save / delete ─────────────────────────────────────────────────────────

    def _save_style(self):
        layer = self._layer_combo.currentLayer()
        if layer is None:
            self._status.setText('No layer selected')
            return
        name, ok = QInputDialog.getText(self, 'Save Style', 'Style name:', text=layer.name())
        if not ok or not name.strip():
            return
        path = os.path.join(_STYLES_DIR, name.strip() + '.qml')
        msg, success = layer.saveNamedStyle(path)
        if success:
            self._status.setText(f'Saved: {name}.qml')
            self._populate_styles()
        else:
            self._status.setText(f'Save failed: {msg}')

    def _delete_style(self):
        item = self._style_list.currentItem()
        if item is None:
            self._status.setText('Select a style to delete')
            return
        name = item.text()
        path = item.data(Qt.ItemDataRole.UserRole)
        reply = QMessageBox.question(
            self, 'Delete Style', f'Delete "{name}.qml" permanently?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.remove(path)
                self._status.setText(f'Deleted: {name}')
                self._populate_styles()
            except Exception as e:
                self._status.setText(f'Delete failed: {e}')
