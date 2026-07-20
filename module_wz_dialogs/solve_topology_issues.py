# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore")

from PyQt5.QtCore import QThread, pyqtSignal, QObject
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QDoubleSpinBox,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel


def _read_source(source):
    import geopandas as gpd
    if '|layername=' in source:
        path, rest = source.split('|', 1)
        layername = rest.split('layername=', 1)[1].split('|')[0]
        return gpd.read_file(path, layer=layername)
    return gpd.read_file(source)


class _Worker(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, parcels_src, rivers_src, roads_src, surveyed_src, output_path,
                 river_buffer_m, road_buffer_m, min_overlap_pct, sliver_threshold_m2):
        super().__init__()
        self.parcels_src         = parcels_src
        self.rivers_src          = rivers_src
        self.roads_src           = roads_src    # may be None
        self.surveyed_src        = surveyed_src
        self.output_path         = output_path
        self.river_buffer_m      = river_buffer_m
        self.road_buffer_m       = road_buffer_m
        self.min_overlap_pct     = min_overlap_pct
        self.sliver_threshold_m2 = sliver_threshold_m2

    def run(self):
        try:
            from shapely.ops import unary_union

            self.progress.emit("Loading layers…")
            parcels      = _read_source(self.parcels_src)
            gdf_rivers   = _read_source(self.rivers_src)
            gdf_surveyed = _read_source(self.surveyed_src)

            target_crs = parcels.crs
            if target_crs is None:
                self.error.emit("Parcels layer has no CRS defined.")
                return

            gdf_rivers   = gdf_rivers.to_crs(target_crs)
            gdf_surveyed = gdf_surveyed.to_crs(target_crs)

            self.progress.emit(f"Buffering rivers by {self.river_buffer_m} m…")
            rivers_union = unary_union(list(gdf_rivers.geometry.buffer(self.river_buffer_m)))

            roads_union = None
            if self.roads_src:
                self.progress.emit(f"Buffering roads by {self.road_buffer_m} m…")
                import geopandas as gpd
                gdf_roads   = _read_source(self.roads_src).to_crs(target_crs)
                roads_union = unary_union(list(gdf_roads.geometry.buffer(self.road_buffer_m)))

            self.progress.emit("Building exclusion zone…")
            surveyed_union = unary_union(list(gdf_surveyed.geometry.buffer(0)))
            exclusion_zone = rivers_union.union(surveyed_union)
            if roads_union is not None:
                exclusion_zone = exclusion_zone.union(roads_union)

            self.progress.emit(f"Adjusting {len(parcels)} parcels…")
            parcels = parcels.copy()
            parcels["solve_code"]        = ""
            parcels["solve_comment"]     = ""
            parcels["geometry_adjusted"] = False

            CODE_CLEAN  = "CLEAN"
            CODE_RIVER  = "RIVER_DIFF"
            CODE_ROAD   = "ROAD_DIFF"
            CODE_LAND   = "LAND_DIFF"
            CODE_MULTI  = "MULTI_DIFF"
            CODE_MANUAL = "MANUAL"
            CODE_NULL   = "NULL_GEOM"
            CODE_SLIVER = "SLIVER"

            adjusted_geoms = []

            for idx, row in parcels.iterrows():
                geom     = row.geometry
                comments = []

                if geom is None or geom.is_empty:
                    parcels.at[idx, "solve_code"]    = CODE_NULL
                    parcels.at[idx, "solve_comment"] = "NULL or empty geometry — needs manual review"
                    adjusted_geoms.append(geom)
                    continue

                hits_river    = geom.intersects(rivers_union)
                hits_road     = roads_union is not None and geom.intersects(roads_union)
                hits_surveyed = geom.intersects(surveyed_union)

                if hits_river:
                    comments.append(f"overlaps buffered river ({self.river_buffer_m} m buffer)")
                if hits_road:
                    comments.append(f"overlaps buffered road ({self.road_buffer_m} m buffer)")
                if hits_surveyed:
                    comments.append("overlaps surveyed land")

                if comments:
                    if self.min_overlap_pct > 0 and geom.area > 0:
                        overlap_pct = geom.intersection(exclusion_zone).area / geom.area * 100
                        if overlap_pct < self.min_overlap_pct:
                            parcels.at[idx, "solve_code"] = CODE_CLEAN
                            adjusted_geoms.append(geom)
                            continue

                    adjusted = geom.difference(exclusion_zone)

                    if adjusted.is_empty:
                        parcels.at[idx, "solve_code"]    = CODE_MANUAL
                        parcels.at[idx, "solve_comment"] = (
                            "FULLY INSIDE exclusion zone ("
                            + "; ".join(comments)
                            + ") — manual fix required"
                        )
                        adjusted_geoms.append(geom)
                    else:
                        hits_count = sum([hits_river, hits_road, hits_surveyed])
                        if hits_count > 1:
                            code = CODE_MULTI
                        elif hits_river:
                            code = CODE_RIVER
                        elif hits_road:
                            code = CODE_ROAD
                        else:
                            code = CODE_LAND

                        parcels.at[idx, "solve_code"]        = code
                        parcels.at[idx, "solve_comment"]     = "Adjusted: " + "; ".join(comments)
                        parcels.at[idx, "geometry_adjusted"] = True
                        adjusted_geoms.append(adjusted)
                else:
                    parcels.at[idx, "solve_code"] = CODE_CLEAN
                    adjusted_geoms.append(geom)

            import geopandas as gpd
            parcels = parcels.set_geometry(adjusted_geoms)
            result  = gpd.GeoDataFrame(parcels, crs=target_crs)
            result  = result.explode(index_parts=False).reset_index(drop=True)

            thr = self.sliver_threshold_m2
            sliver_mask = result.geometry.area <= thr
            result.loc[sliver_mask, "solve_code"]    = CODE_SLIVER
            result.loc[sliver_mask, "solve_comment"] = (
                result.loc[sliver_mask, "solve_comment"]
                .str.rstrip()
                .apply(lambda s: (s + " | " if s else "") + f"SLIVER (<={thr} m²) — manual fix required")
            )

            self.progress.emit("Saving output…")
            result.to_file(self.output_path, driver="GPKG")

            counts  = result["solve_code"].value_counts()
            summary = (
                f"Done.  Total features: {len(result)}\n"
                f"  {CODE_CLEAN}:  {counts.get(CODE_CLEAN, 0)}\n"
                f"  {CODE_RIVER}: {counts.get(CODE_RIVER, 0)}\n"
                f"  {CODE_ROAD}:  {counts.get(CODE_ROAD, 0)}\n"
                f"  {CODE_LAND}:  {counts.get(CODE_LAND, 0)}\n"
                f"  {CODE_MULTI}: {counts.get(CODE_MULTI, 0)}\n"
                f"  {CODE_MANUAL}: {counts.get(CODE_MANUAL, 0)}  ← manual fix required\n"
                f"  {CODE_SLIVER}: {counts.get(CODE_SLIVER, 0)}  ← sliver\n"
                f"  {CODE_NULL}:  {counts.get(CODE_NULL, 0)}  ← null geometry\n"
                f"  Saved → {self.output_path}"
            )
            self.finished.emit(summary)

        except Exception as e:
            self.error.emit(str(e))


class SolveTopologyDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Solve Topology Issues')
        self._thread = None
        self._worker = None

        inputheight = 25

        root   = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        def layer_row(label_text, allow_none=False):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(120)
            combo = QgsMapLayerComboBox()
            combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
            combo.setFixedHeight(inputheight)
            if allow_none:
                combo.setAllowEmptyLayer(True)
                combo.setCurrentIndex(0)
            row.addWidget(lbl)
            row.addWidget(combo)
            layout.addLayout(row)
            return combo

        self.cmb_parcels  = layer_row('Parcels to solve:')
        self.cmb_rivers   = layer_row('Rivers layer:')
        self.cmb_roads    = layer_row('Roads layer:', allow_none=True)
        self.cmb_surveyed = layer_row('Surveyed land:')

        # Output path — stays as a file picker
        out_row = QHBoxLayout()
        lbl_out = QLabel('Output path:')
        lbl_out.setFixedWidth(120)
        self.wgt_output = QgsFileWidget()
        self.wgt_output.setStorageMode(QgsFileWidget.SaveFile)
        self.wgt_output.setFilter("GeoPackage (*.gpkg)")
        out_row.addWidget(lbl_out)
        out_row.addWidget(self.wgt_output)
        layout.addLayout(out_row)

        def spin_row(label_text, lo, hi, default, decimals=1, suffix=''):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(180)
            spn = QDoubleSpinBox()
            spn.setRange(lo, hi)
            spn.setValue(default)
            spn.setDecimals(decimals)
            spn.setFixedHeight(inputheight)
            if suffix:
                spn.setSuffix(suffix)
            row.addWidget(lbl)
            row.addWidget(spn)
            row.addStretch()
            layout.addLayout(row)
            return spn

        self.spn_river_buf = spin_row('River buffer (m):',        0, 10000, 5,  1)
        self.spn_road_buf  = spin_row('Road buffer (m):',         0, 10000, 5,  1)
        self.spn_overlap   = spin_row('Min overlap % to adjust:', 0,   100, 0,  1, ' %')
        self.spn_sliver    = spin_row('Sliver threshold (m²):',   0,  1000, 1,  2)

        self.response = QLabel('Ready.')
        self.response.setWordWrap(True)
        layout.addWidget(self.response)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.run_btn = QPushButton('Run')
        btn_row.addWidget(self.run_btn)
        layout.addLayout(btn_row)

        self.setWidget(root)
        self.run_btn.clicked.connect(self._run)

    def _set_status(self, message, color="green"):
        self.response.setStyleSheet(f"color: {color};")
        self.response.setText(message)

    def _run(self):
        parcels_layer  = self.cmb_parcels.currentLayer()
        rivers_layer   = self.cmb_rivers.currentLayer()
        roads_layer    = self.cmb_roads.currentLayer()   # optional
        surveyed_layer = self.cmb_surveyed.currentLayer()
        output_path    = self.wgt_output.filePath()

        if not parcels_layer:
            self._set_status("Parcels layer is required!", "red"); return
        if not rivers_layer:
            self._set_status("Rivers layer is required!", "red"); return
        if not surveyed_layer:
            self._set_status("Surveyed land layer is required!", "red"); return
        if not output_path:
            self._set_status("Output path is required!", "red"); return

        self.run_btn.setEnabled(False)
        self._set_status("Running…", "orange")

        self._thread = QThread()
        self._worker = _Worker(
            parcels_src          = parcels_layer.source(),
            rivers_src           = rivers_layer.source(),
            roads_src            = roads_layer.source() if roads_layer else None,
            surveyed_src         = surveyed_layer.source(),
            output_path          = output_path,
            river_buffer_m       = self.spn_river_buf.value(),
            road_buffer_m        = self.spn_road_buf.value(),
            min_overlap_pct      = self.spn_overlap.value(),
            sliver_threshold_m2  = self.spn_sliver.value(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda msg: self._set_status(msg, "orange"))
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(lambda: self.run_btn.setEnabled(True))
        self._thread.start()

    def _on_finished(self, summary):
        self._set_status(summary, "green")

    def _on_error(self, msg):
        self._set_status(f"Error: {msg}", "red")
