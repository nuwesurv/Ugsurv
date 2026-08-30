# -*- coding: utf-8 -*-
"""
StorageManager — resolves/creates the geometries GeoPackage, gates the
drafting toolbar until the project has been saved at least once.

Hard rules (§5):
  - NEVER calls layer.commitChanges().
  - Tools are disabled (toolbar greyed out) until QgsProject.fileName() != "".
  - Hooks projectSaved to create the GeoPackage on first save.
  - Hooks cleared/readProject to re-evaluate gating state.

GeoPackage schema (§4):
  - points   (MultiPoint)      — cad_layer attribute
  - lines    (MultiLineString) — cad_layer attribute
  - polygons (MultiPolygon)    — cad_layer attribute
  - cad_layers (non-spatial)   — name, color, linetype, visible, locked, sort_order
"""

import os

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsField, QgsFields,
    QgsWkbTypes, QgsCoordinateReferenceSystem,
    QgsVectorFileWriter, QgsFeature, QgsGeometry,
)
from qgis.PyQt.QtCore import QVariant


class StorageManager(QObject):
    gatingChanged = pyqtSignal(bool)  # True = tools enabled, False = disabled

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled      = False
        self._gpkg_path    = None
        self._points_layer  = None
        self._lines_layer   = None
        self._poly_layer    = None
        self._cad_lyr_layer = None   # non-spatial QgsVectorLayer
        self._toolbar_ref   = None   # set by plugin_main

        # Wire QGIS project signals
        QgsProject.instance().projectSaved.connect(self._on_project_saved)
        QgsProject.instance().cleared.connect(self._on_project_cleared)
        QgsProject.instance().readProject.connect(self._on_project_read)

        self._evaluate_gating()

    # ── public ────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def points_layer(self) -> QgsVectorLayer | None:
        return self._points_layer

    @property
    def lines_layer(self) -> QgsVectorLayer | None:
        return self._lines_layer

    @property
    def polygons_layer(self) -> QgsVectorLayer | None:
        return self._poly_layer

    @property
    def cad_layers_table(self) -> QgsVectorLayer | None:
        return self._cad_lyr_layer

    @property
    def gpkg_path(self) -> str | None:
        return self._gpkg_path

    def set_toolbar(self, toolbar):
        self._toolbar_ref = toolbar
        self._apply_gating()

    def unload(self):
        with _suppress():
            QgsProject.instance().projectSaved.disconnect(self._on_project_saved)
        with _suppress():
            QgsProject.instance().cleared.disconnect(self._on_project_cleared)
        with _suppress():
            QgsProject.instance().readProject.disconnect(self._on_project_read)

    # ── project signal handlers ──────────────────────────────────────────
    def _on_project_saved(self):
        self._evaluate_gating()
        if self._enabled and not self._gpkg_path:
            self._create_or_load_gpkg()

    def _on_project_cleared(self):
        self._gpkg_path    = None
        self._points_layer  = None
        self._lines_layer   = None
        self._poly_layer    = None
        self._cad_lyr_layer = None
        self._evaluate_gating()

    def _on_project_read(self):
        self._evaluate_gating()
        # _evaluate_gating already calls _create_or_load_gpkg if needed

    # ── gating ────────────────────────────────────────────────────────────
    def _evaluate_gating(self):
        proj_file = QgsProject.instance().fileName()
        new_state = bool(proj_file)
        if new_state != self._enabled:
            self._enabled = new_state
            self._apply_gating()
            self.gatingChanged.emit(self._enabled)
        # Load GeoPackage immediately if project already exists on startup
        if self._enabled and not self._gpkg_path:
            self._create_or_load_gpkg()

    def _apply_gating(self):
        if self._toolbar_ref:
            self._toolbar_ref.setEnabled(self._enabled)

    # ── GeoPackage creation / loading ─────────────────────────────────────
    def _gpkg_path_from_project(self) -> str:
        proj_path = QgsProject.instance().absoluteFilePath()
        base = os.path.splitext(proj_path)[0]
        return base + "_geometries.gpkg"

    def _create_or_load_gpkg(self):
        path = self._gpkg_path_from_project()
        if not os.path.exists(path):
            self._create_gpkg(path)
        self._gpkg_path = path
        self._load_layers(path)

    def _create_gpkg(self, path: str):
        crs = QgsProject.instance().crs()
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4326")

        # Common geometry-table fields
        geom_fields = QgsFields()
        geom_fields.append(QgsField("cad_layer", QVariant.String))

        tables = [
            ("points",   QgsWkbTypes.MultiPoint),
            ("lines",    QgsWkbTypes.MultiLineString),
            ("polygons", QgsWkbTypes.MultiPolygon),
        ]
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"

        for idx, (tbl, wkb) in enumerate(tables):
            opts.layerName = tbl
            opts.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile if idx == 0
                else QgsVectorFileWriter.CreateOrOverwriteLayer
            )
            tmp = QgsVectorLayer(
                QgsWkbTypes.displayString(wkb) + "?crs=" + crs.authid(),
                tbl, "memory"
            )
            for field in geom_fields:
                tmp.dataProvider().addAttributes([field])
            tmp.updateFields()
            err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                tmp, path, QgsProject.instance().transformContext(), opts
            )

        # Non-spatial cad_layers table
        cad_fields = QgsFields()
        for fname, ftype in [
            ("name",       QVariant.String),
            ("color",      QVariant.String),
            ("linetype",   QVariant.String),
            ("visible",    QVariant.Int),
            ("locked",     QVariant.Int),
            ("sort_order", QVariant.Int),
        ]:
            cad_fields.append(QgsField(fname, ftype))

        opts2 = QgsVectorFileWriter.SaveVectorOptions()
        opts2.driverName   = "GPKG"
        opts2.layerName    = "cad_layers"
        opts2.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

        # "NoGeometry" is the correct URI for non-spatial memory layers
        tmp2 = QgsVectorLayer("NoGeometry", "cad_layers", "memory")
        for f in cad_fields:
            tmp2.dataProvider().addAttributes([f])
        tmp2.updateFields()
        # seed default layer "0"
        tmp2.dataProvider().addFeatures([_make_cad_layer_feat(tmp2.fields(),
            "0", "#ffffff", "solid", 1, 0, 0)])

        QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp2, path, QgsProject.instance().transformContext(), opts2
        )

    def _load_layers(self, path: str):
        # Normalise path for case-insensitive comparison on Windows.
        norm_path = os.path.normcase(os.path.normpath(path))

        def _find_existing(name):
            """Return an already-loaded layer from the same gpkg table, or None."""
            for lyr in QgsProject.instance().mapLayers().values():
                if not isinstance(lyr, QgsVectorLayer):
                    continue
                src = lyr.source()
                if f"|layername={name}" not in src:
                    continue
                src_file = os.path.normcase(os.path.normpath(src.split("|")[0]))
                if src_file == norm_path:
                    return lyr
            return None

        def open_layer(name, add_to_tree: bool = True):
            existing = _find_existing(name)
            if existing:
                return existing          # reuse — avoids duplicate on reload
            uri = f"{path}|layername={name}"
            lyr = QgsVectorLayer(uri, name, "ogr")
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr, add_to_tree)
            return lyr if lyr.isValid() else None

        self._points_layer  = open_layer("points",   add_to_tree=True)
        self._lines_layer   = open_layer("lines",    add_to_tree=True)
        self._poly_layer    = open_layer("polygons", add_to_tree=True)
        self._cad_lyr_layer = open_layer("cad_layers", add_to_tree=False)


class _suppress:
    """Tiny context manager to swallow exceptions (like contextlib.suppress)."""
    def __enter__(self): return self
    def __exit__(self, *a): return True


def _make_cad_layer_feat(fields, name, color, linetype, visible, locked, sort_order):
    feat = QgsFeature(fields)
    feat["name"]       = name
    feat["color"]      = color
    feat["linetype"]   = linetype
    feat["visible"]    = visible
    feat["locked"]     = locked
    feat["sort_order"] = sort_order
    return feat
