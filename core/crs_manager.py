# -*- coding: utf-8 -*-
"""
CrsManager — enforces Uganda-specific CRS rules for the UgSurv plugin.

Layer CRS (permanent): EPSG:32636  WGS 84 / UTM Zone 36N
  All geometry is stored in this CRS regardless of the project view CRS.

Allowed project CRS (8 systems):
  WGS84  35N  EPSG:32635   WGS84  36N  EPSG:32636
  WGS84  35S  EPSG:32735   WGS84  36S  EPSG:32736
  Arc60  35N  EPSG:21035   Arc60  36N  EPSG:21036
  Arc60  35S  EPSG:21095   Arc60  36S  EPSG:21096

On plugin load the project CRS is forced to EPSG:32636.
If the user later changes it to an out-of-list CRS, the change is reverted
and a warning is shown.
"""

import contextlib

from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QStyle, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsProject, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsPointXY, QgsGeometry,
)

# ── Constants ─────────────────────────────────────────────────────────────────

LAYER_EPSG = 32636   # WGS 84 / UTM Zone 36N — permanent layer/storage CRS

ALLOWED_EPSG: frozenset[int] = frozenset({
    32635, 32636, 32735, 32736,   # WGS 84
    21035, 21036, 21095, 21096,   # Arc 1960
})

_SHORT: dict[int, str] = {
    32635: "WGS84 35N",  32636: "WGS84 36N",
    32735: "WGS84 35S",  32736: "WGS84 36S",
    21035: "Arc60 35N",  21036: "Arc60 36N",
    21095: "Arc60 35S",  21096: "Arc60 36S",
}


class _CrsNotAllowedDialog(QDialog):
    """Styled warning shown when the user picks a CRS not in the allowed list."""

    _ROWS = [
        ("WGS 84 North",   "EPSG:32635", "EPSG:32636"),
        ("WGS 84 South",   "EPSG:32735", "EPSG:32736"),
        ("Arc 1960 North", "EPSG:21035", "EPSG:21036"),
        ("Arc 1960 South", "EPSG:21095", "EPSG:21096"),
    ]
    _ORANGE = "#E67E22"
    _DARK   = "#D35400"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("UgSurv — CRS Not Supported")
        self.setMinimumWidth(440)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Orange header banner ──────────────────────────────────────────
        banner = QFrame()
        banner.setFixedHeight(44)
        banner.setStyleSheet(
            f"background-color: {self._ORANGE}; border-radius: 0px;"
        )
        brow = QHBoxLayout(banner)
        brow.setContentsMargins(14, 6, 14, 6)
        brow.setSpacing(8)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(
            QApplication.style()
            .standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
            .pixmap(22, 22)
        )
        brow.addWidget(icon_lbl)

        title_lbl = QLabel("CRS Not Supported")
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        title_lbl.setFont(f)
        title_lbl.setStyleSheet("color: white; background: transparent;")
        brow.addWidget(title_lbl)
        brow.addStretch()
        root.addWidget(banner)

        # ── Body ──────────────────────────────────────────────────────────
        body = QWidget()
        body.setStyleSheet("background-color: #FAFAFA;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(20, 16, 20, 4)
        bl.setSpacing(10)

        msg = QLabel(
            "The selected coordinate system is not supported by UgSurv.\n"
            "The project CRS has been reset to <b>WGS 84 / UTM Zone 36N</b>."
        )
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #2C3E50; font-size: 10pt;")
        bl.addWidget(msg)

        sub = QLabel("Allowed coordinate systems for Uganda:")
        sub.setStyleSheet("color: #555; font-weight: bold; font-size: 9pt;")
        bl.addWidget(sub)

        # CRS table
        tbl = QTableWidget(len(self._ROWS), 3)
        tbl.setHorizontalHeaderLabels(["Datum", "Zone 35", "Zone 36"])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tbl.setAlternatingRowColors(True)
        tbl.setStyleSheet(
            "QTableWidget { border: 1px solid #DDD; border-radius: 4px; }"
            "QHeaderView::section {"
            "  background-color: #E0E0E0; font-weight: bold;"
            "  border: none; padding: 4px; }"
            "QTableWidget::item { padding: 4px 8px; }"
        )
        tbl.horizontalHeader().setStretchLastSection(True)

        ROW_H = 26
        for r, (datum, z35, z36) in enumerate(self._ROWS):
            tbl.setRowHeight(r, ROW_H)
            for c, val in enumerate([datum, z35, z36]):
                item = QTableWidgetItem(val)
                align = (
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                    if c == 0
                    else Qt.AlignmentFlag.AlignCenter
                )
                item.setTextAlignment(align)
                tbl.setItem(r, c, item)

        tbl.resizeColumnToContents(0)
        tbl.resizeColumnToContents(1)
        tbl.setFixedHeight(
            tbl.horizontalHeader().height() + len(self._ROWS) * ROW_H + 4
        )
        bl.addWidget(tbl)
        root.addWidget(body)

        # ── Button row ────────────────────────────────────────────────────
        btn_area = QWidget()
        btn_area.setStyleSheet("background-color: #FAFAFA;")
        brow2 = QHBoxLayout(btn_area)
        brow2.setContentsMargins(20, 8, 20, 16)
        brow2.addStretch()

        ok_btn = QPushButton("Got it")
        ok_btn.setFixedSize(100, 32)
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: {self._ORANGE}; color: white;"
            f"  border-radius: 5px; font-weight: bold; font-size: 10pt;"
            f"}}"
            f"QPushButton:hover {{ background-color: {self._DARK}; }}"
            f"QPushButton:pressed {{ background-color: {self._DARK}; }}"
        )
        ok_btn.clicked.connect(self.accept)
        brow2.addWidget(ok_btn)
        root.addWidget(btn_area)


