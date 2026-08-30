# -*- coding: utf-8 -*-
"""
SnapSettingsDock — toggles for each SnapProvider, all off by default.

Also exposes a tolerance spinbox and grid spacing controls.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QFormLayout,
    QCheckBox, QSpinBox, QDoubleSpinBox, QGroupBox, QLabel,
)
from qgis.PyQt.QtCore import Qt


_PROVIDERS = [
    ("vertex",       "Vertex"),
    ("midpoint",     "Midpoint"),
    ("center",       "Center"),
    ("intersection", "Intersection"),
    ("perpendicular","Perpendicular"),
    ("extension",    "Extension"),
    ("grid",         "Grid"),
    ("self",         "Self (in-progress)"),
]


class SnapSettingsDock(QDockWidget):
    def __init__(self, snap_settings, snap_engine, parent=None):
        super().__init__("Snap Settings", parent)
        self.setObjectName("UgsurvSnapSettings")
        self._settings = snap_settings
        self._engine   = snap_engine
        self._checks:  dict[str, QCheckBox] = {}

        self._build_ui()

    def _build_ui(self):
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(6, 6, 6, 6)

        grp_prov = QGroupBox("Snap providers (all off by default)")
        form = QFormLayout(grp_prov)
        for key, label in _PROVIDERS:
            chk = QCheckBox()
            chk.setChecked(False)
            chk.stateChanged.connect(
                lambda state, k=key: self._on_provider_toggle(k, state)
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
        form2.addRow("Snap tolerance:", tol_spin)
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

        vlay.addStretch()
        self.setWidget(w)

    def _on_provider_toggle(self, key: str, state: int):
        self._settings.set_enabled(key, state != 0)

    def _on_tol_changed(self, value: int):
        self._settings.tolerance_px = value

    def _on_grid_changed(self, attr: str, value: float):
        if self._engine:
            prov = self._engine._providers.get("grid")
            if prov and hasattr(prov, attr):
                setattr(prov, attr, value)
