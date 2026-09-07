# -*- coding: utf-8 -*-
"""
CadLayersDock — lists the cad_layers table rows; lets the user edit
color/linetype/visible/locked/sort_order.

Drives the rule-based renderer per geometry table and drives
SelectionModel's lock enforcement on every change.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem,
    QColorDialog, QHeaderView, QCheckBox, QAbstractItemView,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QBrush


_COLS = ["Name", "Color", "Linetype", "Visible", "Locked", "Order"]


class CadLayersDock(QDockWidget):
    def __init__(self, cad_layer_manager, selection_model, storage_manager,
                 parent=None):
        super().__init__("CAD Layers", parent)
        self.setObjectName("UgsurvCadLayers")
        self._mgr  = cad_layer_manager
        self._sel  = selection_model
        self._stor = storage_manager

        self._build_ui()
        self._refresh()

    def _build_ui(self):
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 4, 4, 4)

        btn_row = QHBoxLayout()
        for label, slot in [("+ New", self._add_layer),
                             ("− Del", self._del_layer),
                             ("Set Active", self._set_active)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            btn_row.addWidget(b)
        vlay.addLayout(btn_row)

        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._table.cellChanged.connect(self._on_cell_changed)
        self._table.cellDoubleClicked.connect(self._on_double_click)
        vlay.addWidget(self._table)

        self.setWidget(w)

    def _refresh(self):
        try:
            self._table.blockSignals(True)
        except RuntimeError:
            return  # table already deleted (dock being torn down)
        self._table.setRowCount(0)
        for i, lyr in enumerate(self._mgr.all_layers()):
            self._table.insertRow(i)
            self._table.setItem(i, 0, QTableWidgetItem(lyr.name))
            color_item = QTableWidgetItem(lyr.color)
            color_item.setBackground(QBrush(QColor(lyr.color)))
            self._table.setItem(i, 1, color_item)
            self._table.setItem(i, 2, QTableWidgetItem(lyr.linetype))
            v = QTableWidgetItem()
            v.setCheckState(Qt.CheckState.Checked if lyr.visible else Qt.CheckState.Unchecked)
            v.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(i, 3, v)
            l = QTableWidgetItem()
            l.setCheckState(Qt.CheckState.Checked if lyr.locked else Qt.CheckState.Unchecked)
            l.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(i, 4, l)
            self._table.setItem(i, 5, QTableWidgetItem(str(lyr.sort_order)))
        self._table.blockSignals(False)

    def _on_cell_changed(self, row: int, col: int):
        lyrs = self._mgr.all_layers()
        if row >= len(lyrs):
            return
        lyr = lyrs[row]
        item = self._table.item(row, col)
        if item is None:
            return
        if col == 0:
            lyr.name = item.text()
        elif col == 2:
            lyr.linetype = item.text()
        elif col == 3:
            lyr.visible = item.checkState() == Qt.CheckState.Checked
        elif col == 4:
            lyr.locked  = item.checkState() == Qt.CheckState.Checked
        elif col == 5:
            try:
                lyr.sort_order = int(item.text())
            except ValueError:  # nosec B110
                pass
        self._apply_changes()

    def _on_double_click(self, row: int, col: int):
        if col == 1:   # color column
            lyrs = self._mgr.all_layers()
            if row < len(lyrs):
                current = QColor(lyrs[row].color)
                new_col = QColorDialog.getColor(current, self)
                if new_col.isValid():
                    lyrs[row].color = new_col.name()
                    self._refresh()
                    self._apply_changes()

    def _add_layer(self):
        name = f"Layer{len(self._mgr.all_layers())}"
        self._mgr.add(name)
        self._refresh()
        self._apply_changes()

    def _del_layer(self):
        rows = {i.row() for i in self._table.selectedIndexes()}
        lyrs = self._mgr.all_layers()
        for r in sorted(rows, reverse=True):
            if r < len(lyrs) and lyrs[r].name != "0":
                self._mgr._layers.remove(lyrs[r])
        self._refresh()
        self._apply_changes()

    def _set_active(self):
        rows = {i.row() for i in self._table.selectedIndexes()}
        lyrs = self._mgr.all_layers()
        if rows:
            r = min(rows)
            if r < len(lyrs):
                self._mgr.active = lyrs[r].name

    def _apply_changes(self):
        sm = self._stor
        self._mgr.apply_renderer_to_layers(
            sm.points_layer, sm.lines_layer
        )
        self._sel.set_locked_cad_layers(self._mgr.locked_names())
        # persist to GeoPackage table if available
        if sm.cad_layers_table and sm.cad_layers_table.isValid():
            self._mgr.save_to_gpkg_layer(sm.cad_layers_table)
