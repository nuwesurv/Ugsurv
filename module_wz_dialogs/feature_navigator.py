# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton,
    QLineEdit, QCheckBox, QFrame, QSizePolicy,
)

from qgis.gui import QgsMapLayerComboBox, QgsExpressionBuilderDialog
from qgis.core import (
    QgsFeatureRequest,
    QgsMapLayerProxyModel,
    QgsVectorLayer,
)


class FeatureNavigatorDock(QDockWidget):
    """
    Navigate through filtered features one by one using ◀ / ▶ buttons (or
    the Left / Right arrow keys while the dock is focused).

    Workflow
    --------
    1. Pick a vector layer.
    2. Type (or build) a filter expression.
    3. Click **Apply Filter** — matching features are loaded.
    4. Step through them with ◀ / ▶; the canvas zooms to each feature and
       an orange rubber band highlights it.
    """

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.setWindowTitle('Feature Navigator')
        self.setMinimumWidth(310)
        self.setFocusPolicy(Qt.ClickFocus)

        self._fids: list = []      # ordered list of feature IDs that match the filter
        self._index: int = -1      # current position in _fids

        self._build_ui()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QWidget()
        root.setFocusPolicy(Qt.ClickFocus)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(6)
        vbox.setContentsMargins(8, 8, 8, 8)

        # ── Layer ───────────────────────────────────────────────────────
        layer_row = QHBoxLayout()
        layer_row.addWidget(QLabel('Layer:'))
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        layer_row.addWidget(self.layer_combo)
        vbox.addLayout(layer_row)

        # ── Expression ──────────────────────────────────────────────────
        expr_row = QHBoxLayout()
        self.expr_edit = QLineEdit()
        self.expr_edit.setPlaceholderText('Filter expression  (empty = all features)')
        self.expr_edit.returnPressed.connect(self._apply_filter)
        self.expr_btn = QPushButton('…')
        self.expr_btn.setFixedWidth(30)
        self.expr_btn.setToolTip('Open expression builder')
        self.expr_btn.clicked.connect(self._open_expr_builder)
        expr_row.addWidget(self.expr_edit)
        expr_row.addWidget(self.expr_btn)
        vbox.addLayout(expr_row)

        self.apply_btn = QPushButton('Apply Filter')
        self.apply_btn.clicked.connect(self._apply_filter)
        vbox.addWidget(self.apply_btn)

        # ── Separator ───────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep)

        # ── Navigation ──────────────────────────────────────────────────
        nav_row = QHBoxLayout()

        self.prev_btn = QPushButton('◀  Prev')
        self.prev_btn.setEnabled(False)
        self.prev_btn.setMinimumWidth(80)
        self.prev_btn.clicked.connect(self._go_prev)

        self.counter_lbl = QLabel('—')
        self.counter_lbl.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setBold(True)
        self.counter_lbl.setFont(font)
        self.counter_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.next_btn = QPushButton('Next  ▶')
        self.next_btn.setEnabled(False)
        self.next_btn.setMinimumWidth(80)
        self.next_btn.clicked.connect(self._go_next)

        nav_row.addWidget(self.prev_btn)
        nav_row.addWidget(self.counter_lbl)
        nav_row.addWidget(self.next_btn)
        vbox.addLayout(nav_row)

        # Hint label
        hint = QLabel('Tip: Left / Right arrow keys also navigate')
        hint.setStyleSheet('color: gray; font-size: 10px;')
        hint.setAlignment(Qt.AlignCenter)
        vbox.addWidget(hint)

        # ── Options ─────────────────────────────────────────────────────
        self.zoom_chk = QCheckBox('Zoom to feature')
        self.zoom_chk.setChecked(True)
        vbox.addWidget(self.zoom_chk)

        self.select_chk = QCheckBox('Select feature on layer')
        self.select_chk.setChecked(True)
        vbox.addWidget(self.select_chk)

        self.setWidget(root)

    # ------------------------------------------------------------------ #
    #  Keyboard navigation                                                 #
    # ------------------------------------------------------------------ #

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self._go_prev()
        elif event.key() == Qt.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    #  Slots                                                               #
    # ------------------------------------------------------------------ #

    def _on_layer_changed(self, _layer):
        """Reset state when the user picks a different layer."""
        self._fids = []
        self._index = -1
        self.counter_lbl.setText('—')
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)

    def _open_expr_builder(self):
        layer = self.layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer):
            return
        dlg = QgsExpressionBuilderDialog(layer, self.expr_edit.text(), self)
        if dlg.exec():
            self.expr_edit.setText(dlg.expressionText().strip())

    def _apply_filter(self):
        layer = self.layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer):
            self.counter_lbl.setText('Select a vector layer')
            return

        expr = self.expr_edit.text().strip()
        req = QgsFeatureRequest()
        if expr:
            req.setFilterExpression(expr)

        self._fids = [f.id() for f in layer.getFeatures(req)]

        if not self._fids:
            self.counter_lbl.setText('0 features found')
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return

        self._index = 0
        self._show_current()

    # ------------------------------------------------------------------ #
    #  Navigation helpers                                                  #
    # ------------------------------------------------------------------ #

    def _go_prev(self):
        if self._fids and self._index > 0:
            self._index -= 1
            self._show_current()

    def _go_next(self):
        if self._fids and self._index < len(self._fids) - 1:
            self._index += 1
            self._show_current()

    def _update_nav_buttons(self):
        total = len(self._fids)
        self.prev_btn.setEnabled(self._index > 0)
        self.next_btn.setEnabled(self._index < total - 1)
        if total:
            self.counter_lbl.setText(f'Feature {self._index + 1} of {total}')
        else:
            self.counter_lbl.setText('No features')

    def _show_current(self):
        layer = self.layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer) or self._index < 0 or not self._fids:
            return

        fid = self._fids[self._index]
        feat = layer.getFeature(fid)

        # Selection
        if self.select_chk.isChecked():
            layer.selectByIds([fid])

        geom = feat.geometry()
        if geom and not geom.isEmpty():
            # Zoom
            if self.zoom_chk.isChecked():
                bbox = geom.boundingBox()
                size = max(bbox.width(), bbox.height(), 1.0)
                bbox.grow(size * 0.25)
                self.canvas.setExtent(bbox)
                self.canvas.refresh()


        self._update_nav_buttons()

