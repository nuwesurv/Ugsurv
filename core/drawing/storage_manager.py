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
  - points   (Point) — cad_layer attribute
  - lines    (CompoundCurve) — cad_layer attribute; stores true circles as CircularString
  - cad_layers (non-spatial) — name, color, linetype, visible, locked, sort_order

Legacy "polygons" table (MultiPolygon) is migrated to closed LineStrings in
the lines table on first load of an older GeoPackage, then removed from the project.
"""

import os

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsField, QgsFields,
    QgsWkbTypes, QgsCoordinateReferenceSystem,
    QgsVectorFileWriter, QgsFeature, QgsGeometry, QgsPointXY,
    QgsPoint, QgsLineString, QgsCompoundCurve,
)

_LAYER_EPSG = 32636   # WGS 84 / UTM Zone 36N — all geometry stored here
from qgis.PyQt.QtCore import QVariant

try:
    from ..renderer_utils import (
        apply_polyline_color_renderer,
        apply_point_color_renderer,
        apply_point_label_style,
    )
except Exception:
    def apply_polyline_color_renderer(layer): pass
    def apply_point_color_renderer(layer): pass
    def apply_point_label_style(layer): pass


def _geom_has_circular_string(geom: QgsGeometry) -> bool:
    """Return True if geom contains a QgsCircularString arc."""
    curve = geom.constGet()
    name = type(curve).__name__
    if name == 'QgsCircularString':
        return True
    if name == 'QgsCompoundCurve':
        for i in range(curve.nCurves()):
            if type(curve.curveAt(i)).__name__ == 'QgsCircularString':
                return True
    return False


def _linestring_to_compound_curve(geom: QgsGeometry) -> QgsGeometry:
    """Wrap a plain LineString in a CompoundCurve so it can be stored in a CompoundCurve layer."""
    pts = [QgsPoint(p.x(), p.y()) for p in geom.asPolyline()]
    ls = QgsLineString(pts)
    cc = QgsCompoundCurve()
    cc.addCurve(ls)
    return QgsGeometry(cc)


class StorageManager(QObject):
    gatingChanged = pyqtSignal(bool)  # True = tools enabled, False = disabled

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled      = False
        self._gpkg_path    = None
        self._points_layer  = None
        self._lines_layer   = None
        self._circles_layer = None
        self._cad_lyr_layer = None   # non-spatial QgsVectorLayer
        self._toolbar_ref   = None   # set by plugin_main
        self._crs_manager   = None   # set by plugin after CrsManager is created

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
        return self._live('_points_layer')

    @property
    def lines_layer(self) -> QgsVectorLayer | None:
        return self._live('_lines_layer')

    @property
    def circles_layer(self) -> QgsVectorLayer | None:
        return self._live('_circles_layer')

    @property
    def cad_layers_table(self) -> QgsVectorLayer | None:
        return self._live('_cad_lyr_layer')

    @property
    def gpkg_path(self) -> str | None:
        return self._gpkg_path

    @staticmethod
    def _layer_alive(lyr) -> bool:
        """Return True only if lyr is a live, valid QgsVectorLayer."""
        if lyr is None:
            return False
        try:
            return lyr.isValid()
        except RuntimeError:
            return False

    def _live(self, attr: str) -> QgsVectorLayer | None:
        """Return the layer stored in *attr*, reloading from disk if stale."""
        lyr = getattr(self, attr)
        if self._layer_alive(lyr):
            return lyr
        if not (self._gpkg_path and os.path.exists(self._gpkg_path)):
            return None
        # Stale reference — reopen from disk (don't reload all layers).
        name = {"_points_layer": "points", "_lines_layer": "lines",
                "_cad_lyr_layer": "cad_layers", "_circles_layer": "circles"}.get(attr)
        if name is None:
            return None
        add_to_tree = attr in ("_points_layer", "_lines_layer", "_circles_layer")
        lyr = self._open_gpkg_layer(name, add_to_tree=add_to_tree)
        if lyr is not None:
            setattr(self, attr, lyr)
        return lyr

    def set_toolbar(self, toolbar):
        self._toolbar_ref = toolbar
        self._apply_gating()

    def set_crs_manager(self, mgr):
        """Wire the CrsManager so that add_line/add_point can transform geometry."""
        self._crs_manager = mgr

    # ── CRS-aware feature writers ─────────────────────────────────────────
    def add_line(self, geom: QgsGeometry, cad_layer: str) -> bool:
        """
        Add a line geometry (in current project CRS) to the lines layer.
        The geometry is transformed to EPSG:32636 (layer CRS) if needed.
        """
        if not self._enabled:
            return False
        self._ensure_lines_layer()
        layer = self._lines_layer
        if not self._layer_alive(layer):
            return False
        if self._crs_manager is not None:
            geom = self._crs_manager.transform_geom_to_layer(geom)
        if layer.wkbType() == QgsWkbTypes.CompoundCurve and geom.wkbType() == QgsWkbTypes.LineString:
            geom = _linestring_to_compound_curve(geom)
        if not layer.isEditable():
            layer.startEditing()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat["cad_layer"] = cad_layer
        return layer.addFeature(feat)

    def add_circle(self, geom: QgsGeometry, cad_layer: str) -> bool:
        """Add a circle geometry (CompoundCurve/CircularString) to the circles layer."""
        if not self._enabled:
            return False
        self._ensure_circles_layer()
        layer = self._circles_layer
        if not self._layer_alive(layer):
            return False
        if self._crs_manager is not None:
            geom = self._crs_manager.transform_geom_to_layer(geom)
        if not layer.isEditable():
            layer.startEditing()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat["cad_layer"] = cad_layer
        return layer.addFeature(feat)

    def add_point(self, geom: QgsGeometry, cad_layer: str,
                  description=None, symbol: str = "basic",
                  symbol_size: float = None) -> bool:
        """
        Add a point geometry (in current project CRS) to the points layer.
        The geometry is transformed to EPSG:32636 (layer CRS) if needed.
        Description defaults to NULL; Symbol (SVG) and symbol (shape) default to "basic".
        symbol_size sets the symbol_size field when provided.
        """
        if not self._enabled:
            return False
        self._ensure_points_layer()
        layer = self._points_layer
        if not self._layer_alive(layer):
            return False
        if self._crs_manager is not None:
            geom = self._crs_manager.transform_geom_to_layer(geom)
        if not layer.isEditable():
            layer.startEditing()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat["cad_layer"] = cad_layer
        flds = layer.fields()
        if description is not None and flds.indexOf("Description") >= 0:
            feat["Description"] = description
        if flds.indexOf("Symbol") >= 0:
            feat["Symbol"] = symbol
        if flds.indexOf("symbol") >= 0:
            feat["symbol"] = symbol
        if symbol_size is not None and flds.indexOf("symbol_size") >= 0:
            feat["symbol_size"] = float(symbol_size)
        return layer.addFeature(feat)

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
        # Do NOT auto-create the GPKG here — it is created on first actual use.

    def _on_project_cleared(self):
        self._gpkg_path     = None
        self._points_layer  = None
        self._lines_layer   = None
        self._circles_layer = None
        self._cad_lyr_layer = None
        self._evaluate_gating()

    def _on_layer_deleted(self):
        """Called when any relevant layer is removed; nulls stale references."""
        for attr in ('_points_layer', '_lines_layer', '_circles_layer', '_cad_lyr_layer'):
            lyr = getattr(self, attr)
            if not self._layer_alive(lyr):
                setattr(self, attr, None)

    def _on_project_read(self):
        self._evaluate_gating()

    # ── gating ────────────────────────────────────────────────────────────
    def _evaluate_gating(self):
        proj_file = QgsProject.instance().fileName()
        new_state = bool(proj_file)
        if new_state != self._enabled:
            self._enabled = new_state
            self._apply_gating()
            self.gatingChanged.emit(self._enabled)
        # Re-attach to an existing GPKG when a project is opened, but never create one.
        if self._enabled and not self._gpkg_path:
            self._load_existing_gpkg_if_present()

    def _apply_gating(self):
        if self._toolbar_ref:
            self._toolbar_ref.setEnabled(self._enabled)

    # ── GeoPackage creation / loading ─────────────────────────────────────
    def _load_existing_gpkg_if_present(self):
        """Re-attach to an existing GeoPackage when a project is opened. Never creates one."""
        path = self._gpkg_path_from_project()
        if os.path.exists(path):
            self._gpkg_path = path
            self._load_layers(path)

    def _ensure_lines_layer(self):
        """Create and load the lines layer on first actual use. No-op if already alive."""
        if self._layer_alive(self._lines_layer):
            return
        self._ensure_gpkg_file()
        lyr = self._open_gpkg_layer("lines", add_to_tree=True)
        if lyr is None:
            self._add_geom_table(self._gpkg_path, "lines", QgsWkbTypes.CompoundCurve)
            lyr = self._open_gpkg_layer("lines", add_to_tree=True)
        self._lines_layer = lyr
        self._ensure_extra_line_fields()
        if self._layer_alive(lyr):
            apply_polyline_color_renderer(lyr)

    def _ensure_circles_layer(self):
        if self._layer_alive(self._circles_layer):
            return
        self._ensure_gpkg_file()
        lyr = self._open_gpkg_layer("circles", add_to_tree=True)
        if lyr is None:
            self._add_geom_table(self._gpkg_path, "circles", QgsWkbTypes.CompoundCurve)
            lyr = self._open_gpkg_layer("circles", add_to_tree=True)
        self._circles_layer = lyr

    def _ensure_points_layer(self):
        """Create and load the points layer on first actual use. No-op if already alive."""
        if self._layer_alive(self._points_layer):
            return
        self._ensure_gpkg_file()
        lyr = self._open_gpkg_layer("points", add_to_tree=True)
        if lyr is None:
            self._add_geom_table(self._gpkg_path, "points", QgsWkbTypes.Point)
            lyr = self._open_gpkg_layer("points", add_to_tree=True)
        self._points_layer = lyr
        self._ensure_extra_point_fields()
        if self._layer_alive(lyr):
            apply_point_color_renderer(lyr)
            apply_point_label_style(lyr)

    def _ensure_extra_point_fields(self):
        """Add any missing columns to the points layer (safe no-op if all exist)."""
        lyr = self._points_layer
        if not self._layer_alive(lyr):
            return
        needed = [
            ("Description",  QVariant.String),
            ("color",        QVariant.String),
            ("symbol",       QVariant.String),
            ("symbol_size",  QVariant.Double),
        ]
        # Case-insensitive check: SQLite treats "Symbol" and "symbol" as the same column.
        existing = {f.name().lower() for f in lyr.fields()}
        missing = [QgsField(n, t) for n, t in needed if n.lower() not in existing]
        if not missing:
            return
        lyr.dataProvider().addAttributes(missing)
        lyr.updateFields()

    def _ensure_extra_line_fields(self):
        """Add any missing columns to the lines layer (safe no-op if all exist)."""
        lyr = self._lines_layer
        if not self._layer_alive(lyr):
            return
        needed = [
            ("color",           QVariant.String),
            ("line_type",       QVariant.String),
            ("line_thickness",  QVariant.Double),
        ]
        missing = [QgsField(n, t) for n, t in needed if lyr.fields().indexOf(n) < 0]
        if not missing:
            return
        lyr.dataProvider().addAttributes(missing)
        lyr.updateFields()

    def _ensure_gpkg_file(self):
        """Ensure the GPKG file and cad_layers metadata table exist. No geometry tables yet."""
        if self._gpkg_path and os.path.exists(self._gpkg_path):
            if not self._layer_alive(self._cad_lyr_layer):
                self._cad_lyr_layer = self._open_gpkg_layer("cad_layers", add_to_tree=False)
            return
        path = self._gpkg_path_from_project()
        self._create_gpkg_file(path)
        self._gpkg_path = path
        self._cad_lyr_layer = self._open_gpkg_layer("cad_layers", add_to_tree=False)

    def _open_gpkg_layer(self, name: str, add_to_tree: bool = False) -> QgsVectorLayer | None:
        """Return a layer from the current GPKG table, reusing an already-loaded one if present."""
        if not self._gpkg_path:
            return None
        norm_path = os.path.normcase(os.path.normpath(self._gpkg_path))
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            src = lyr.source()
            if f"|layername={name}" not in src:
                continue
            if os.path.normcase(os.path.normpath(src.split("|")[0])) == norm_path:
                return lyr
        uri = f"{self._gpkg_path}|layername={name}"
        lyr = QgsVectorLayer(uri, name, "ogr")
        if not lyr.isValid():
            return None
        if add_to_tree:
            QgsProject.instance().addMapLayer(lyr, False)
            self._get_or_create_group().addLayer(lyr)
        else:
            QgsProject.instance().addMapLayer(lyr, False)
        return lyr

    def _gpkg_path_from_project(self) -> str:
        proj_path = QgsProject.instance().absoluteFilePath()
        base = os.path.splitext(proj_path)[0]
        return base + "_Ugsurv.gpkg"

    def _create_gpkg_file(self, path: str):
        """Create a new GPKG containing only the cad_layers metadata table."""
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

        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName   = "GPKG"
        opts.layerName    = "cad_layers"
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

        tmp = QgsVectorLayer("NoGeometry", "cad_layers", "memory")
        for f in cad_fields:
            tmp.dataProvider().addAttributes([f])
        tmp.updateFields()
        tmp.dataProvider().addFeatures([_make_cad_layer_feat(tmp.fields(),
            "0", "#ffffff", "solid", 1, 0, 0)])
        QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, path, QgsProject.instance().transformContext(), opts
        )

    def _add_geom_table(self, path: str, name: str, wkb_type):
        """Add a single geometry table to an existing GPKG."""
        crs = QgsCoordinateReferenceSystem(f"EPSG:{_LAYER_EPSG}")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName  = name
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        tmp = QgsVectorLayer(
            QgsWkbTypes.displayString(wkb_type) + "?crs=" + crs.authid(), name, "memory"
        )
        tmp.dataProvider().addAttributes([QgsField("cad_layer", QVariant.String)])
        tmp.updateFields()
        QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, path, QgsProject.instance().transformContext(), opts
        )

    def _get_or_create_group(self):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup("Ugsurv")
        if group is None:
            group = root.insertGroup(0, "Ugsurv")
        return group

    def _load_layers(self, path: str):
        """Reload all existing tables from a GPKG (used when opening a saved project)."""
        self._gpkg_path     = path
        self._points_layer  = self._open_gpkg_layer("points",     add_to_tree=True)
        self._lines_layer   = self._open_gpkg_layer("lines",      add_to_tree=True)
        self._circles_layer = self._open_gpkg_layer("circles",    add_to_tree=True)
        self._cad_lyr_layer = self._open_gpkg_layer("cad_layers", add_to_tree=False)
        if self._points_layer and self._points_layer.wkbType() != QgsWkbTypes.Point:
            self._points_layer = self._migrate_points_to_point(path, self._points_layer)

        # Migrate existing points layers that predate the Description/Symbol columns
        if self._points_layer is not None:
            self._ensure_extra_point_fields()

        if self._lines_layer and self._lines_layer.wkbType() != QgsWkbTypes.CompoundCurve:
            self._lines_layer = self._migrate_lines_to_compound_curve(path, self._lines_layer)
        if self._lines_layer is not None:
            self._ensure_extra_line_fields()

        poly_lyr = self._open_gpkg_layer("polygons", add_to_tree=False)
        if poly_lyr:
            self._migrate_polygons_to_lines(poly_lyr)

        self._migrate_circles_to_own_layer()

        if self._layer_alive(self._lines_layer):
            apply_polyline_color_renderer(self._lines_layer)
        if self._layer_alive(self._points_layer):
            apply_point_color_renderer(self._points_layer)
            apply_point_label_style(self._points_layer)


    def _migrate_points_to_point(self, path: str, old_lyr: QgsVectorLayer) -> QgsVectorLayer | None:
        """Migrate a MultiPoint (or other non-Point) points table to Point."""
        crs = old_lyr.crs()
        old_fields = old_lyr.fields()
        new_feats = []
        for feat in old_lyr.getFeatures():
            geom = feat.geometry()
            for part in geom.parts():
                nf = QgsFeature(old_fields)
                nf.setGeometry(QgsGeometry(QgsPoint(part.x(), part.y())))
                for field in old_fields:
                    nf[field.name()] = feat[field.name()]
                new_feats.append(nf)

        QgsProject.instance().removeMapLayer(old_lyr.id())

        tmp = QgsVectorLayer(f"Point?crs={crs.authid()}", "points", "memory")
        tmp.dataProvider().addAttributes(list(old_fields))
        tmp.updateFields()
        tmp.dataProvider().addFeatures(new_feats)

        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName  = "points"
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, path, QgsProject.instance().transformContext(), opts
        )

        new_lyr = QgsVectorLayer(f"{path}|layername=points", "points", "ogr")
        if new_lyr.isValid():
            QgsProject.instance().addMapLayer(new_lyr, False)
            self._get_or_create_group().addLayer(new_lyr)
            print(f"[UgSurv] Migrated {len(new_feats)} point(s) from MultiPoint to Point.")
            return new_lyr
        return None

    def _migrate_lines_to_compound_curve(self, path: str, old_lyr: QgsVectorLayer) -> QgsVectorLayer | None:
        """Migrate any LineString/MultiLineString lines layer to CompoundCurve.

        Existing straight-line features are preserved exactly — each part becomes
        a CompoundCurve wrapping a straight QgsLineString. New circles will be
        stored as CompoundCurve(CircularString) without segment approximation.
        """
        crs = old_lyr.crs()
        tmp_fields = QgsFields()
        tmp_fields.append(QgsField("cad_layer", QVariant.String))
        new_feats = []

        for feat in old_lyr.getFeatures():
            geom = feat.geometry()
            cad = feat["cad_layer"]
            for part in geom.parts():
                pts = [QgsPoint(v.x(), v.y()) for v in part.vertices()]
                if len(pts) < 2:
                    continue
                ls = QgsLineString(pts)
                cc = QgsCompoundCurve()
                cc.addCurve(ls)
                nf = QgsFeature(tmp_fields)
                nf.setGeometry(QgsGeometry(cc))
                nf["cad_layer"] = cad
                new_feats.append(nf)

        QgsProject.instance().removeMapLayer(old_lyr.id())

        uri = "CompoundCurve?crs=" + crs.authid()
        tmp = QgsVectorLayer(uri, "lines", "memory")
        tmp.dataProvider().addAttributes(list(tmp_fields))
        tmp.updateFields()
        tmp.dataProvider().addFeatures(new_feats)

        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName  = "lines"
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, path, QgsProject.instance().transformContext(), opts
        )

        new_lyr = QgsVectorLayer(f"{path}|layername=lines", "lines", "ogr")
        if new_lyr.isValid():
            QgsProject.instance().addMapLayer(new_lyr, False)
            self._get_or_create_group().addLayer(new_lyr)
            return new_lyr
        return None


    def _migrate_circles_to_own_layer(self):
        """Move any CircularString features from the lines layer into the circles layer."""
        lines_lyr = self._lines_layer
        if not self._layer_alive(lines_lyr):
            return
        circle_fids = []
        circle_data = []
        for feat in lines_lyr.getFeatures():
            if _geom_has_circular_string(feat.geometry()):
                circle_fids.append(feat.id())
                circle_data.append((feat.geometry(), feat["cad_layer"]))
        if not circle_fids:
            return
        self._ensure_circles_layer()
        circles_lyr = self._circles_layer
        if not self._layer_alive(circles_lyr):
            return
        if not circles_lyr.isEditable():
            circles_lyr.startEditing()
        for geom, cad in circle_data:
            nf = QgsFeature(circles_lyr.fields())
            nf.setGeometry(geom)
            nf["cad_layer"] = cad
            circles_lyr.addFeature(nf)
        if not lines_lyr.isEditable():
            lines_lyr.startEditing()
        lines_lyr.deleteFeatures(circle_fids)
        print(f"[UgSurv] Migrated {len(circle_fids)} circle(s) to circles layer.")

    def _migrate_polygons_to_lines(self, poly_layer: QgsVectorLayer):
        """One-time migration: convert legacy polygon features to closed LineStrings.

        Each polygon's exterior ring becomes a LineString in the lines layer.
        The polygons layer is then removed from the project (the table stays in
        the GPKG file but is no longer loaded by the plugin).
        """
        if not self._layer_alive(self._lines_layer):
            QgsProject.instance().removeMapLayer(poly_layer.id())
            return

        new_feats = []
        for feat in poly_layer.getFeatures():
            geom = feat.geometry()
            cad  = feat["cad_layer"]
            if QgsWkbTypes.isMultiType(geom.wkbType()):
                rings = [p[0] for p in geom.asMultiPolygon() if p]
            else:
                poly = geom.asPolygon()
                rings = [poly[0]] if poly else []
            for ring in rings:
                if len(ring) < 2:
                    continue
                nf = QgsFeature(self._lines_layer.fields())
                nf.setGeometry(QgsGeometry.fromPolylineXY(ring))
                nf["cad_layer"] = cad
                new_feats.append(nf)

        if new_feats:
            if not self._lines_layer.isEditable():
                self._lines_layer.startEditing()
            self._lines_layer.addFeatures(new_feats)
            print(f"[UgSurv] Migrated {len(new_feats)} polygon feature(s) to lines layer")

        QgsProject.instance().removeMapLayer(poly_layer.id())


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
