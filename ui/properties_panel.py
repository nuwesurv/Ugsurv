# -*- coding: utf-8 -*-
"""
PropertiesPanel — reflects the current selection's parameters.

Talks to the SelectionModel via a common interface (get_property /
set_property) — it does not contain domain logic.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QFormLayout, QLineEdit,
    QLabel, QPushButton, QScrollArea,
)
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject


class PropertiesPanel(QDockWidget):
    def __init__(self, selection_model, parent=None):
        super().__init__("Properties", parent)
        self.setObjectName("UgsurvProperties")
        self._sel = selection_model
        self._editors: dict = {}

        self._build_ui()
        self._sel.selectionChanged.connect(self._refresh)

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._form = QFormLayout(inner)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        scroll.setWidget(inner)
        self.setWidget(scroll)

    def _refresh(self):
        try:
            row_count = self._form.rowCount()
        except RuntimeError:
            return  # form already deleted (dock being torn down)
        while row_count:
            self._form.removeRow(0)
            row_count -= 1
        self._editors.clear()

        if self._sel.is_empty():
            self._form.addRow(QLabel("No selection"))
            return

        # Show first selected feature's attributes
        lid, fid = next(iter(self._sel))
        layer = QgsProject.instance().mapLayer(lid)
        if layer is None:
            return
        feat = layer.getFeature(fid)
        if not feat.isValid():
            return

        for field in layer.fields():
            val = feat[field.name()]
            ed = QLineEdit(str(val) if val is not None else "")
            ed.setReadOnly(True)
            self._form.addRow(field.name(), ed)
            self._editors[field.name()] = ed

        # geometry info
        geom = feat.geometry()
        if not geom.isEmpty():
            _GNAMES = {0: "Point", 1: "Line", 2: "Polygon"}
            lbl = QLabel(_GNAMES.get(int(geom.type()), "Unknown"))
            self._form.addRow("Geometry:", lbl)
            lbl2 = QLabel(f"{geom.length():.4f}" if geom.length() > 0 else "-")
            self._form.addRow("Length:", lbl2)
