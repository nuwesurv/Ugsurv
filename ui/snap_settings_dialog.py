# -*- coding: utf-8 -*-
"""
SnapSettingsDialog — floating, modeless dialog for snap provider toggles.

Open with show() / raise_() / activateWindow().
Accessible via the CAD toolbar button or by typing SNAP / OS in the command line.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QFormLayout,
    QCheckBox, QSpinBox, QDoubleSpinBox, QGroupBox, QPushButton,
)
from qgis.PyQt.QtCore import Qt


_PROVIDERS = [
    ("vertex",        "Endpoint  (line/polygon vertex)"),
    ("point",         "Point  (point feature)"),
    ("midpoint",      "Midpoint"),
    ("center",        "Center"),
    ("intersection",  "Intersection"),
    ("perpendicular", "Perpendicular"),
    ("extension",     "Extension"),
    ("grid",          "Grid"),
    ("self",          "Self  (in-progress geometry)"),
]


class SnapSettingsDialog(QDialog):
    def __init__(self, snap_settings, snap_engine, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Snap Settings")
        self.setObjectName("UgsurvSnapSettings")
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setModal(False)
        self.setMinimumWidth(260)

        self._settings = snap_settings
        self._engine   = snap_engine
        self._checks:  dict[str, QCheckBox] = {}

        self._build_ui()

    def _build_ui(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        grp_prov = QGroupBox("Snap providers")
        form = QFormLayout(grp_prov)
        for key, label in _PROVIDERS:
            chk = QCheckBox()
            chk.setChecked(self._settings.is_enabled(key))
            chk.stateChanged.connect(
                lambda state, k=key: self._settings.set_enabled(k, state != 0)
            )
            form.addRow(label, chk)
            self._checks[key] = chk
        vlay.addWidget(grp_prov)

        grp_tol = QGroupBox("Tolerance")
        form2 = QFormLayout(grp_tol)
        tol_spin = QSpinBox()
        tol_spin.setRange(1, 100)
        tol_spin.setValue(self._settings.tolerance_px)
        tol_spin.setSuffix(" px")
        tol_spin.valueChanged.connect(self._on_tol_changed)
        form2.addRow("Snap radius:", tol_spin)
        vlay.addWidget(grp_tol)

        grp_grid = QGroupBox("Grid snap spacing")
        form3 = QFormLayout(grp_grid)
        for attr, label in [("spacing_x", "X spacing:"), ("spacing_y", "Y spacing:")]:
            sp = QDoubleSpinBox()
            sp.setRange(0.001, 100000)
            sp.setDecimals(4)
            sp.setValue(1.0)
            sp.valueChanged.connect(
                lambda v, a=attr: self._on_grid_changed(a, v)
            )
            form3.addRow(label, sp)
        vlay.addWidget(grp_grid)

        btn = QPushButton("Close")
        btn.clicked.connect(self.hide)
        vlay.addWidget(btn)

    def open_or_raise(self):
        """Show the dialog if hidden, or bring it to front if already open."""
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_tol_changed(self, value: int):
        self._settings.tolerance_px = value

    def _on_grid_changed(self, attr: str, value: float):
        if self._engine:
            prov = self._engine._providers.get("grid")
            if prov and hasattr(prov, attr):
                setattr(prov, attr, value)
