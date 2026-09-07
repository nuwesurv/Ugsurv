# -*- coding: utf-8 -*-
import os
import warnings
warnings.filterwarnings("ignore")

import numpy as np
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject, Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QDoubleSpinBox, QTabWidget,
    QGroupBox, QLineEdit, QCheckBox, QSplitter,
    QTreeWidget, QTreeWidgetItem, QTextBrowser, QComboBox,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel, QgsProject, QgsRectangle

# ── Constants ─────────────────────────────────────────────────────────────────
_COORD_TOLERANCE_M = 0.2
_MIN_MATCH_RATIO   = 0.50
_NBR_MANUAL_FRAC   = 0.60


# ── Spatial index (shapely STRtree — no geopandas / pyarrow) ─────────────────

class _STRIndex:
    """Thin wrapper around shapely STRtree matching the geopandas .sindex.intersection() API."""

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


# ── File I/O (OGR — no geopandas / pyarrow) ──────────────────────────────────

def _read_source(source):
    """
    Open a vector source string via OGR.
    Returns (geoms, attrs, srs, field_names, field_types) where
      geoms       : list[shapely geometry | None]
      attrs       : list[dict]
      srs         : ogr.SpatialReference | None
      field_names : list[str]
      field_types : list[(name, ogr_type, ogr_subtype)]
    """
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

    srs  = lyr.GetSpatialRef()
    defn = lyr.GetLayerDefn()
    field_names, field_types = [], []
    for i in range(defn.GetFieldCount()):
        fd = defn.GetFieldDefn(i)
        field_names.append(fd.GetName())
        field_types.append((fd.GetName(), fd.GetType(), fd.GetSubType()))

    geoms, attrs = [], []
    for feat in lyr:
        geom_ref = feat.GetGeometryRef()
        if geom_ref is not None:
            geom_ref.FlattenTo2D()
            g = wkt_loads(geom_ref.ExportToWkt())
        else:
            g = None
        geoms.append(g)
        attrs.append({f: feat.GetField(f) for f in field_names})

    ds = None
    return geoms, attrs, srs, field_names, field_types


def _reproject_geoms(geoms, from_srs, to_srs):
    """Reproject shapely geometries; returns list unchanged when CRS is the same."""
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


def _write_gpkg(output_path, geoms, attr_rows, field_specs, srs, layer_name=None):  # noqa: C901
    """
    Write features to a GeoPackage via OGR.
    field_specs : list of (name, ogr_type, ogr_subtype_or_None)
    attr_rows   : list of dicts; '_geometry' key is silently skipped
    """
    from osgeo import ogr

    if layer_name is None:
        layer_name = os.path.splitext(os.path.basename(output_path))[0]

    drv = ogr.GetDriverByName('GPKG')
    if os.path.exists(output_path):
        drv.DeleteDataSource(output_path)

    ds  = drv.CreateDataSource(output_path)
    lyr = ds.CreateLayer(layer_name, srs=srs, geom_type=ogr.wkbMultiPolygon)

    for name, otype, osubtype in field_specs:
        fd = ogr.FieldDefn(name, otype)
        if osubtype is not None:
            fd.SetSubType(osubtype)
        lyr.CreateField(fd)

    field_names = [name for name, _, _ in field_specs]
    defn = lyr.GetLayerDefn()

    for g, row in zip(geoms, attr_rows):
        feat = ogr.Feature(defn)
        if g is not None and not g.is_empty:
            feat.SetGeometry(ogr.CreateGeometryFromWkt(g.wkt))
        for name in field_names:
            val = row.get(name)
            if val is None:
                continue
            if isinstance(val, bool):
                feat.SetField(name, int(val))
            else:
                try:
                    feat.SetField(name, val)
                except Exception:
                    feat.SetField(name, str(val))
        lyr.CreateFeature(feat)

    ds = None


# ── Overlap comment ───────────────────────────────────────────────────────────

_COMMENT_ORDER = ['road', 'river', 'waterbody', 'Surveyed land']


def _make_overlap_comment(overlap_set):
    ordered = [n for n in _COMMENT_ORDER if n in overlap_set]
    k = len(ordered)
    if k == 0: return "Plot is fine."
    if k == 1: return f"Overlap {ordered[0]}."
    if k == 2: return f"Overlap {ordered[0]} and {ordered[1]}."
    if k == 3: return f"Overlap {ordered[0]}, {ordered[1]} and {ordered[2]}."
    return f"Overlap {ordered[0]}, {ordered[1]}, {ordered[2]} and {ordered[3]}."


# ── Geometry helpers ──────────────────────────────────────────────────────────

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


def _find_laf_col(field_names):
    for col in field_names:
        if 'laf' in col.lower():
            return col
    return None


# ── Background worker ─────────────────────────────────────────────────────────

