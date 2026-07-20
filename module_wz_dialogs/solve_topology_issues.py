# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore")

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal, QObject
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QDoubleSpinBox,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel

# ── Constants (mirror the source script defaults) ────────────────────────────
_COORD_TOLERANCE_M = 0.2   # metres — point-match tolerance for geometry comparison
_MIN_MATCH_RATIO   = 0.50  # at least 50 % of vertices must match
_NBR_MANUAL_FRAC   = 0.60  # neighbour-only overlap > 60 % → manual review


# ── Helpers (ported verbatim from "Solve deffered with comments.py") ─────────

def _read_source(source):
    import geopandas as gpd
    if '|layername=' in source:
        path, rest = source.split('|', 1)
        layername = rest.split('layername=', 1)[1].split('|')[0]
        return gpd.read_file(path, layer=layername)
    return gpd.read_file(source)


def _get_coords_np(geom):
    from shapely.geometry import Polygon, MultiPolygon
    if isinstance(geom, Polygon):
        return np.array(geom.exterior.coords)[:, :2]
    if isinstance(geom, MultiPolygon):
        parts = [np.array(p.exterior.coords)[:, :2] for p in geom.geoms]
        return np.vstack(parts) if parts else np.empty((0, 2))
    return np.empty((0, 2))


def _coords_mostly_match(geom_a, geom_b):
    pts_a = _get_coords_np(geom_a)
    pts_b = _get_coords_np(geom_b)
    if len(pts_a) == 0 or len(pts_b) == 0:
        return False
    try:
        from scipy.spatial import cKDTree
        dists, _ = cKDTree(pts_b).query(pts_a)
        matched = int(np.sum(dists <= _COORD_TOLERANCE_M))
    except ImportError:
        matched = 0
        for s in range(0, len(pts_a), 200):
            blk  = pts_a[s:s + 200]
            diff = blk[:, None, :] - pts_b[None, :, :]
            d    = np.hypot(diff[..., 0], diff[..., 1]).min(axis=1)
            matched += int(np.sum(d <= _COORD_TOLERANCE_M))
    return (matched / len(pts_a)) >= _MIN_MATCH_RATIO


def _largest_part(geom):
    from shapely.geometry import MultiPolygon
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    return geom


def _find_laf_col(gdf):
    for col in gdf.columns:
        if 'laf' in col.lower():
            return col
    return None