class CrsManager(QObject):
    """
    Enforces CRS rules and provides coordinate transformation helpers.

    Signals:
        crsLabelChanged(str) — short label of the current project CRS, emitted
                               whenever the project CRS is accepted or reset.
    """

    crsLabelChanged = pyqtSignal(str)

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self._iface     = iface
        self._layer_crs = QgsCoordinateReferenceSystem(f"EPSG:{LAYER_EPSG}")
        self._reverting = False   # guard against re-entrant crsChanged

        QgsProject.instance().crsChanged.connect(self._on_crs_changed)

    # ── public API ────────────────────────────────────────────────────────────

    def enforce_project_crs(self):
        """Force the QGIS project CRS to WGS84 36N (EPSG:32636)."""
        self._set_project_crs(LAYER_EPSG)

    @property
    def layer_crs(self) -> QgsCoordinateReferenceSystem:
        """The fixed layer / storage CRS (EPSG:32636)."""
        return self._layer_crs

    @property
    def project_crs(self) -> QgsCoordinateReferenceSystem:
        """The current QGIS project CRS."""
        return QgsProject.instance().crs()

    @property
    def short_label(self) -> str:
        """Short human-readable name of the current project CRS."""
        return _SHORT.get(self._project_epsg(), "Unknown CRS")

    def project_to_layer(self, point: QgsPointXY) -> QgsPointXY:
        """Transform a point from the current project CRS to EPSG:32636."""
        proj = QgsProject.instance().crs()
        if proj.authid() == self._layer_crs.authid():
            return point
        xform = QgsCoordinateTransform(proj, self._layer_crs,
                                       QgsProject.instance())
        return xform.transform(point)

    def layer_to_project(self, point: QgsPointXY) -> QgsPointXY:
        """Transform a point from EPSG:32636 to the current project CRS."""
        proj = QgsProject.instance().crs()
        if proj.authid() == self._layer_crs.authid():
            return point
        xform = QgsCoordinateTransform(self._layer_crs, proj,
                                       QgsProject.instance())
        return xform.transform(point)

    def transform_geom_to_layer(self, geom: QgsGeometry) -> QgsGeometry:
        """
        Return a copy of geom transformed from project CRS to EPSG:32636.
        The original geometry is unchanged.
        """
        proj = QgsProject.instance().crs()
        result = QgsGeometry(geom)
        if proj.authid() == self._layer_crs.authid():
            return result
        xform = QgsCoordinateTransform(proj, self._layer_crs,
                                       QgsProject.instance())
        result.transform(xform)
        return result

    def unload(self):
        """Disconnect project signals on plugin teardown."""
        with contextlib.suppress(Exception):
            QgsProject.instance().crsChanged.disconnect(self._on_crs_changed)

    # ── internal ──────────────────────────────────────────────────────────────

    def _set_project_crs(self, epsg: int):
        self._reverting = True
        try:
            QgsProject.instance().setCrs(
                QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
            )
        finally:
            self._reverting = False
        self.crsLabelChanged.emit(self.short_label)

    def _on_crs_changed(self):
        if self._reverting:
            return
        epsg = self._project_epsg()
        if epsg not in ALLOWED_EPSG:
            # Revert and warn
            self._set_project_crs(LAYER_EPSG)
            dlg = _CrsNotAllowedDialog(self._iface.mainWindow())
            dlg.exec()
            return
        self.crsLabelChanged.emit(self.short_label)

    def _project_epsg(self) -> int:
        crs = QgsProject.instance().crs()
        if not crs.isValid():
            return 0
        auth = crs.authid()      # e.g. "EPSG:32636"
        try:
            return int(auth.split(":")[-1])
        except (ValueError, IndexError):
            return 0
