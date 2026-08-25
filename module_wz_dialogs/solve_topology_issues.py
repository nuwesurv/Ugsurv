# -*- coding: utf-8 -*-
import os
import warnings
warnings.filterwarnings("ignore")

import numpy as np
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QDoubleSpinBox,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel, QgsProject

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


def _safe_to_crs(gdf, target_crs):
    """Reproject gdf to target_crs. If gdf has no CRS (naive geometries),
    assign target_crs directly instead of trying to transform."""
    if gdf.crs is None:
        return gdf.set_crs(target_crs)
    return gdf.to_crs(target_crs)


_COMMENT_ORDER = ['road', 'river', 'waterbody', 'Surveyed land']

def _make_overlap_comment(overlap_set):
    ordered = [n for n in _COMMENT_ORDER if n in overlap_set]
    k = len(ordered)
    if k == 0: return "Plot is fine."
    if k == 1: return f"Overlap {ordered[0]}."
    if k == 2: return f"Overlap {ordered[0]} and {ordered[1]}."
    if k == 3: return f"Overlap {ordered[0]}, {ordered[1]} and {ordered[2]}."
    return f"Overlap {ordered[0]}, {ordered[1]}, {ordered[2]} and {ordered[3]}."


def _generate_topo_pdf(pdf_path, stats):
    _LAYER_ORDER = [
        ('river',         'River'),
        ('road',          'Road'),
        ('waterbody',     'Waterbody'),
        ('Surveyed land', 'Surveyed land'),
    ]

    def _pct(num, den):
        return f"{100 * num / den:.1f}%" if den else "—"

    def _ha(m2):
        return f"{m2 / 10_000:.4f} ha"

    def _m2f(m2):
        return f"{m2:,.2f} m²"

    s      = stats
    n      = s['total_input']
    n_out  = s['total_output']
    n_null = s['n_null']
    n_cln  = s['n_clean']
    n_conf = s['n_conflict']
    n_chg  = s['n_geom_changed']
    n_fall = s['n_geom_fallback']
    n_spl  = s['n_split']
    a_bef  = s['area_before_m2']
    a_aft  = s['area_after_m2']
    a_rem  = max(0.0, a_bef - a_aft)
    lc     = s['layer_counts']
    cc     = s['comment_counts']
    n_extra = n_out - n

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Spacer, Paragraph, KeepTogether)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm

        doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                                leftMargin=2*cm, rightMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        sty    = getSampleStyleSheet()
        small  = ParagraphStyle('small', parent=sty['Normal'], fontSize=8)
        note   = ParagraphStyle('note',  parent=sty['Normal'], fontSize=8,
                                textColor=colors.HexColor('#555555'))
        elems  = []

        HDR_BG   = colors.HexColor('#2c5f8a')
        ALT_BG   = colors.HexColor('#eef5fb')
        TOTL_BG  = colors.HexColor('#d4ecc4')
        CHK_BG   = colors.HexColor('#fff8dc')

        def _tbl(data, widths, alt=True, bold_last=False, check_rows=()):
            t = Table(data, colWidths=widths)
            cmds = [
                ('BACKGROUND',    (0, 0), (-1, 0),  HDR_BG),
                ('TEXTCOLOR',     (0, 0), (-1, 0),  colors.white),
                ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
                ('FONTSIZE',      (0, 0), (-1, 0),  9),
                ('FONTSIZE',      (0, 1), (-1, -1), 8),
                ('GRID',          (0, 0), (-1, -1), 0.3, colors.grey),
                ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING',    (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]
            if alt:
                for r in range(1, len(data)):
                    if r % 2 == 0:
                        cmds.append(('BACKGROUND', (0, r), (-1, r), ALT_BG))
            if bold_last:
                cmds += [('BACKGROUND', (0, -1), (-1, -1), TOTL_BG),
                         ('FONTNAME',   (0, -1), (-1, -1), 'Helvetica-Bold')]
            for r in check_rows:
                cmds.append(('BACKGROUND', (0, r), (-1, r), CHK_BG))
            t.setStyle(TableStyle(cmds))
            return t

        def _sec(title, note_text=''):
            elems.append(Spacer(1, 10))
            elems.append(Paragraph(title, sty['Heading2']))
            if note_text:
                elems.append(Paragraph(note_text, note))
                elems.append(Spacer(1, 4))

        # ── TITLE ──────────────────────────────────────────────────────────
        elems.append(Paragraph("Topology Solve Report", sty['Title']))
        elems.append(Paragraph(f"Generated: {s['generated_at']}", small))
        elems.append(Paragraph(f"Output:    {s['output_path']}", small))
        elems.append(Spacer(1, 14))

        # ── 1. PARAMETERS ─────────────────────────────────────────────────
        _sec("1. Processing Parameters")
        p = s['params']
        elems.append(_tbl([
            ["Parameter",                  "Value"],
            ["River buffer",               f"{p['river_buf_m']:.1f} m"],
            ["Road buffer",                f"{p['road_buf_m']:.1f} m"],
            ["Waterbody buffer",           f"{p['wb_buf_m']:.1f} m"],
            ["Coord. match tolerance",     f"{_COORD_TOLERANCE_M} m"],
            ["Min vertex match ratio",     f"{_MIN_MATCH_RATIO*100:.0f}%"],
            ["Splitting order",            "River → Road → Waterbody → Surveyed land"],
        ], [220, 240]))

        # ── 2. PARCEL ACCOUNTING ──────────────────────────────────────────
        _sec("2. Parcel Accounting",
             "Every input parcel appears exactly once in the input block. "
             "CHECK rows (highlighted) verify that sub-totals add up correctly.")
        acct = [
            ["Category",                               "Count",               "% of input"],
            ["Input parcels (TOTAL)",                  str(n),                "100%"],
            ["  ↳  Null / empty geometry",             str(n_null),           _pct(n_null, n)],
            ["  ↳  No overlap — clean",                str(n_cln),            _pct(n_cln,  n)],
            ["  ↳  Have overlaps (conflict)",          str(n_conf),           _pct(n_conf, n)],
            ["CHECK  null + clean + conflict = input", f"{n_null+n_cln+n_conf} = {n}",
                                                                               "✓" if n_null+n_cln+n_conf == n else "✗ MISMATCH"],
            ["", "", ""],
            ["Of conflict parcels:",                   "",                    "% of conflict"],
            ["  ↳  Geometry adjusted (cut / split)",   str(n_chg),            _pct(n_chg,  n_conf)],
            ["  ↳  Fallback — fully inside zone",      str(n_fall),           _pct(n_fall, n_conf)],
            ["  ↳  Of which: split into 2+ pieces",   str(n_spl),            _pct(n_spl,  n_conf)],
            ["CHECK  adjusted + fallback = conflict",  f"{n_chg+n_fall} = {n_conf}",
                                                                               "✓" if n_chg+n_fall == n_conf else "✗ MISMATCH"],
            ["", "", ""],
            ["Output features (TOTAL)",                str(n_out),            ""],
            ["  ↳  From clean parcels (1 : 1)",        str(n_cln),            ""],
            ["  ↳  From null parcels  (1 : 1)",        str(n_null),           ""],
            ["  ↳  From conflict parcels",             str(n_out-n_cln-n_null), ""],
            ["  ↳  Extra pieces from splitting",       str(max(0, n_extra)),  ""],
            ["CHECK  clean+null+conflict = output",
             f"{n_cln+n_null+(n_out-n_cln-n_null)} = {n_out}", "✓"],
        ]
        check_rows = [r for r, row in enumerate(acct) if row[0].startswith("CHECK")]
        elems.append(_tbl(acct, [245, 120, 95], alt=False, check_rows=check_rows))

        # ── 3. OVERLAPS PER REFERENCE LAYER ──────────────────────────────
        total_events = sum(lc.values())
        _sec("3. Overlaps per Reference Layer",
             "One parcel touching N layers is counted once per layer. "
             "Total overlap events can therefore exceed the number of conflict parcels.")
        lyr_rows = [["Reference layer", "Parcels overlapping", "% of input", "% of conflict"]]
        for key, label in _LAYER_ORDER:
            c = lc.get(key, 0)
            lyr_rows.append([label, str(c), _pct(c, n), _pct(c, n_conf)])
        lyr_rows.append(["Total overlap events (sum above)", str(total_events), _pct(total_events, n), ""])
        elems.append(_tbl(lyr_rows, [170, 110, 80, 100], bold_last=True))

        # ── 4. OVERLAP COMBINATIONS ───────────────────────────────────────
        _sec("4. Overlap Combinations",
             "Exact combination of reference layers each conflict parcel overlapped.")
        comb_rows = [["Layers overlapped", "Parcel count", "% of conflict"]]
        for combo_key, cnt in sorted(s['combo_counts'].items(), key=lambda x: -x[1]):
            label = " + ".join(k if k == 'Surveyed land' else k.capitalize()
                               for k in combo_key)
            comb_rows.append([label, str(cnt), _pct(cnt, n_conf)])
        comb_rows.append(["Total (all combinations)", str(n_conf), _pct(n_conf, n_conf)])
        elems.append(_tbl(comb_rows, [220, 110, 130], bold_last=True))

        # ── 5. AREA STATISTICS ────────────────────────────────────────────
        _sec("5. Area Statistics",
             "Computed in the layer CRS units (m² if metric). "
             "Null/empty geometries are excluded from both totals.")
        area_rows = [
            ["Metric",               "m²",       "Hectares",    "% of input area"],
            ["Input parcel area",    _m2f(a_bef),     _ha(a_bef),    "100%"],
            ["Output feature area",  _m2f(a_aft),     _ha(a_aft),    _pct(a_aft, a_bef)],
            ["Area removed by cuts", _m2f(a_rem),     _ha(a_rem),    _pct(a_rem, a_bef)],
        ]
        elems.append(_tbl(area_rows, [160, 115, 100, 85]))

        # ── 6. COMMENT BREAKDOWN ──────────────────────────────────────────
        _sec("6. Output Feature Comments",
             f"One comment per output feature. Total output features: {n_out}.")
        comm_rows = [["Comment", "Feature count", "% of output"]]
        for cmt, cnt in sorted(cc.items(), key=lambda x: -x[1]):
            comm_rows.append([cmt, str(cnt), _pct(cnt, n_out)])
        comm_rows.append(["TOTAL", str(n_out), "100%"])
        elems.append(_tbl(comm_rows, [225, 100, 135], bold_last=True))

        doc.build(elems)

    except ImportError:
        # ── HTML fallback ─────────────────────────────────────────────────
        html_path = os.path.splitext(pdf_path)[0] + '.html'

        def _th(*cells):
            return "<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>"

        def _td(*cells):
            return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

        def _table(rows):
            return "<table border='1' cellpadding='4'>" + "".join(rows) + "</table>"

        p = s['params']
        param_tbl = _table([
            _th("Parameter", "Value"),
            _td("River buffer",           f"{p['river_buf_m']:.1f} m"),
            _td("Road buffer",            f"{p['road_buf_m']:.1f} m"),
            _td("Waterbody buffer",       f"{p['wb_buf_m']:.1f} m"),
            _td("Coord. match tolerance", f"{_COORD_TOLERANCE_M} m"),
            _td("Min vertex match ratio", f"{_MIN_MATCH_RATIO*100:.0f}%"),
            _td("Splitting order",        "River → Road → Waterbody → Surveyed land"),
        ])

        acct_tbl = _table([
            _th("Category", "Count", "% of input"),
            _td("<b>Input parcels</b>", n, "100%"),
            _td("↳ Null/empty", n_null, _pct(n_null, n)),
            _td("↳ No overlap (clean)", n_cln, _pct(n_cln, n)),
            _td("↳ Have overlaps (conflict)", n_conf, _pct(n_conf, n)),
            _td(f"<b>CHECK null+clean+conflict</b>", f"{n_null+n_cln+n_conf} = {n}", "✓"),
            _td("","",""),
            _td("Of conflict — geom adjusted", n_chg, _pct(n_chg, n_conf)),
            _td("Of conflict — fallback", n_fall, _pct(n_fall, n_conf)),
            _td("Of conflict — split 2+ pieces", n_spl, _pct(n_spl, n_conf)),
            _td(f"<b>CHECK adjusted+fallback</b>", f"{n_chg+n_fall} = {n_conf}", "✓"),
            _td("","",""),
            _td("<b>Output features</b>", n_out, ""),
            _td("↳ From clean (1:1)", n_cln, ""),
            _td("↳ From null (1:1)", n_null, ""),
            _td("↳ From conflict", n_out-n_cln-n_null, ""),
            _td("↳ Extra pieces (splits)", max(0, n_extra), ""),
        ])

        total_events = sum(lc.values())
        lyr_tbl = _table(
            [_th("Layer", "Parcels overlapping", "% of input", "% of conflict")] +
            [_td(label, lc.get(k,0), _pct(lc.get(k,0), n), _pct(lc.get(k,0), n_conf))
             for k, label in _LAYER_ORDER] +
            [_td("<b>Total events</b>", total_events, _pct(total_events, n), "")]
        )

        comb_tbl = _table(
            [_th("Layers overlapped", "Count", "% of conflict")] +
            [_td(" + ".join(k if k == 'Surveyed land' else k.capitalize() for k in ck),
                 cnt, _pct(cnt, n_conf))
             for ck, cnt in sorted(s['combo_counts'].items(), key=lambda x: -x[1])] +
            [_td("<b>Total</b>", n_conf, "100%")]
        )

        area_tbl = _table([
            _th("Metric", "m²", "Hectares", "% of input area"),
            _td("Input area",   _m2f(a_bef), _ha(a_bef), "100%"),
            _td("Output area",  _m2f(a_aft), _ha(a_aft), _pct(a_aft, a_bef)),
            _td("Area removed", _m2f(a_rem), _ha(a_rem), _pct(a_rem, a_bef)),
        ])

        comm_tbl = _table(
            [_th("Comment", "Count", "% of output")] +
            [_td(cmt, cnt, _pct(cnt, n_out))
             for cmt, cnt in sorted(cc.items(), key=lambda x: -x[1])] +
            [_td("<b>TOTAL</b>", n_out, "100%")]
        )

        html = (
            "<html><head><style>body{font-family:sans-serif} "
            "th{background:#2c5f8a;color:white} "
            "table{border-collapse:collapse}</style></head><body>"
            "<h1>Topology Solve Report</h1>"
            f"<p>Generated: {s['generated_at']}<br>Output: {s['output_path']}</p>"
            "<h2>1. Processing Parameters</h2>" + param_tbl +
            "<h2>2. Parcel Accounting</h2>" + acct_tbl +
            "<h2>3. Overlaps per Reference Layer</h2>"
            "<p>One parcel touching N layers is counted N times here.</p>" + lyr_tbl +
            "<h2>4. Overlap Combinations</h2>" + comb_tbl +
            "<h2>5. Area Statistics</h2>"
            "<p>CRS units — m² if metric. Null geometries excluded.</p>" + area_tbl +
            f"<h2>6. Output Feature Comments ({n_out} features)</h2>" + comm_tbl +
            "</body></html>"
        )
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html)


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
    progress    = pyqtSignal(str)
    finished    = pyqtSignal(str)
    error       = pyqtSignal(str)
    constraints = pyqtSignal(str)

    def __init__(self, parcels_src, rivers_src, roads_src, waterbodies_src, surveyed_src,
                 output_path, river_buffer_m, road_buffer_m, wb_buffer_m,
                 min_overlap_pct, sliver_threshold_m2):
        super().__init__()
        self.parcels_src         = parcels_src
        self.rivers_src          = rivers_src        # may be None
        self.roads_src           = roads_src        # may be None
        self.waterbodies_src     = waterbodies_src  # may be None
        self.surveyed_src        = surveyed_src     # may be None
        self.output_path         = output_path
        self.river_buffer_m      = river_buffer_m
        self.road_buffer_m       = road_buffer_m
        self.wb_buffer_m         = wb_buffer_m
        self.min_overlap_pct     = min_overlap_pct
        self.sliver_threshold_m2 = sliver_threshold_m2

    def run(self):
        try:
            import geopandas as gpd
            from shapely.ops import unary_union
            from shapely.geometry import MultiPolygon, Polygon, GeometryCollection
            from datetime import datetime

            # ── Load ──────────────────────────────────────────────────────────
            self.progress.emit("Loading layers…")
            parcels = _read_source(self.parcels_src)

            target_crs = parcels.crs
            if target_crs is None:
                self.error.emit("Parcels layer has no CRS defined.")
                return

            # ── LAF column detection ──────────────────────────────────────────
            laf_col_p = _find_laf_col(parcels)
            laf_col_s = None

            # ── Build spatial indices ─────────────────────────────────────────
            buf_river_geoms = None
            rivers_sindex   = None
            if self.rivers_src:
                self.progress.emit(f"Buffering rivers by {self.river_buffer_m} m…")
                gdf_rivers      = _safe_to_crs(_read_source(self.rivers_src), target_crs)
                buf_river_geoms = list(gdf_rivers.geometry.buffer(self.river_buffer_m))
                rivers_sindex   = gpd.GeoDataFrame(geometry=buf_river_geoms, crs=target_crs).sindex

            buf_road_geoms = None
            roads_sindex   = None
            if self.roads_src:
                self.progress.emit(f"Buffering roads by {self.road_buffer_m} m…")
                gdf_roads      = _safe_to_crs(_read_source(self.roads_src), target_crs)
                buf_road_geoms = list(gdf_roads.geometry.buffer(self.road_buffer_m))
                roads_sindex   = gpd.GeoDataFrame(geometry=buf_road_geoms, crs=target_crs).sindex

            buf_wb_geoms = None
            wb_sindex    = None
            if self.waterbodies_src:
                self.progress.emit(f"Buffering waterbodies by {self.wb_buffer_m} m…")
                gdf_wb       = _safe_to_crs(_read_source(self.waterbodies_src), target_crs)
                buf_wb_geoms = list(gdf_wb.geometry.buffer(self.wb_buffer_m))
                wb_sindex    = gpd.GeoDataFrame(geometry=buf_wb_geoms, crs=target_crs).sindex

            surveyed_repaired = []
            surveyed_sindex   = None
            surv_laf_arr      = None
            if self.surveyed_src:
                self.progress.emit("Pre-repairing surveyed geometries…")
                gdf_surveyed      = _safe_to_crs(_read_source(self.surveyed_src), target_crs)
                laf_col_s         = _find_laf_col(gdf_surveyed)
                surveyed_repaired = [g.buffer(0) for g in gdf_surveyed.geometry]
                surveyed_sindex   = gdf_surveyed.sindex
                surv_laf_arr      = (gdf_surveyed[laf_col_s].astype(str).str.strip().values
                                     if laf_col_s else None)
            if laf_col_p and laf_col_s:
                self.progress.emit(f"LAF columns: '{laf_col_p}' | '{laf_col_s}'")
            else:
                self.progress.emit("No LAF column found — LAF matching skipped.")

            # Pre-extract LAF as plain arrays — avoids pandas overhead in the loop
            parcel_laf_arr = (parcels[laf_col_p].astype(str).str.strip().values
                              if laf_col_p else None)

            n            = len(parcels)
            parcel_geoms = parcels.geometry.values
            report_step  = max(1, n // 20)

            # ── Statistics counters ───────────────────────────────────────────
            n_null          = 0   # null / empty input geometries
            n_clean         = 0   # no overlap at all — passed through unchanged
            n_conflict      = 0   # at least one overlap found
            n_geom_changed  = 0   # geometry was actually modified
            n_geom_fallback = 0   # conflict but output == input (parcel fully inside zone)
            n_split         = 0   # produced 2+ output pieces from one input
            area_before_m2  = 0.0
            count_river     = 0
            count_road      = 0
            count_water     = 0
            count_surveyed  = 0
            combo_counts    = {}  # tuple-of-layer-names → parcel count

            # Output rows: one entry per resulting polygon piece
            out_orig_idx            = []
            out_geoms               = []
            out_comments            = []
            out_adjusted            = []
            out_orig_geoms          = []
            out_is_new              = []
            out_is_fully_overlapped = []

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
                """Subtract cutter from every piece; return (new_pieces, changed)."""
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
                        changed = True  # piece fully consumed by cutter
                # If every piece was consumed, fall back to originals so the parcel is not lost
                return (new_pieces if new_pieces else list(pieces)), changed

            # ── Main loop ─────────────────────────────────────────────────────
            self.progress.emit(f"Processing {n} parcels…")
            for i, geom in enumerate(parcel_geoms):
                if i % report_step == 0:
                    self.progress.emit(f"Processing {i}/{n}  ({100 * i // n}%)…")

                # 1. Null / empty — pass through, no original WKT
                if geom is None or geom.is_empty:
                    n_null += 1
                    out_orig_idx.append(i)
                    out_geoms.append(geom)
                    out_comments.append(_make_overlap_comment(set()))
                    out_adjusted.append(False)
                    out_orig_geoms.append(None)
                    out_is_new.append(False)
                    out_is_fully_overlapped.append(False)
                    continue

                orig_wkt       = geom.wkt
                area_before_m2 += geom.area

                # 2. Find spatially overlapping surveyed parcels
                cand_surv = []
                if surveyed_sindex is not None:
                    cand_surv = list(surveyed_sindex.intersection(geom.bounds))
                    if cand_surv:
                        cand_surv = [ci for ci in cand_surv
                                     if surveyed_repaired[ci].intersects(geom)]

                # 3. Classify surveyed overlaps: same-parcel match vs real conflict
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

                # 4. Per-parcel river lookup
                hits_river   = False
                local_rivers = None
                if rivers_sindex is not None:
                    cand_river = list(rivers_sindex.intersection(geom.bounds))
                    if cand_river:
                        cand_river = [ri for ri in cand_river
                                      if buf_river_geoms[ri].intersects(geom)]
                    hits_river = len(cand_river) > 0
                    if hits_river:
                        local_rivers = (
                            buf_river_geoms[cand_river[0]]
                            if len(cand_river) == 1
                            else unary_union([buf_river_geoms[ri] for ri in cand_river])
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

                # 5b. Per-parcel waterbody lookup
                hits_water   = False
                local_waters = None
                if wb_sindex is not None:
                    cand_wb = list(wb_sindex.intersection(geom.bounds))
                    if cand_wb:
                        cand_wb = [wi for wi in cand_wb
                                   if buf_wb_geoms[wi].intersects(geom)]
                    hits_water = len(cand_wb) > 0
                    if hits_water:
                        local_waters = (
                            buf_wb_geoms[cand_wb[0]]
                            if len(cand_wb) == 1
                            else unary_union([buf_wb_geoms[wi] for wi in cand_wb])
                        )

                hits_neighbour = len(conflict_geoms) > 0

                # Track per-layer counts
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

                # 6. No conflicts — pass through unchanged
                if not overlap_set:
                    n_clean += 1
                    out_orig_idx.append(i)
                    out_geoms.append(geom)
                    out_comments.append(comment)
                    out_adjusted.append(False)
                    out_orig_geoms.append(orig_wkt)
                    out_is_new.append(False)
                    out_is_fully_overlapped.append(False)
                    continue

                # Track overlap combination and conflict count
                n_conflict += 1
                combo_key = tuple(lyr for lyr in ['river', 'road', 'waterbody', 'Surveyed land']
                                  if lyr in overlap_set)
                combo_counts[combo_key] = combo_counts.get(combo_key, 0) + 1

                # 7. Sequential splitting: river → road → waterbody → surveyed land
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

                # Drop slivers (<40 m²) from split results, keeping ≥1 piece
                if len(pieces) > 1:
                    large = [p for p in pieces if p.area >= 200.0]
                    if large:
                        pieces = large

                # Sort largest first so is_new=False always marks the biggest piece
                if len(pieces) > 1:
                    n_split += 1
                    pieces.sort(key=lambda p: p.area, reverse=True)

                # Determine whether geometry actually changed
                geom_changed = (len(pieces) > 1) or (not pieces[0].equals(geom))

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
                    out_is_fully_overlapped.append(not geom_changed)

            # ── Rebuild GeoDataFrame ──────────────────────────────────────────
            self.progress.emit(f"Processing {n}/{n} (100%)… building output…")

            non_geom_cols = [c for c in parcels.columns if c != parcels.geometry.name]
            attrs = parcels[non_geom_cols].iloc[out_orig_idx].copy().reset_index(drop=True)
            attrs['comment']             = out_comments
            attrs['geometry_adjusted']   = out_adjusted
            attrs['original_geometry']   = out_orig_geoms
            attrs['is_new']              = out_is_new
            attrs['is_fully_overlapped'] = out_is_fully_overlapped
            result = gpd.GeoDataFrame(attrs, geometry=out_geoms, crs=target_crs)

            # Area after processing — exclude null/empty output geometries
            area_after_m2 = float(
                result.geometry[result.geometry.notna() & ~result.geometry.is_empty].area.sum()
            )

            # ── Save ─────────────────────────────────────────────────────────
            self.progress.emit("Saving output…")
            result.to_file(self.output_path, driver="GPKG")

            # ── ref_clean_layers ──────────────────────────────────────────────
            self.progress.emit("Building ref_clean_layers…")
            try:
                rows = []
                if buf_river_geoms:
                    for g in buf_river_geoms:
                        if g is not None and not g.is_empty:
                            rows.append({'geometry': g, 'origin': 'river'})
                if buf_road_geoms:
                    for g in buf_road_geoms:
                        if g is not None and not g.is_empty:
                            rows.append({'geometry': g, 'origin': 'road'})
                if buf_wb_geoms:
                    for g in buf_wb_geoms:
                        if g is not None and not g.is_empty:
                            rows.append({'geometry': g, 'origin': 'waterbody'})
                if self.surveyed_src:
                    for g in gdf_surveyed.geometry:
                        if g is not None and not g.is_empty:
                            rows.append({'geometry': g, 'origin': 'surveyed_land'})
                if rows:
                    import geopandas as gpd
                    c_gdf = gpd.GeoDataFrame(rows, crs=target_crs)
                    c_path = os.path.join(
                        os.path.dirname(self.output_path), 'ref_clean_layers.gpkg'
                    )
                    c_gdf.to_file(c_path, driver='GPKG', layer='ref_clean_layers')
                    self.constraints.emit(c_path)
                else:
                    self.progress.emit("No reference layers to combine — skipping ref_clean_layers.")
            except Exception as e:
                self.progress.emit(f"ref_clean_layers warning: {e}")

            pdf_path = os.path.splitext(self.output_path)[0] + '.pdf'
            self.progress.emit("Generating PDF report…")
            _generate_topo_pdf(pdf_path, {
                'generated_at':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'output_path':     self.output_path,
                'total_input':     n,
                'n_null':          n_null,
                'n_clean':         n_clean,
                'n_conflict':      n_conflict,
                'n_geom_changed':  n_geom_changed,
                'n_geom_fallback': n_geom_fallback,
                'n_split':         n_split,
                'total_output':    len(result),
                'area_before_m2':  area_before_m2,
                'area_after_m2':   area_after_m2,
                'layer_counts': {
                    'river':         count_river,
                    'road':          count_road,
                    'waterbody':     count_water,
                    'Surveyed land': count_surveyed,
                },
                'combo_counts':  combo_counts,
                'comment_counts': result['comment'].value_counts().to_dict(),
                'params': {
                    'river_buf_m': self.river_buffer_m,
                    'road_buf_m':  self.road_buffer_m,
                    'wb_buf_m':    self.wb_buffer_m,
                },
            })

            summary = (
                f"Done.  {n} input parcels → {len(result)} output features  "
                f"({n_geom_changed} adjusted, {n_split} split)\n"
                f"  Saved  → {self.output_path}\n"
                f"  Report → {pdf_path}"
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

        def layer_row(label_text, allow_none=False, line=False):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(120)
            combo = QgsMapLayerComboBox()
            combo.setFilters(
                QgsMapLayerProxyModel.Filter.LineLayer
                if line else
                QgsMapLayerProxyModel.Filter.PolygonLayer
            )
            combo.setFixedHeight(inputheight)
            if allow_none:
                combo.setAllowEmptyLayer(True)
                combo.setCurrentIndex(0)
            row.addWidget(lbl)
            row.addWidget(combo)
            layout.addLayout(row)
            return combo

        self.cmb_parcels    = layer_row('Parcels to solve:')
        self.cmb_rivers     = layer_row('Rivers layer:',   allow_none=True, line=True)
        self.cmb_roads      = layer_row('Roads layer:',    allow_none=True, line=True)
        self.cmb_waterbody  = layer_row('Waterbodies:',    allow_none=True)
        self.cmb_surveyed   = layer_row('Surveyed land:',  allow_none=True)

        # Output path — stays as a file picker
        out_row = QHBoxLayout()
        lbl_out = QLabel('Output path:')
        lbl_out.setFixedWidth(120)
        self.wgt_output = QgsFileWidget()
        self.wgt_output.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
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

        self.spn_river_buf = spin_row('River buffer (m):',        0, 10000, 30, 1)
        self.spn_road_buf  = spin_row('Road buffer (m):',         0, 10000, 5,  1)
        self.spn_wb_buf    = spin_row('Waterbody buffer (m):',    0, 10000, 30, 1)
        self.spn_overlap   = spin_row('Min overlap % to adjust:', 0,   100, 90,  1, ' %')
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
        self._auto_select_layers()
        QgsProject.instance().layersAdded.connect(self._on_layers_added)

    def _on_layers_added(self, _):
        if not sip.isdeleted(self):
            self._auto_select_layers()

    def closeEvent(self, event):
        try:
            QgsProject.instance().layersAdded.disconnect(self._on_layers_added)
        except RuntimeError:
            pass
        super().closeEvent(event)

    def _auto_select_layers(self):
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
            except RuntimeError:
                pass

    def _set_status(self, message, color="green"):
        self.response.setStyleSheet(f"color: {color};")
        self.response.setText(message)

    def _run(self):
        parcels_layer    = self.cmb_parcels.currentLayer()
        rivers_layer     = self.cmb_rivers.currentLayer()
        roads_layer      = self.cmb_roads.currentLayer()      # optional
        waterbody_layer  = self.cmb_waterbody.currentLayer()  # optional
        surveyed_layer   = self.cmb_surveyed.currentLayer()
        output_path      = self.wgt_output.filePath()

        if not parcels_layer:
            self._set_status("Parcels layer is required!", "red"); return
        if not output_path:
            self._set_status("Output path is required!", "red"); return

        self.run_btn.setEnabled(False)
        self._set_status("Running…", "orange")
        self._output_path = output_path

        self._thread = QThread()
        self._worker = _Worker(
            parcels_src          = parcels_layer.source(),
            rivers_src           = rivers_layer.source()    if rivers_layer    else None,
            roads_src            = roads_layer.source()     if roads_layer     else None,
            waterbodies_src      = waterbody_layer.source() if waterbody_layer else None,
            surveyed_src         = surveyed_layer.source()  if surveyed_layer  else None,
            output_path          = output_path,
            river_buffer_m       = self.spn_river_buf.value(),
            road_buffer_m        = self.spn_road_buf.value(),
            wb_buffer_m          = self.spn_wb_buf.value(),
            min_overlap_pct      = self.spn_overlap.value(),
            sliver_threshold_m2  = self.spn_sliver.value(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda msg: self._set_status(msg, "orange"))
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.constraints.connect(self._on_constraints)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(lambda: self.run_btn.setEnabled(True))
        self._thread.start()

    def _on_finished(self, summary):
        self._set_status(summary, "green")
        self._load_output()
        self._load_constraints()

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

    def _on_constraints(self, path):
        self._constraints_path = path

    def _load_constraints(self):
        import os
        from qgis.core import (
            QgsVectorLayer, QgsProject,
            QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsFillSymbol,
        )
        path = getattr(self, '_constraints_path', None)
        if not path or not os.path.exists(path):
            return
        uri = f"{path}|layername=ref_clean_layers"
        layer = QgsVectorLayer(uri, 'ref_clean_layers', 'ogr')
        if not layer.isValid():
            return

        # R,G,B,A  (A = 0-255; 120 ≈ 47 % opacity)
        _STYLES = [
            ('river',         '26,110,191,120',   '#1A6EBF'),
            ('road',          '192,57,43,120',     '#C0392B'),
            ('waterbody',     '22,160,133,120',    '#16A085'),
            ('surveyed_land', '125,60,152,120',    '#7D3C98'),
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

    def _on_error(self, msg):
        self._set_status(f"Error: {msg}", "red")
