# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore")

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject, QVariant
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton,
)
from qgis.core import QgsMapLayerProxyModel, QgsField
from qgis.gui import QgsMapLayerComboBox

_COL_PCT  = 'overlap_pct'
_COL_AREA = 'overlap_area'


class _STRIndex:
    """Spatial index backed by shapely STRtree; drop-in for geopandas .sindex.intersection()."""

    def __init__(self, geoms):
        from shapely.strtree import STRtree
        self._real_idxs = [i for i, g in enumerate(geoms)
                           if g is not None and not g.is_empty]
        valid = [geoms[i] for i in self._real_idxs]
        self._tree = STRtree(valid) if valid else None

    def intersection(self, bounds):
        if self._tree is None:
            return []
        from shapely.geometry import box
        hits = self._tree.query(box(*bounds))
        return [self._real_idxs[int(h)] for h in hits]


def _read_source(source):
    """Read a vector file via OGR. Returns (geoms, srs)."""
    from osgeo import ogr
    from shapely.wkt import loads as wkt_loads

    path, layername = source, None
    if '|layername=' in source:
        path, rest = source.split('|', 1)
        layername  = rest.split('layername=', 1)[1].split('|')[0]

    ds = ogr.Open(path, 0)
    if ds is None:
        raise IOError(f"Cannot open: {path}")
    lyr = ds.GetLayerByName(layername) if layername else ds.GetLayer(0)
    if lyr is None:
        raise IOError(f"Layer '{layername}' not found in {path}")

    srs   = lyr.GetSpatialRef()
    geoms = []
    for feat in lyr:
        geom_ref = feat.GetGeometryRef()
        if geom_ref is not None:
            geom_ref.FlattenTo2D()
            geoms.append(wkt_loads(geom_ref.ExportToWkt()))
        else:
            geoms.append(None)

    ds = None
    return geoms, srs


def _reproject_geoms(geoms, from_srs, to_srs):
    """Reproject shapely geometries; returns list unchanged if CRS is the same."""
    if from_srs is None or to_srs is None:
        return geoms
    if from_srs.IsSame(to_srs):
        return geoms
    from osgeo import ogr, osr
    from shapely.wkt import loads as wkt_loads
    ct  = osr.CoordinateTransformation(from_srs, to_srs)
    out = []
    for g in geoms:
        if g is None or g.is_empty:
            out.append(g)
            continue
        ogr_g = ogr.CreateGeometryFromWkt(g.wkt)
        if ogr_g is None:
            out.append(g)
            continue
        ogr_g.Transform(ct)
        out.append(wkt_loads(ogr_g.ExportToWkt()))
    return out


