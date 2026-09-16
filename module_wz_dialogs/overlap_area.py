# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt, QVariant, QThread, pyqtSignal, QObject
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
)
from qgis.PyQt import sip
from qgis.core import (
    QgsProject,
    QgsCoordinateTransform,
    QgsWkbTypes,
    QgsField,
    QgsMapLayerProxyModel,
)
from qgis.gui import QgsMapLayerComboBox

_COL_PCT    = 'overlap_pct'
_COL_AREA   = 'overlap_area'
_ORIG_FIELD = 'original_geometry'


class _WorkerCOA3(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(object)
    error    = pyqtSignal(str)

    def __init__(self, fid_wkt_list, comp_geoms):
        super().__init__()
        self.fid_wkt_list = fid_wkt_list  # list of (fid, wkt_str_or_None)
        self.comp_geoms   = comp_geoms    # list of valid shapely geoms (same process)

    def run(self):
        try:
            from shapely.strtree import STRtree
            from shapely.wkt import loads as wkt_loads
            from shapely.ops import unary_union

            tree        = STRtree(self.comp_geoms)
            n           = len(self.fid_wkt_list)
            results     = {}
            report_step = max(1, n // 20)

            self.progress.emit(f'Computing overlap for {n} features…')
            for i, (fid, wkt) in enumerate(self.fid_wkt_list):
                if i % report_step == 0:
                    self.progress.emit(f'Processing {i}/{n}  ({100 * i // n}%)…')

                if not wkt:
                    results[fid] = {'pct': None, 'area': None}
                    continue

                try:
                    target_sh = wkt_loads(str(wkt))
                except Exception:
                    results[fid] = {'pct': None, 'area': None}
                    continue

                if target_sh is None or target_sh.is_empty:
                    results[fid] = {'pct': None, 'area': None}
                    continue

                target_area = target_sh.area
                if target_area <= 0:
                    results[fid] = {'pct': 0.0, 'area': 0.0}
                    continue

                if not target_sh.is_valid:
                    target_sh = target_sh.buffer(0)

                hits = tree.query(target_sh)
                if not len(hits):
                    results[fid] = {'pct': 0.0, 'area': 0.0}
                    continue

                try:
                    cands      = [self.comp_geoms[j] for j in hits]
                    comp_local = unary_union(cands) if len(cands) > 1 else cands[0]
                    intersection = target_sh.intersection(comp_local)
                    ovl_area = intersection.area if not intersection.is_empty else 0.0
                except Exception:
                    try:
                        comp_local   = unary_union([g.buffer(0) for g in cands])
                        intersection = target_sh.buffer(0).intersection(comp_local)
                        ovl_area = intersection.area if not intersection.is_empty else 0.0
                    except Exception:
                        ovl_area = 0.0

                pct = min(100.0, 100.0 * ovl_area / target_area)
                results[fid] = {
                    'pct':  round(pct,      4),
                    'area': round(ovl_area, 4),
                }

            self.progress.emit(f'Done. Computed {n} feature(s).')
            self.finished.emit(results)

        except Exception as e:
            import traceback
            self.error.emit(f'{e}\n{traceback.format_exc()}')


class CalcOverlapAreaDock3(QDockWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('COA — Overlap Area')
        self._thread     = None
        self._worker     = None
        self._layer1_ref = None

        root   = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        H = 26

        def _layer_row(label_text):
            row   = QHBoxLayout()
            lbl   = QLabel(label_text)
            lbl.setFixedWidth(120)
            combo = QgsMapLayerComboBox()
            combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
            combo.setFixedHeight(H)
            row.addWidget(lbl)
            row.addWidget(combo)
            layout.addLayout(row)
            return combo

        self.cmb_layer1 = _layer_row('Layer 1:')

        src_row = QHBoxLayout()
        src_lbl = QLabel('Layer 1 geometry:')
        src_lbl.setFixedWidth(120)
        self.cmb_l1_source = QComboBox()
        self.cmb_l1_source.setFixedHeight(H)
        self.cmb_l1_source.addItem('original_geometry field', 'original')
        self.cmb_l1_source.addItem('Actual geometry',         'actual')
        src_row.addWidget(src_lbl)
        src_row.addWidget(self.cmb_l1_source)
        layout.addLayout(src_row)

        self.cmb_layer2 = _layer_row('Comparison layer:')

        self._status = QLabel('Ready.')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._run_btn = QPushButton('Run')
        self._run_btn.setFixedHeight(H + 4)
        self._run_btn.clicked.connect(self._run)
        btn_row.addWidget(self._run_btn)
        layout.addLayout(btn_row)

        self.setWidget(root)

    def _set_status(self, msg, color='green'):
        self._status.setStyleSheet(f'color: {color};')
        self._status.setText(msg)

    def _run(self):
        layer1 = self.cmb_layer1.currentLayer()
        layer2 = self.cmb_layer2.currentLayer()

        if not layer1:
            self._set_status('Layer 1 is required!', 'red'); return
        if not layer2:
            self._set_status('Comparison layer is required!', 'red'); return
        if layer1 == layer2:
            self._set_status('Layers must be different!', 'red'); return

        use_original = self.cmb_l1_source.currentData() == 'original'

        if use_original:
            if layer1.fields().indexOf(_ORIG_FIELD) == -1:
                self._set_status(f'Layer 1 has no "{_ORIG_FIELD}" field.', 'red'); return
            fid_wkt_list = [(feat.id(), feat[_ORIG_FIELD]) for feat in layer1.getFeatures()]
        else:
            fid_wkt_list = []
            for feat in layer1.getFeatures():
                geom = feat.geometry()
                fid_wkt_list.append((feat.id(), geom.asWkt() if (geom and not geom.isEmpty()) else None))

        if not fid_wkt_list:
            self._set_status('Layer 1 has no features.', 'red'); return

        l1_crs = layer1.crs()
        l2_crs = layer2.crs()
        xform  = QgsCoordinateTransform(l2_crs, l1_crs, QgsProject.instance()) if l2_crs != l1_crs else None

        from shapely.wkt import loads as sh_loads

        comp_geoms = []
        for feat in layer2.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            if xform:
                geom.transform(xform)
            try:
                g = sh_loads(geom.asWkt())
                if not g.is_valid:
                    g = g.buffer(0)
                comp_geoms.append(g)
            except Exception:
                pass

        if not comp_geoms:
            self._set_status('Comparison layer has no valid geometries.', 'red'); return

        self._layer1_ref = layer1
        self._run_btn.setEnabled(False)
        self._set_status('Running…', 'orange')

        self._thread = QThread()
        self._worker = _WorkerCOA3(fid_wkt_list, comp_geoms)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda msg: self._set_status(msg, 'orange'))
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
        layer = self._layer1_ref
        if layer is None or sip.isdeleted(layer):
            self._set_status('Layer was removed before results could be applied.', 'red')
            return

        provider = layer.dataProvider()
        for col_name in (_COL_PCT, _COL_AREA):
            if provider.fields().indexOf(col_name) == -1:
                provider.addAttributes([QgsField(col_name, QVariant.Double, 'double', 14, 4)])
                layer.updateFields()

        idx_pct  = layer.fields().indexOf(_COL_PCT)
        idx_area = layer.fields().indexOf(_COL_AREA)

        if idx_pct == -1 or idx_area == -1:
            self._set_status('Could not create output fields.', 'red')
            return

        attr_map = {}
        for fid, vals in results.items():
            if vals['pct'] is not None:
                attr_map[fid] = {idx_pct: vals['pct'], idx_area: vals['area']}

        provider.changeAttributeValues(attr_map)
        layer.updateFields()
        layer.triggerRepaint()

        n         = len(results)
        n_written = len(attr_map)
        n_nonzero = sum(1 for v in results.values() if v['pct'] is not None and v['pct'] > 0)
        self._set_status(
            f'Done. {n} features processed — {n_written} written, '
            f'{n_nonzero} with non-zero overlap.\n'
            f'Columns: {_COL_PCT}, {_COL_AREA}',
            'green',
        )

    def _on_error(self, msg):
        self._set_status(f'Error: {msg}', 'red')
