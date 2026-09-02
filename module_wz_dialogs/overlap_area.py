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


def _read_source(source):
    import geopandas as gpd
    if '|layername=' in source:
        path, rest = source.split('|', 1)
        layername = rest.split('layername=', 1)[1].split('|')[0]
        return gpd.read_file(path, layer=layername)
    return gpd.read_file(source)


def _safe_to_crs(gdf, target_crs):
    if gdf.crs is None:
        return gdf.set_crs(target_crs)
    return gdf.to_crs(target_crs)


class _Worker(QObject):
    progress = pyqtSignal(str)
    # finished emits {fid: {'pct': float|None, 'area': float|None}}
    finished = pyqtSignal(object)
    error    = pyqtSignal(str)

    def __init__(self, fid_wkt_list, layer2_src, layer1_crs_wkt):
        super().__init__()
        self.fid_wkt_list   = fid_wkt_list
        self.layer2_src     = layer2_src
        self.layer1_crs_wkt = layer1_crs_wkt

    def run(self):
        try:
            import geopandas as gpd
            from shapely.wkt import loads as wkt_loads
            from shapely.ops import unary_union
            from pyproj import CRS

            self.progress.emit("Loading comparison layer…")
            gdf2 = _read_source(self.layer2_src)

            target_crs = CRS.from_wkt(self.layer1_crs_wkt)
            gdf2 = _safe_to_crs(gdf2, target_crs)

            self.progress.emit("Building spatial index for comparison layer…")
            geoms2  = [g for g in gdf2.geometry if g is not None and not g.is_empty]
            sindex2 = gpd.GeoDataFrame(geometry=geoms2, crs=target_crs).sindex

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

                cands = list(sindex2.intersection(geom1.bounds))
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