class _Worker(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(object)
    error    = pyqtSignal(str)

    def __init__(self, fid_wkt_list, layer2_src, layer1_crs_wkt):
        super().__init__()
        self.fid_wkt_list   = fid_wkt_list
        self.layer2_src     = layer2_src
        self.layer1_crs_wkt = layer1_crs_wkt

    def run(self):
        try:
            from shapely.wkt import loads as wkt_loads
            from shapely.ops import unary_union
            from osgeo import osr

            target_srs = osr.SpatialReference()
            target_srs.ImportFromWkt(self.layer1_crs_wkt)

            self.progress.emit("Loading comparison layer…")
            geoms2_raw, src_srs = _read_source(self.layer2_src)
            geoms2_raw = _reproject_geoms(geoms2_raw, src_srs, target_srs)
            geoms2  = [g for g in geoms2_raw if g is not None and not g.is_empty]

            self.progress.emit("Building spatial index for comparison layer…")
            sindex2 = _STRIndex(geoms2)

            n           = len(self.fid_wkt_list)
            results     = {}
            report_step = max(1, n // 20)

            self.progress.emit(f"Computing overlap for {n} features…")
            for i, (fid, wkt) in enumerate(self.fid_wkt_list):
                if i % report_step == 0:
                    self.progress.emit(f"Processing {i}/{n}  ({100 * i // n}%)…")

                if not wkt:
                    results[fid] = {'pct': None, 'area': None}
                    continue

                try:
                    geom1 = wkt_loads(wkt)
                except Exception:
                    results[fid] = {'pct': None, 'area': None}
                    continue

                if geom1 is None or geom1.is_empty:
                    results[fid] = {'pct': None, 'area': None}
                    continue

                feature_area = geom1.area
                if feature_area <= 0:
                    results[fid] = {'pct': 0.0, 'area': 0.0}
                    continue

                cands = sindex2.intersection(geom1.bounds)
                if not cands:
                    results[fid] = {'pct': 0.0, 'area': 0.0}
                    continue

                try:
                    cand_geoms   = [geoms2[c] for c in cands]
                    layer2_local = (
                        unary_union(cand_geoms) if len(cand_geoms) > 1 else cand_geoms[0]
                    )
                    intersection = geom1.intersection(layer2_local)
                    ovl_area     = intersection.area if not intersection.is_empty else 0.0
                except Exception:
                    ovl_area = 0.0

                pct = min(100.0, 100.0 * ovl_area / feature_area)
                results[fid] = {
                    'pct':  round(pct,      4),
                    'area': round(ovl_area, 4),
                }

            self.progress.emit(f"Done. Computed {n} feature(s).")
            self.finished.emit(results)

        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


class OverlapAreaDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Overlap Area')
        self._thread = None
        self._worker = None

        root   = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        H = 26

        def _layer_row(label_text):
            row   = QHBoxLayout()
            lbl   = QLabel(label_text)
            lbl.setFixedWidth(130)
            combo = QgsMapLayerComboBox()
            combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
            combo.setFixedHeight(H)
            row.addWidget(lbl)
            row.addWidget(combo)
            layout.addLayout(row)
            return combo

        self.cmb_layer1 = _layer_row("Dataset1 (target):")
        self.cmb_layer2 = _layer_row("Comparison layer:")

        self._status = QLabel("Ready.")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._run_btn = QPushButton("Run")
        self._run_btn.setFixedHeight(H + 4)
        self._run_btn.clicked.connect(self._run)
        btn_row.addWidget(self._run_btn)
        layout.addLayout(btn_row)

        self.setWidget(root)

    def _set_status(self, message, color="green"):
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(message)

    def _run(self):
        layer1 = self.cmb_layer1.currentLayer()
        layer2 = self.cmb_layer2.currentLayer()

        if not layer1:
            self._set_status("Dataset1 is required!", "red"); return
        if not layer2:
            self._set_status("Comparison layer is required!", "red"); return
        if layer1 == layer2:
            self._set_status("Dataset1 and comparison layer must be different!", "red"); return

        fid_wkt_list = []
        for feat in layer1.getFeatures():
            geom = feat.geometry()
            wkt  = geom.asWkt() if (geom and not geom.isEmpty()) else None
            fid_wkt_list.append((feat.id(), wkt))

        if not fid_wkt_list:
            self._set_status("Dataset1 has no features.", "red"); return

        self._run_btn.setEnabled(False)
        self._set_status("Running…", "orange")
        self._layer1_ref = layer1

        self._thread = QThread()
        self._worker = _Worker(
            fid_wkt_list   = fid_wkt_list,
            layer2_src     = layer2.source(),
            layer1_crs_wkt = layer1.crs().toWkt(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda msg: self._set_status(msg, "orange"))
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _cleanup_thread(self):
        self._run_btn.setEnabled(True)
        if self._thread:
            self._thread.deleteLater()
            self._thread = None
        if self._worker:
            self._worker.deleteLater()
            self._worker = None

    def _on_finished(self, results: dict):
        layer = getattr(self, '_layer1_ref', None)
        if layer is None or sip.isdeleted(layer):
            self._set_status("Layer was removed before results could be applied.", "red")
            return

        provider = layer.dataProvider()

        # Create any missing fields first, then resolve indices once both exist.
        # Check against the provider's own field list (the real DB schema) to avoid
        # a stale layer field-cache triggering a duplicate-column OGR error.
        for col_name in (_COL_PCT, _COL_AREA):
            if provider.fields().indexOf(col_name) == -1:
                provider.addAttributes([QgsField(col_name, QVariant.Double, 'double', 14, 4)])
                layer.updateFields()

        idx_pct  = layer.fields().indexOf(_COL_PCT)
        idx_area = layer.fields().indexOf(_COL_AREA)

        if idx_pct == -1 or idx_area == -1:
            self._set_status("Could not create one or both output fields.", "red")
            return

        attr_map = {}
        for fid, vals in results.items():
            if vals['pct'] is not None:
                attr_map[fid] = {idx_pct: vals['pct'], idx_area: vals['area']}

        provider.changeAttributeValues(attr_map)
        layer.updateFields()
        layer.triggerRepaint()

        n         = len(results)
        n_nonzero = sum(1 for v in results.values() if v['pct'] is not None and v['pct'] > 0)
        self._set_status(
            f"Done. {n} features processed — "
            f"{n_nonzero} with non-zero overlap.\n"
            f"Columns written: {_COL_PCT}, {_COL_AREA}",
            "green",
        )

    def _on_error(self, msg):
        self._set_status(f"Error: {msg}", "red")