class _Worker(QObject):
    progress    = pyqtSignal(str)
    finished    = pyqtSignal(object)
    error       = pyqtSignal(str)
    constraints = pyqtSignal(str)

    def __init__(self, parcels_src, rivers_src, roads_src, waterbodies_src, surveyed_src,
                 output_path, river_buffer_m, road_buffer_m, wb_buffer_m,
                 min_overlap_pct, sliver_threshold_m2):
        super().__init__()
        self.parcels_src         = parcels_src
        self.rivers_src          = rivers_src
        self.roads_src           = roads_src
        self.waterbodies_src     = waterbodies_src
        self.surveyed_src        = surveyed_src
        self.output_path         = output_path
        self.river_buffer_m      = river_buffer_m
        self.road_buffer_m       = road_buffer_m
        self.wb_buffer_m         = wb_buffer_m
        self.min_overlap_pct     = min_overlap_pct
        self.sliver_threshold_m2 = sliver_threshold_m2

    def run(self):  # noqa: C901
        try:
            from shapely.ops import unary_union
            from shapely.geometry import MultiPolygon, Polygon, GeometryCollection
            from osgeo import ogr

            # ── Load parcels ──────────────────────────────────────────────
            self.progress.emit("Loading layers…")
            parcel_geoms, parcel_attrs, target_srs, parcel_fields, parcel_field_types = \
                _read_source(self.parcels_src)

            if target_srs is None:
                self.error.emit("Parcels layer has no CRS defined.")
                return

            laf_col_p = _find_laf_col(parcel_fields)
            laf_col_s = None

            # ── Reference layers ──────────────────────────────────────────
            buf_river_geoms = None
            rivers_sindex   = None
            if self.rivers_src:
                self.progress.emit(f"Buffering rivers by {self.river_buffer_m} m…")
                rv_geoms, _, rv_srs, _, _ = _read_source(self.rivers_src)
                rv_geoms        = _reproject_geoms(rv_geoms, rv_srs, target_srs)
                buf_river_geoms = [g.buffer(self.river_buffer_m) if (g and not g.is_empty) else None
                                   for g in rv_geoms]
                rivers_sindex   = _STRIndex(buf_river_geoms)

            buf_road_geoms = None
            roads_sindex   = None
            if self.roads_src:
                self.progress.emit(f"Buffering roads by {self.road_buffer_m} m…")
                rd_geoms, _, rd_srs, _, _ = _read_source(self.roads_src)
                rd_geoms       = _reproject_geoms(rd_geoms, rd_srs, target_srs)
                buf_road_geoms = [g.buffer(self.road_buffer_m) if (g and not g.is_empty) else None
                                  for g in rd_geoms]
                roads_sindex   = _STRIndex(buf_road_geoms)

            buf_wb_geoms = None
            wb_sindex    = None
            if self.waterbodies_src:
                self.progress.emit(f"Buffering waterbodies by {self.wb_buffer_m} m…")
                wb_geoms, _, wb_srs, _, _ = _read_source(self.waterbodies_src)
                wb_geoms     = _reproject_geoms(wb_geoms, wb_srs, target_srs)
                buf_wb_geoms = [g.buffer(self.wb_buffer_m) if (g and not g.is_empty) else None
                                for g in wb_geoms]
                wb_sindex    = _STRIndex(buf_wb_geoms)

            surveyed_repaired  = []
            surveyed_sindex    = None
            surv_laf_arr       = None
            surveyed_raw_geoms = []
            if self.surveyed_src:
                self.progress.emit("Pre-repairing surveyed geometries…")
                sv_geoms, sv_attrs, sv_srs, sv_fields, _ = _read_source(self.surveyed_src)
                sv_geoms           = _reproject_geoms(sv_geoms, sv_srs, target_srs)
                surveyed_raw_geoms = sv_geoms
                laf_col_s          = _find_laf_col(sv_fields)
                surveyed_repaired  = [g.buffer(0) if (g and not g.is_empty) else None
                                      for g in sv_geoms]
                surveyed_sindex    = _STRIndex(surveyed_repaired)
                surv_laf_arr       = (
                    [str(a.get(laf_col_s, '') or '').strip() for a in sv_attrs]
                    if laf_col_s else None
                )

            if laf_col_p and laf_col_s:
                self.progress.emit(f"LAF columns: '{laf_col_p}' | '{laf_col_s}'")
            else:
                self.progress.emit("No LAF column found — LAF matching skipped.")

            parcel_laf_arr = (
                [str(a.get(laf_col_p, '') or '').strip() for a in parcel_attrs]
                if laf_col_p else None
            )

            n           = len(parcel_geoms)
            report_step = max(1, n // 20)

            # ── Statistics counters ───────────────────────────────────────
            n_null          = 0
            n_clean         = 0
            n_conflict      = 0
            n_geom_changed  = 0
            n_geom_fallback = 0
            n_split         = 0
            area_before_m2  = 0.0
            count_river     = 0
            count_road      = 0
            count_water     = 0
            count_surveyed  = 0
            combo_counts    = {}

            out_orig_idx            = []
            out_geoms               = []
            out_comments            = []
            out_adjusted            = []
            out_orig_geoms          = []
            out_is_new              = []
            out_is_fully_overlapped = []
            out_is_much_overlap     = []

            def _flatten_geom(g):
                if g is None or g.is_empty:
                    return []
                if isinstance(g, Polygon):
                    return [g]
                if isinstance(g, MultiPolygon):
                    return list(g.geoms)
                if isinstance(g, GeometryCollection):
                    parts = []
                    for sub in g.geoms:
                        parts.extend(_flatten_geom(sub))
                    return parts
                return [g]

            def _apply_cut(pieces, cutter):
                new_pieces = []
                changed    = False
                for p in pieces:
                    diff  = p.difference(cutter)
                    parts = _flatten_geom(diff)
                    if parts:
                        if len(parts) > 1 or not parts[0].equals(p):
                            changed = True
                        new_pieces.extend(parts)
                    else:
                        changed = True
                return (new_pieces if new_pieces else list(pieces)), changed

            # ── Main loop ─────────────────────────────────────────────────
            self.progress.emit(f"Processing {n} parcels…")
            for i, geom in enumerate(parcel_geoms):
                if i % report_step == 0:
                    self.progress.emit(f"Processing {i}/{n}  ({100 * i // n}%)…")

                if geom is None or geom.is_empty:
                    n_null += 1
                    out_orig_idx.append(i)
                    out_geoms.append(geom)
                    out_comments.append(_make_overlap_comment(set()))
                    out_adjusted.append(False)
                    out_orig_geoms.append(None)
                    out_is_new.append(False)
                    out_is_fully_overlapped.append(False)
                    out_is_much_overlap.append(False)
                    continue

                orig_wkt       = geom.wkt
                area_before_m2 += geom.area

                cand_surv = []
                if surveyed_sindex is not None:
                    cand_surv = surveyed_sindex.intersection(geom.bounds)
                    if cand_surv:
                        cand_surv = [ci for ci in cand_surv
                                     if surveyed_repaired[ci] is not None
                                     and surveyed_repaired[ci].intersects(geom)]

                conflict_geoms = []
                for ci in cand_surv:
                    surv_geom  = surveyed_repaired[ci]
                    is_matched = False
                    if parcel_laf_arr is not None and surv_laf_arr is not None:
                        p_laf = parcel_laf_arr[i]
                        s_laf = surv_laf_arr[ci]
                        if p_laf and s_laf and p_laf != 'nan' and p_laf == s_laf:
                            is_matched = True
                    if not is_matched and _coords_mostly_match(geom, surv_geom):
                        is_matched = True
                    if not is_matched:
                        conflict_geoms.append(surv_geom)

                hits_river   = False
                local_rivers = None
                if rivers_sindex is not None:
                    cand_river = rivers_sindex.intersection(geom.bounds)
                    if cand_river:
                        cand_river = [ri for ri in cand_river
                                      if buf_river_geoms[ri] is not None
                                      and buf_river_geoms[ri].intersects(geom)]
                    hits_river = len(cand_river) > 0
                    if hits_river:
                        local_rivers = (
                            buf_river_geoms[cand_river[0]]
                            if len(cand_river) == 1
                            else unary_union([buf_river_geoms[ri] for ri in cand_river])
                        )

                hits_road   = False
                local_roads = None
                if roads_sindex is not None:
                    cand_road = roads_sindex.intersection(geom.bounds)
                    if cand_road:
                        cand_road = [ri for ri in cand_road
                                     if buf_road_geoms[ri] is not None
                                     and buf_road_geoms[ri].intersects(geom)]
                    hits_road = len(cand_road) > 0
                    if hits_road:
                        local_roads = (
                            buf_road_geoms[cand_road[0]]
                            if len(cand_road) == 1
                            else unary_union([buf_road_geoms[ri] for ri in cand_road])
                        )

                hits_water   = False
                local_waters = None
                if wb_sindex is not None:
                    cand_wb = wb_sindex.intersection(geom.bounds)
                    if cand_wb:
                        cand_wb = [wi for wi in cand_wb
                                   if buf_wb_geoms[wi] is not None
                                   and buf_wb_geoms[wi].intersects(geom)]
                    hits_water = len(cand_wb) > 0
                    if hits_water:
                        local_waters = (
                            buf_wb_geoms[cand_wb[0]]
                            if len(cand_wb) == 1
                            else unary_union([buf_wb_geoms[wi] for wi in cand_wb])
                        )

                hits_neighbour = len(conflict_geoms) > 0

                if hits_river:     count_river    += 1
                if hits_road:      count_road     += 1
                if hits_water:     count_water    += 1
                if hits_neighbour: count_surveyed += 1

                overlap_set = set()
                if hits_river:     overlap_set.add('river')
                if hits_road:      overlap_set.add('road')
                if hits_water:     overlap_set.add('waterbody')
                if hits_neighbour: overlap_set.add('Surveyed land')

                comment = _make_overlap_comment(overlap_set)

                if not overlap_set:
                    n_clean += 1
                    out_orig_idx.append(i)
                    out_geoms.append(geom)
                    out_comments.append(comment)
                    out_adjusted.append(False)
                    out_orig_geoms.append(orig_wkt)
                    out_is_new.append(False)
                    out_is_fully_overlapped.append(False)
                    out_is_much_overlap.append(False)
                    continue

                n_conflict += 1
                combo_key = tuple(lyr for lyr in ['river', 'road', 'waterbody', 'Surveyed land']
                                  if lyr in overlap_set)
                combo_counts[combo_key] = combo_counts.get(combo_key, 0) + 1

                pieces = [geom]

                if hits_river and local_rivers:
                    pieces, _ = _apply_cut(pieces, local_rivers)

                if hits_road and local_roads:
                    pieces, _ = _apply_cut(pieces, local_roads)

                if hits_water and local_waters:
                    pieces, _ = _apply_cut(pieces, local_waters)

                if hits_neighbour:
                    nbr_union = (conflict_geoms[0]
                                 if len(conflict_geoms) == 1
                                 else unary_union(conflict_geoms))
                    pieces, _ = _apply_cut(pieces, nbr_union)

                if len(pieces) > 1:
                    large = [p for p in pieces if p.area >= 200.0]
                    if large:
                        pieces = large

                if len(pieces) > 1:
                    n_split += 1
                    pieces.sort(key=lambda p: p.area, reverse=True)

                geom_changed = (len(pieces) > 1) or (not pieces[0].equals(geom))

                is_much_ovl = False
                if geom_changed and geom.area > 0:
                    remaining_area = sum(p.area for p in pieces)
                    removed_ratio  = 1.0 - (remaining_area / geom.area)
                    if removed_ratio >= self.min_overlap_pct / 100.0:
                        is_much_ovl  = True
                        pieces       = [geom]
                        geom_changed = False

                if geom_changed:
                    n_geom_changed += 1
                else:
                    n_geom_fallback += 1

                for piece_idx, piece in enumerate(pieces):
                    out_orig_idx.append(i)
                    out_geoms.append(piece)
                    out_comments.append(comment)
                    out_adjusted.append(geom_changed)
                    out_orig_geoms.append(orig_wkt)
                    out_is_new.append(piece_idx > 0)
                    out_is_fully_overlapped.append(not geom_changed and not is_much_ovl)
                    out_is_much_overlap.append(is_much_ovl)

            # ── Build output row dicts ────────────────────────────────────
            self.progress.emit(f"Processing {n}/{n} (100%)… building output…")

            out_rows = []
            for k, orig_i in enumerate(out_orig_idx):
                row = dict(parcel_attrs[orig_i])
                row['comment']             = out_comments[k]
                row['geometry_adjusted']   = out_adjusted[k]
                row['original_geometry']   = out_orig_geoms[k]
                row['is_new']              = out_is_new[k]
                row['is_fully_overlapped'] = out_is_fully_overlapped[k]
                row['is_much_overlap']     = out_is_much_overlap[k]
                row['_orig_row_idx']       = orig_i
                row['overlap_pct']         = None
                row['overlap_area']        = None
                row['_geometry']           = out_geoms[k]
                out_rows.append(row)

            # ── Overlap area columns ──────────────────────────────────────
            self.progress.emit("Computing overlap_pct / overlap_area…")
            try:
                all_ref = []
                for src in (buf_river_geoms, buf_road_geoms, buf_wb_geoms):
                    if src:
                        all_ref.extend(g for g in src if g is not None and not g.is_empty)
                if surveyed_repaired:
                    all_ref.extend(g for g in surveyed_repaired if g is not None and not g.is_empty)

                if all_ref:
                    self.progress.emit("Building reference union…")
                    ref_union = unary_union(all_ref)
                    seen_orig = {}   # orig_idx → (overlap_pct, overlap_area) — compute once per original parcel
                    for row in out_rows:
                        orig_idx = int(row['_orig_row_idx'])
                        if orig_idx not in seen_orig:
                            orig_geom = parcel_geoms[orig_idx]
                            if orig_geom is None or orig_geom.is_empty or orig_geom.area <= 0:
                                seen_orig[orig_idx] = (0.0, 0.0)
                            else:
                                try:
                                    inter    = orig_geom.buffer(0).intersection(ref_union)
                                    ovl_area = inter.area if not inter.is_empty else 0.0
                                except Exception:
                                    ovl_area = 0.0
                                seen_orig[orig_idx] = (
                                    round(min(100.0, 100.0 * ovl_area / orig_geom.area), 4),
                                    round(ovl_area, 4),
                                )
                        row['overlap_pct'], row['overlap_area'] = seen_orig[orig_idx]
                else:
                    self.progress.emit("No reference layers — overlap columns skipped.")
            except Exception as _oe:
                self.progress.emit(f"Overlap computation warning: {_oe}")

            area_after_m2 = sum(
                r['_geometry'].area for r in out_rows
                if r['_geometry'] is not None and not r['_geometry'].is_empty
            )

            # ── Save output GPKG ──────────────────────────────────────────
            self.progress.emit("Saving output…")
            computed_field_specs = [
                ('comment',             ogr.OFTString,  None),
                ('geometry_adjusted',   ogr.OFTInteger, ogr.OFSTBoolean),
                ('original_geometry',   ogr.OFTString,  None),
                ('is_new',              ogr.OFTInteger, ogr.OFSTBoolean),
                ('is_fully_overlapped', ogr.OFTInteger, ogr.OFSTBoolean),
                ('is_much_overlap',     ogr.OFTInteger, ogr.OFSTBoolean),
                ('_orig_row_idx',       ogr.OFTInteger, None),
                ('overlap_pct',         ogr.OFTReal,    None),
                ('overlap_area',        ogr.OFTReal,    None),
            ]
            comp_names      = {n for n, _, _ in computed_field_specs}
            safe_orig_types = [(n, t, st) for n, t, st in parcel_field_types if n not in comp_names]
            all_field_specs = safe_orig_types + computed_field_specs

            _write_gpkg(
                self.output_path,
                [r['_geometry'] for r in out_rows],
                out_rows,
                all_field_specs,
                target_srs,
            )

            # ── ref_clean_layers ──────────────────────────────────────────
            self.progress.emit("Building ref_clean_layers…")
            try:
                ref_geoms, ref_rows = [], []
                if buf_river_geoms:
                    for g in buf_river_geoms:
                        if g is not None and not g.is_empty:
                            ref_geoms.append(g)
                            ref_rows.append({'origin': 'river'})
                if buf_road_geoms:
                    for g in buf_road_geoms:
                        if g is not None and not g.is_empty:
                            ref_geoms.append(g)
                            ref_rows.append({'origin': 'road'})
                if buf_wb_geoms:
                    for g in buf_wb_geoms:
                        if g is not None and not g.is_empty:
                            ref_geoms.append(g)
                            ref_rows.append({'origin': 'waterbody'})
                if self.surveyed_src and surveyed_raw_geoms:
                    for g in surveyed_raw_geoms:
                        if g is not None and not g.is_empty:
                            ref_geoms.append(g)
                            ref_rows.append({'origin': 'surveyed_land'})

                if ref_rows:
                    c_path = os.path.join(
                        os.path.dirname(self.output_path), 'ref_clean_layers.gpkg'
                    )
                    _write_gpkg(
                        c_path, ref_geoms, ref_rows,
                        [('origin', ogr.OFTString, None)],
                        target_srs,
                        layer_name='ref_clean_layers',
                    )
                    self.constraints.emit(c_path)
                else:
                    self.progress.emit("No reference layers — skipping ref_clean_layers.")
            except Exception as e:
                self.progress.emit(f"ref_clean_layers warning: {e}")

            summary = (
                f"Done.  {n} input parcels → {len(out_rows)} output features  "
                f"({n_geom_changed} adjusted, {n_split} split)\n"
                f"  Saved → {self.output_path}"
            )
            self.finished.emit({
                'summary':        summary,
                'output_path':    self.output_path,
                'result_rows':    out_rows,
                'parcel_fields':  parcel_fields,
                'n':              n,
                'n_geom_changed': n_geom_changed,
                'n_split':        n_split,
            })

        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── Dock widget ───────────────────────────────────────────────────────────────

class SolveTopologyDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Solve Topology Issues')
        self._thread      = None
        self._worker      = None
        self._result      = None
        self._out_layer   = None
        self._review_rows = []

        root = QWidget()
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(6, 6, 6, 6)
        vbox.setSpacing(4)

        self._tabs = QTabWidget()
        vbox.addWidget(self._tabs)

        self._tabs.addTab(self._build_process_tab(), "Process")
        self._review_tab = self._build_review_tab()
        self._tabs.addTab(self._review_tab, "Review")
        self._tabs.setTabEnabled(1, False)

        self.setWidget(root)
        self._auto_select_layers()
        QgsProject.instance().layersAdded.connect(self._on_layers_added)

    # ── Process tab ───────────────────────────────────────────────────────────

    def _build_process_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(5)
        layout.setContentsMargins(6, 6, 6, 6)

        H = 26

        def layer_row(label_text):
            row   = QHBoxLayout()
            lbl   = QLabel(label_text)
            lbl.setFixedWidth(110)
            combo = QgsMapLayerComboBox()
            combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
            combo.setFixedHeight(H)
            row.addWidget(lbl)
            row.addWidget(combo)
            layout.addLayout(row)
            return combo

        def ref_layer_row(label_text, default_buf=0.0, line=False):
            row   = QHBoxLayout()
            lbl   = QLabel(label_text)
            lbl.setFixedWidth(110)
            combo = QgsMapLayerComboBox()
            combo.setFilters(
                QgsMapLayerProxyModel.Filter.LineLayer if line
                else QgsMapLayerProxyModel.Filter.PolygonLayer
            )
            combo.setFixedHeight(H)
            combo.setAllowEmptyLayer(True)
            combo.setCurrentIndex(0)
            row.addWidget(lbl)
            row.addWidget(combo)
            buf_lbl = QLabel("buf:")
            buf_lbl.setFixedWidth(26)
            spn = QDoubleSpinBox()
            spn.setRange(0, 50000)
            spn.setValue(default_buf)
            spn.setDecimals(1)
            spn.setSuffix(" m")
            spn.setFixedWidth(76)
            spn.setFixedHeight(H)
            row.addWidget(buf_lbl)
            row.addWidget(spn)
            layout.addLayout(row)
            return combo, spn

        def plain_ref_row(label_text):
            row   = QHBoxLayout()
            lbl   = QLabel(label_text)
            lbl.setFixedWidth(110)
            combo = QgsMapLayerComboBox()
            combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
            combo.setFixedHeight(H)
            combo.setAllowEmptyLayer(True)
            combo.setCurrentIndex(0)
            row.addWidget(lbl)
            row.addWidget(combo)
            layout.addLayout(row)
            return combo

        def spin_row(label_text, lo, hi, default, decimals=1, suffix=''):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(170)
            spn = QDoubleSpinBox()
            spn.setRange(lo, hi)
            spn.setValue(default)
            spn.setDecimals(decimals)
            spn.setFixedHeight(H)
            if suffix:
                spn.setSuffix(suffix)
            row.addWidget(lbl)
            row.addWidget(spn)
            row.addStretch()
            layout.addLayout(row)
            return spn

        self.cmb_parcels                         = layer_row("Parcels:")
        self.cmb_rivers,    self.spn_river_buf   = ref_layer_row("Rivers:",       default_buf=30.0, line=True)
        self.cmb_roads,     self.spn_road_buf    = ref_layer_row("Roads:",        default_buf=5.0,  line=True)
        self.cmb_waterbody, self.spn_wb_buf      = ref_layer_row("Waterbodies:",  default_buf=30.0)
        self.cmb_surveyed                        = plain_ref_row("Surveyed land:")

        self.spn_overlap = spin_row('Min overlap % to adjust:', 0, 100, 90, 1, ' %')
        self.spn_sliver  = spin_row('Sliver threshold (m²):',   0, 1000, 1,  2)

        out_row = QHBoxLayout()
        lbl_out = QLabel('Output path:')
        lbl_out.setFixedWidth(110)
        self.wgt_output = QgsFileWidget()
        self.wgt_output.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.wgt_output.setFilter("GeoPackage (*.gpkg)")
        out_row.addWidget(lbl_out)
        out_row.addWidget(self.wgt_output)
        layout.addLayout(out_row)

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

        return w

    # ── Review tab ────────────────────────────────────────────────────────────

    def _build_review_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 6, 6, 6)

        filter_group = QGroupBox("Filter")
        fg_layout    = QHBoxLayout(filter_group)
        fg_layout.setSpacing(6)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search comment…")
        self._search_box.textChanged.connect(self._apply_filter)
        fg_layout.addWidget(QLabel("Search:"))
        fg_layout.addWidget(self._search_box)

        self._comment_filter = QComboBox()
        self._comment_filter.addItem("All comments")
        self._comment_filter.currentIndexChanged.connect(self._apply_filter)
        fg_layout.addWidget(QLabel("Comment:"))
        fg_layout.addWidget(self._comment_filter)

        self._adjusted_only = QCheckBox("Adjusted only")
        self._adjusted_only.stateChanged.connect(self._apply_filter)
        fg_layout.addWidget(self._adjusted_only)

        layout.addWidget(filter_group)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Parcel", "Comment"])
        self._tree.setColumnWidth(0, 100)
        self._tree.itemClicked.connect(self._on_item_clicked)
        splitter.addWidget(self._tree)

        detail_w = QWidget()
        detail_l = QVBoxLayout(detail_w)
        detail_l.setContentsMargins(4, 4, 4, 4)
        self._detail = QTextBrowser()
        self._detail.setOpenExternalLinks(True)
        detail_l.addWidget(self._detail)

        rev_btn_row = QHBoxLayout()
        self._zoom_btn = QPushButton("Zoom to in map")
        self._zoom_btn.setEnabled(False)
        self._zoom_btn.clicked.connect(self._zoom_to_selected)
        rev_btn_row.addWidget(self._zoom_btn)
        rev_btn_row.addStretch()
        detail_l.addLayout(rev_btn_row)
        splitter.addWidget(detail_w)

        splitter.setSizes([220, 280])
        layout.addWidget(splitter, 1)

        return w

    # ── Signals ───────────────────────────────────────────────────────────────

    def _on_layers_added(self, _):
        if not sip.isdeleted(self):
            self._auto_select_layers()

    def closeEvent(self, event):
        try:
            QgsProject.instance().layersAdded.disconnect(self._on_layers_added)
        except (RuntimeError, TypeError):  # nosec B110
            pass
        super().closeEvent(event)

    # ── Status ────────────────────────────────────────────────────────────────

    def _set_status(self, message, color="green"):
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(message)

    # ── Auto-select layers ────────────────────────────────────────────────────

    def _auto_select_layers(self):  # noqa: C901
        try:
            from qgis.core import QgsVectorLayer
            layers = [
                l for l in QgsProject.instance().mapLayers().values()
                if isinstance(l, QgsVectorLayer)
            ]

            def find_layer(keywords):
                for layer in layers:
                    if any(kw in layer.name().lower() for kw in keywords):
                        return layer
                return None

            river_layer    = find_layer(['river'])
            road_layer     = find_layer(['road'])
            water_layer    = find_layer(['lake', 'waterbody', 'waterbodies', 'water'])
            surveyed_layer = find_layer(['surveyed', 'mzo'])

            if river_layer:
                self.cmb_rivers.setLayer(river_layer)
            if road_layer:
                self.cmb_roads.setLayer(road_layer)
            if water_layer:
                self.cmb_waterbody.setLayer(water_layer)
            if surveyed_layer:
                self.cmb_surveyed.setLayer(surveyed_layer)
        except RuntimeError:
            try:
                QgsProject.instance().layersAdded.disconnect(self._on_layers_added)
            except (RuntimeError, TypeError):  # nosec B110
                pass

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        parcels_layer   = self.cmb_parcels.currentLayer()
        rivers_layer    = self.cmb_rivers.currentLayer()
        roads_layer     = self.cmb_roads.currentLayer()
        waterbody_layer = self.cmb_waterbody.currentLayer()
        surveyed_layer  = self.cmb_surveyed.currentLayer()
        output_path     = self.wgt_output.filePath()

        if not parcels_layer:
            self._set_status("Parcels layer is required!", "red"); return
        if not output_path:
            self._set_status("Output path is required!", "red"); return

        self._run_btn.setEnabled(False)
        self._set_status("Running…", "orange")
        self._output_path = output_path

        self._thread = QThread()
        self._worker = _Worker(
            parcels_src         = parcels_layer.source(),
            rivers_src          = rivers_layer.source()    if rivers_layer    else None,
            roads_src           = roads_layer.source()     if roads_layer     else None,
            waterbodies_src     = waterbody_layer.source() if waterbody_layer else None,
            surveyed_src        = surveyed_layer.source()  if surveyed_layer  else None,
            output_path         = output_path,
            river_buffer_m      = self.spn_river_buf.value(),
            road_buffer_m       = self.spn_road_buf.value(),
            wb_buffer_m         = self.spn_wb_buf.value(),
            min_overlap_pct     = self.spn_overlap.value(),
            sliver_threshold_m2 = self.spn_sliver.value(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda msg: self._set_status(msg, "orange"))
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.constraints.connect(self._on_constraints)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(lambda: self._run_btn.setEnabled(True))
        self._thread.start()

    # ── Finished handler ──────────────────────────────────────────────────────

    def _on_finished(self, result: dict):
        self._result = result
        self._set_status(result['summary'], "green")
        self._load_output()
        self._load_constraints()
        self._populate_review(result)

    def _on_error(self, msg):
        self._set_status(f"Error: {msg}", "red")

    # ── Load output into QGIS ─────────────────────────────────────────────────

    def _load_output(self):
        path = getattr(self, '_output_path', None)
        if not path or not os.path.exists(path):
            return
        from qgis.core import QgsVectorLayer
        layer_name = os.path.splitext(os.path.basename(path))[0]
        uri   = f"{path}|layername={layer_name}"
        layer = QgsVectorLayer(uri, layer_name, "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            self._out_layer = layer

    def _on_constraints(self, path):
        self._constraints_path = path

    def _load_constraints(self):
        from qgis.core import (
            QgsVectorLayer,
            QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsFillSymbol,
        )
        path = getattr(self, '_constraints_path', None)
        if not path or not os.path.exists(path):
            return
        uri = f"{path}|layername=ref_clean_layers"
        layer = QgsVectorLayer(uri, 'ref_clean_layers', 'ogr')
        if not layer.isValid():
            return

        _STYLES = [
            ('river',         '26,110,191,120',  '#1A6EBF'),
            ('road',          '192,57,43,120',   '#C0392B'),
            ('waterbody',     '22,160,133,120',  '#16A085'),
            ('surveyed_land', '125,60,152,120',  '#7D3C98'),
        ]
        categories = []
        for origin, fill_rgba, border_color in _STYLES:
            sym = QgsFillSymbol.createSimple({
                'color':         fill_rgba,
                'outline_color': border_color,
                'outline_width': '0.4',
            })
            label = origin.replace('_', ' ').title()
            categories.append(QgsRendererCategory(origin, sym, label))

        layer.setRenderer(QgsCategorizedSymbolRenderer('origin', categories))
        QgsProject.instance().addMapLayer(layer)

    # ── Populate review tab ───────────────────────────────────────────────────

    def _populate_review(self, result: dict):
        rows_data = result['result_rows']

        _skip = {'comment', 'geometry_adjusted', 'original_geometry', 'is_new',
                 'is_fully_overlapped', '_orig_row_idx', '_geometry', 'is_much_overlap'}

        self._comment_filter.blockSignals(True)
        self._comment_filter.clear()
        self._comment_filter.addItem("All comments")
        for cmt in sorted(set(r.get('comment', '') or '' for r in rows_data)):
            self._comment_filter.addItem(cmt, userData=cmt)
        self._comment_filter.blockSignals(False)

        self._review_rows = []
        for i, row in enumerate(rows_data):
            geom     = row.get('_geometry')
            orig_idx = int(row.get('_orig_row_idx', i))
            self._review_rows.append({
                'row_idx':     i,
                'orig_idx':    orig_idx,
                'comment':     str(row.get('comment', '')),
                'adjusted':    bool(row.get('geometry_adjusted', False)),
                'is_new':      bool(row.get('is_new', False)),
                'fully_ovl':   bool(row.get('is_fully_overlapped', False)),
                'bounds':      geom.bounds if (geom is not None and not geom.is_empty) else None,
                'extra_attrs': {
                    col: str(row[col])
                    for col in row
                    if col not in _skip
                },
            })

        self._tabs.setTabEnabled(1, True)
        self._apply_filter()
        self._tabs.setCurrentIndex(1)

    def _apply_filter(self):
        self._tree.clear()
        self._zoom_btn.setEnabled(False)
        self._detail.clear()

        search_text    = self._search_box.text().lower()
        adjusted_only  = self._adjusted_only.isChecked()
        filter_comment = self._comment_filter.currentData()

        groups = {}
        for row in self._review_rows:
            if adjusted_only and not row['adjusted']:
                continue
            if search_text and search_text not in row['comment'].lower():
                continue
            if filter_comment is not None and row['comment'] != filter_comment:
                continue
            groups.setdefault(row['orig_idx'], []).append(row)

        for orig_idx, rows in sorted(groups.items()):
            parent_item = QTreeWidgetItem(self._tree, [f"Parcel {orig_idx}", ""])
            parent_item.setExpanded(len(rows) > 1)
            parent_item.setData(0, Qt.ItemDataRole.UserRole, None)

            if len(rows) == 1 and not rows[0]['is_new']:
                r = rows[0]
                parent_item.setText(1, r['comment'])
                parent_item.setData(0, Qt.ItemDataRole.UserRole, r)
            else:
                for idx, r in enumerate(rows):
                    label = f"Fragment {idx + 1}" if r['is_new'] else f"Parcel {orig_idx}"
                    child = QTreeWidgetItem(parent_item, [label, r['comment']])
                    child.setData(0, Qt.ItemDataRole.UserRole, r)

        self._tree.resizeColumnToContents(0)

    def _on_item_clicked(self, item, _col):
        row = item.data(0, Qt.ItemDataRole.UserRole)
        if row is None:
            self._zoom_btn.setEnabled(False)
            self._detail.clear()
            return

        self._zoom_btn.setEnabled(row['bounds'] is not None)

        adj_tag = "&#x2714; adjusted" if row['adjusted'] else "&#x2014; not adjusted"
        new_tag = " (new split piece)" if row['is_new'] else ""
        ovl_tag = " (fully overlapped)" if row['fully_ovl'] else ""
        extra_rows = "".join(
            f"<tr><td><b>{k}</b></td><td>{v}</td></tr>"
            for k, v in row['extra_attrs'].items()
        )
        html = f"""<style>
          table {{ border-collapse: collapse; width: 100%; }}
          td {{ border: 1px solid #ccc; padding: 4px 8px; }}
          td:first-child {{ font-weight: bold; color: #555; width: 120px; }}
          h3 {{ margin: 4px 0; color: #2c5282; }}
        </style>
        <h3>Parcel detail</h3>
        <table>
          <tr><td>Parcel #</td><td>{row['orig_idx']}{new_tag}</td></tr>
          <tr><td>Geometry</td><td>{adj_tag}{ovl_tag}</td></tr>
          <tr><td>Comment</td><td><b>{row['comment']}</b></td></tr>
          {extra_rows}
        </table>"""
        self._detail.setHtml(html)

    def _zoom_to_selected(self):
        item = self._tree.currentItem()
        if item is None:
            return
        row = item.data(0, Qt.ItemDataRole.UserRole)
        if row is None or row['bounds'] is None:
            return
        try:
            from qgis.utils import iface
        except Exception:
            return
        if iface is None:
            return
        xmin, ymin, xmax, ymax = row['bounds']
        canvas = iface.mapCanvas()
        rect   = QgsRectangle(xmin, ymin, xmax, ymax)
        pad_x  = max(rect.width()  * 0.15, 1.0)
        pad_y  = max(rect.height() * 0.15, 1.0)
        rect.grow(max(pad_x, pad_y))
        canvas.setExtent(rect)
        canvas.refresh()