# ── Background worker ─────────────────────────────────────────────────────────

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
        self.min_overlap_pct     = min_overlap_pct   # used as neighbour-overlap manual threshold
        self.sliver_threshold_m2 = sliver_threshold_m2

    def run(self):
        try:
            import geopandas as gpd
            from shapely.ops import unary_union
            from shapely.geometry import box, MultiPolygon

            # ── Load ──────────────────────────────────────────────────────────
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

            # ── LAF column detection ──────────────────────────────────────────
            laf_col_p = _find_laf_col(parcels)
            laf_col_s = _find_laf_col(gdf_surveyed)
            if laf_col_p and laf_col_s:
                self.progress.emit(f"LAF columns: '{laf_col_p}' | '{laf_col_s}'")
            else:
                self.progress.emit("No LAF column found — LAF matching skipped.")

            # ── Build spatial indices ─────────────────────────────────────────
            self.progress.emit(f"Buffering rivers by {self.river_buffer_m} m…")
            buf_river_geoms = list(gdf_rivers.geometry.buffer(self.river_buffer_m))
            rivers_sindex   = gpd.GeoDataFrame(geometry=buf_river_geoms, crs=target_crs).sindex

            buf_road_geoms = None
            roads_sindex   = None
            if self.roads_src:
                self.progress.emit(f"Buffering roads by {self.road_buffer_m} m…")
                gdf_roads      = _read_source(self.roads_src).to_crs(target_crs)
                buf_road_geoms = list(gdf_roads.geometry.buffer(self.road_buffer_m))
                roads_sindex   = gpd.GeoDataFrame(geometry=buf_road_geoms, crs=target_crs).sindex

            self.progress.emit("Pre-repairing surveyed geometries…")
            surveyed_repaired = [g.buffer(0) for g in gdf_surveyed.geometry]
            surveyed_sindex   = gdf_surveyed.sindex

            # Pre-extract LAF as plain arrays — avoids pandas overhead in the loop
            parcel_laf_arr = (parcels[laf_col_p].astype(str).str.strip().values
                              if laf_col_p else None)
            surv_laf_arr   = (gdf_surveyed[laf_col_s].astype(str).str.strip().values
                              if laf_col_s else None)

            # ── Solve codes ───────────────────────────────────────────────────
            CODE_CLEAN     = "CLEAN"
            CODE_SAME_LAF  = "SAME_LAF"
            CODE_SAME_GEOM = "SAME_GEOM"
            CODE_RIVER     = "RIVER_DIFF"
            CODE_ROAD      = "ROAD_DIFF"
            CODE_LAND      = "LAND_DIFF"
            CODE_MULTI     = "MULTI_DIFF"
            CODE_MANUAL    = "MANUAL"
            CODE_NULL      = "NULL_GEOM"
            CODE_SLIVER    = "SLIVER"

            n = len(parcels)
            solve_codes    = [''] * n
            solve_comments = [''] * n
            geom_adj_flags = [False] * n
            adjusted_geoms = [None] * n
            parcel_geoms   = parcels.geometry.values
            report_step    = max(1, n // 20)

            # neighbour-only overlap fraction that triggers manual review
            nbr_manual_frac = (self.min_overlap_pct / 100.0
                               if self.min_overlap_pct > 0 else _NBR_MANUAL_FRAC)

            # ── Main loop ─────────────────────────────────────────────────────
            self.progress.emit(f"Processing {n} parcels…")
            for i, geom in enumerate(parcel_geoms):
                if i % report_step == 0:
                    self.progress.emit(f"Processing {i}/{n}  ({100 * i // n}%)…")

                # 1. Null / empty
                if geom is None or geom.is_empty:
                    solve_codes[i]    = CODE_NULL
                    solve_comments[i] = "NULL or empty geometry — needs manual review"
                    adjusted_geoms[i] = geom
                    continue

                # 2. Find spatially overlapping surveyed parcels
                cand_surv = list(surveyed_sindex.intersection(geom.bounds))
                if cand_surv:
                    cand_surv = [ci for ci in cand_surv
                                 if surveyed_repaired[ci].intersects(geom)]

                # 3. Classify each surveyed overlap: same-parcel match vs real conflict
                matched_notes  = []
                conflict_geoms = []
                for ci in cand_surv:
                    surv_geom  = surveyed_repaired[ci]
                    is_matched = False
                    # 3a. LAF match
                    if parcel_laf_arr is not None and surv_laf_arr is not None:
                        p_laf = parcel_laf_arr[i]
                        s_laf = surv_laf_arr[ci]
                        if p_laf and s_laf and p_laf != 'nan' and p_laf == s_laf:
                            matched_notes.append(f"same LAF ({p_laf})")
                            is_matched = True
                    # 3b. Coordinate match (≥ 50 % of vertices within 0.2 m)
                    if not is_matched and _coords_mostly_match(geom, surv_geom):
                        matched_notes.append(f"same geometry (≤{_COORD_TOLERANCE_M} m)")
                        is_matched = True
                    if not is_matched:
                        conflict_geoms.append(surv_geom)

                # 4. Per-parcel river lookup (only nearby rivers, not the global union)
                cand_river = list(rivers_sindex.intersection(geom.bounds))
                if cand_river:
                    cand_river = [ri for ri in cand_river
                                  if buf_river_geoms[ri].intersects(geom)]
                hits_river = len(cand_river) > 0
                local_rivers = (
                    (buf_river_geoms[cand_river[0]]
                     if len(cand_river) == 1
                     else unary_union([buf_river_geoms[ri] for ri in cand_river]))
                    if hits_river else None
                )

                # 5. Per-parcel road lookup
                hits_road   = False
                local_roads = None
                if roads_sindex is not None:
                    cand_road = list(roads_sindex.intersection(geom.bounds))
                    if cand_road:
                        cand_road = [ri for ri in cand_road
                                     if buf_road_geoms[ri].intersects(geom)]
                    hits_road = len(cand_road) > 0
                    if hits_road:
                        local_roads = (
                            buf_road_geoms[cand_road[0]]
                            if len(cand_road) == 1
                            else unary_union([buf_road_geoms[ri] for ri in cand_road])
                        )

                hits_neighbour = len(conflict_geoms) > 0
                has_match      = len(matched_notes) > 0
                hits_linear    = hits_river or hits_road

                comments = []
                if hits_river:
                    comments.append(f"overlaps buffered river ({self.river_buffer_m} m buffer)")
                if hits_road:
                    comments.append(f"overlaps buffered road ({self.road_buffer_m} m buffer)")
                if hits_neighbour:
                    comments.append("overlaps neighbouring surveyed land")
                if has_match:
                    comments.append("skipped: " + "; ".join(matched_notes))

                # 6. No real conflicts at all
                if not hits_linear and not hits_neighbour:
                    if has_match:
                        code = CODE_SAME_LAF if any("LAF" in n for n in matched_notes) else CODE_SAME_GEOM
                        solve_codes[i]    = code
                        solve_comments[i] = "No real conflict — " + "; ".join(matched_notes)
                    else:
                        solve_codes[i] = CODE_CLEAN
                    adjusted_geoms[i] = geom
                    continue

                # 7. Build parcel-specific exclusion zone from local geometries only
                if local_rivers and local_roads:
                    local_linear = local_rivers.union(local_roads)
                elif local_rivers:
                    local_linear = local_rivers
                elif local_roads:
                    local_linear = local_roads
                else:
                    local_linear = None

                if conflict_geoms:
                    nbr_union = (conflict_geoms[0]
                                 if len(conflict_geoms) == 1
                                 else unary_union(conflict_geoms))
                    parcel_exclusion = local_linear.union(nbr_union) if local_linear else nbr_union
                else:
                    parcel_exclusion = local_linear

                # Clip to parcel bbox — avoids operating on geometry far outside the parcel
                parcel_exclusion = parcel_exclusion.intersection(box(*geom.bounds))

                # 8. Neighbour-only conflicts: flag as manual if overlap > threshold
                if not hits_linear:
                    overlap_frac = geom.intersection(parcel_exclusion).area / geom.area
                    if overlap_frac > nbr_manual_frac:
                        solve_codes[i]    = CODE_MANUAL
                        solve_comments[i] = (
                            f"OVERLAP {overlap_frac * 100:.0f}% > {nbr_manual_frac * 100:.0f}% of parcel "
                            "(" + "; ".join(comments) + ") — manual fix required"
                        )
                        adjusted_geoms[i] = geom
                        continue

                # 9. Apply difference
                adjusted = geom.difference(parcel_exclusion)

                if adjusted.is_empty:
                    solve_codes[i]    = CODE_MANUAL
                    solve_comments[i] = (
                        "FULLY INSIDE exclusion zone ("
                        + "; ".join(comments) + ") — manual fix required"
                    )
                    adjusted_geoms[i] = geom
                else:
                    # Determine code
                    if (hits_river and hits_road) or (hits_linear and hits_neighbour):
                        code = CODE_MULTI
                    elif hits_river:
                        code = CODE_RIVER
                    elif hits_road:
                        code = CODE_ROAD
                    else:
                        code = CODE_LAND

                    if hits_linear and isinstance(adjusted, MultiPolygon):
                        # River/road cut: keep largest piece
                        n_parts  = len(list(adjusted.geoms))
                        adjusted = _largest_part(adjusted)
                        comments.append(f"kept largest of {n_parts} pieces (linear cut)")
                        solve_codes[i]    = code
                        solve_comments[i] = "Adjusted: " + "; ".join(comments)
                        geom_adj_flags[i] = True
                        adjusted_geoms[i] = adjusted
                    elif hits_neighbour and isinstance(adjusted, MultiPolygon) and not hits_linear:
                        # Neighbour split into disconnected pieces → manual
                        solve_codes[i]    = CODE_MANUAL
                        solve_comments[i] = (
                            "SPLIT into multiple parts by neighbouring parcel ("
                            + "; ".join(comments) + ") — manual fix required"
                        )
                        adjusted_geoms[i] = geom
                    else:
                        solve_codes[i]    = code
                        solve_comments[i] = "Adjusted: " + "; ".join(comments)
                        geom_adj_flags[i] = True
                        adjusted_geoms[i] = adjusted

            # ── Rebuild GeoDataFrame ──────────────────────────────────────────
            self.progress.emit(f"Processing {n}/{n} (100%)… building output…")
            parcels = parcels.copy()
            parcels["solve_code"]        = solve_codes
            parcels["solve_comment"]     = solve_comments
            parcels["geometry_adjusted"] = geom_adj_flags
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

            # ── Save ─────────────────────────────────────────────────────────
            self.progress.emit("Saving output…")
            result.to_file(self.output_path, driver="GPKG")

            counts  = result["solve_code"].value_counts()
            summary = (
                f"Done.  Total features: {len(result)}\n"
                f"  {CODE_CLEAN}:     {counts.get(CODE_CLEAN, 0)}\n"
                f"  {CODE_SAME_LAF}:  {counts.get(CODE_SAME_LAF, 0)}  — same LAF, not touched\n"
                f"  {CODE_SAME_GEOM}: {counts.get(CODE_SAME_GEOM, 0)}  — same geometry, not touched\n"
                f"  {CODE_RIVER}:     {counts.get(CODE_RIVER, 0)}\n"
                f"  {CODE_ROAD}:      {counts.get(CODE_ROAD, 0)}\n"
                f"  {CODE_LAND}:      {counts.get(CODE_LAND, 0)}\n"
                f"  {CODE_MULTI}:     {counts.get(CODE_MULTI, 0)}\n"
                f"  {CODE_MANUAL}:    {counts.get(CODE_MANUAL, 0)}  ← manual fix required\n"
                f"  {CODE_SLIVER}:    {counts.get(CODE_SLIVER, 0)}  ← sliver\n"
                f"  {CODE_NULL}:      {counts.get(CODE_NULL, 0)}  ← null geometry\n"
                f"  Saved → {self.output_path}"
            )
            self.finished.emit(summary)

        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── Dock widget (UI untouched) ────────────────────────────────────────────────

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
        self.spn_overlap   = spin_row('Min overlap % to adjust:', 0,   100, 50,  1, ' %')
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
        self._output_path = output_path

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
        self._load_output()

    def _load_output(self):
        import os
        from qgis.core import QgsVectorLayer, QgsProject
        path = getattr(self, '_output_path', None)
        if not path or not os.path.exists(path):
            return
        layer_name = os.path.splitext(os.path.basename(path))[0]
        uri   = f"{path}|layername={layer_name}"
        layer = QgsVectorLayer(uri, layer_name, "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)

    def _on_error(self, msg):
        self._set_status(f"Error: {msg}", "red")
