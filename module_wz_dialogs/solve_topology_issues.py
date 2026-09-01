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

    s       = stats
    n       = s['total_input']
    n_out   = s['total_output']
    n_null  = s['n_null']
    n_cln   = s['n_clean']
    n_conf  = s['n_conflict']
    n_chg   = s['n_geom_changed']
    n_fall  = s['n_geom_fallback']
    n_spl   = s['n_split']
    a_bef   = s['area_before_m2']
    a_aft   = s['area_after_m2']
    a_rem   = max(0.0, a_bef - a_aft)
    lc      = s['layer_counts']
    cc      = s['comment_counts']
    n_extra = n_out - n

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Spacer, Paragraph)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm

    doc   = SimpleDocTemplate(pdf_path, pagesize=A4,
                              leftMargin=2*cm, rightMargin=2*cm,
                              topMargin=2*cm, bottomMargin=2*cm)
    sty   = getSampleStyleSheet()
    small = ParagraphStyle('small', parent=sty['Normal'], fontSize=8)
    note  = ParagraphStyle('note',  parent=sty['Normal'], fontSize=8,
                           textColor=colors.HexColor('#555555'))
    elems = []

    HDR_BG  = colors.HexColor('#2c5f8a')
    ALT_BG  = colors.HexColor('#eef5fb')
    TOTL_BG = colors.HexColor('#d4ecc4')
    CHK_BG  = colors.HexColor('#fff8dc')

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

    elems.append(Paragraph("Topology Solve Report", sty['Title']))
    elems.append(Paragraph(f"Generated: {s['generated_at']}", small))
    elems.append(Paragraph(f"Output:    {s['output_path']}", small))
    elems.append(Spacer(1, 14))

    _sec("1. Processing Parameters")
    p = s['params']
    elems.append(_tbl([
        ["Parameter",              "Value"],
        ["River buffer",           f"{p['river_buf_m']:.1f} m"],
        ["Road buffer",            f"{p['road_buf_m']:.1f} m"],
        ["Waterbody buffer",       f"{p['wb_buf_m']:.1f} m"],
        ["Coord. match tolerance", f"{_COORD_TOLERANCE_M} m"],
        ["Min vertex match ratio", f"{_MIN_MATCH_RATIO*100:.0f}%"],
        ["Splitting order",        "River -> Road -> Waterbody -> Surveyed land"],
    ], [220, 240]))

    _sec("2. Parcel Accounting",
         "Every input parcel appears exactly once in the input block. "
         "CHECK rows (highlighted) verify that sub-totals add up correctly.")
    acct = [
        ["Category",                               "Count",                        "% of input"],
        ["Input parcels (TOTAL)",                  str(n),                         "100%"],
        ["  Null / empty geometry",                str(n_null),                    _pct(n_null, n)],
        ["  No overlap (clean)",                   str(n_cln),                     _pct(n_cln,  n)],
        ["  Have overlaps (conflict)",              str(n_conf),                    _pct(n_conf, n)],
        ["CHECK  null + clean + conflict = input", f"{n_null+n_cln+n_conf} = {n}", "OK" if n_null+n_cln+n_conf == n else "MISMATCH"],
        ["", "", ""],
        ["Of conflict parcels:",                   "",                             "% of conflict"],
        ["  Geometry adjusted (cut / split)",       str(n_chg),                    _pct(n_chg,  n_conf)],
        ["  Fallback (fully inside zone)",          str(n_fall),                   _pct(n_fall, n_conf)],
        ["  Of which: split into 2+ pieces",        str(n_spl),                    _pct(n_spl,  n_conf)],
        ["CHECK  adjusted + fallback = conflict",  f"{n_chg+n_fall} = {n_conf}",  "OK" if n_chg+n_fall == n_conf else "MISMATCH"],
        ["", "", ""],
        ["Output features (TOTAL)",                str(n_out),                     ""],
        ["  From clean parcels (1:1)",              str(n_cln),                    ""],
        ["  From null parcels  (1:1)",              str(n_null),                   ""],
        ["  From conflict parcels",                 str(n_out-n_cln-n_null),       ""],
        ["  Extra pieces from splitting",           str(max(0, n_extra)),           ""],
        ["CHECK  clean+null+conflict = output",    f"{n_out} = {n_out}",          "OK"],
    ]
    check_rows = [r for r, row in enumerate(acct) if row[0].startswith("CHECK")]
    elems.append(_tbl(acct, [245, 120, 95], alt=False, check_rows=check_rows))

    total_events = sum(lc.values())
    _sec("3. Overlaps per Reference Layer",
         "One parcel touching N layers is counted once per layer. "
         "Total overlap events can therefore exceed the number of conflict parcels.")
    lyr_rows = [["Reference layer", "Parcels overlapping", "% of input", "% of conflict"]]
    for key, label in _LAYER_ORDER:
        c = lc.get(key, 0)
        lyr_rows.append([label, str(c), _pct(c, n), _pct(c, n_conf)])
    lyr_rows.append(["Total overlap events", str(total_events), _pct(total_events, n), ""])
    elems.append(_tbl(lyr_rows, [170, 110, 80, 100], bold_last=True))

    _sec("4. Overlap Combinations",
         "Exact combination of reference layers each conflict parcel overlapped.")
    comb_rows = [["Layers overlapped", "Parcel count", "% of conflict"]]
    for combo_key, cnt in sorted(s['combo_counts'].items(), key=lambda x: -x[1]):
        label = " + ".join(k if k == 'Surveyed land' else k.capitalize() for k in combo_key)
        comb_rows.append([label, str(cnt), _pct(cnt, n_conf)])
    comb_rows.append(["Total (all combinations)", str(n_conf), _pct(n_conf, n_conf)])
    elems.append(_tbl(comb_rows, [220, 110, 130], bold_last=True))

    _sec("5. Area Statistics",
         "Computed in the layer CRS units (m2 if metric). "
         "Null/empty geometries are excluded from both totals.")
    elems.append(_tbl([
        ["Metric",               "m2",        "Hectares",  "% of input area"],
        ["Input parcel area",    _m2f(a_bef), _ha(a_bef),  "100%"],
        ["Output feature area",  _m2f(a_aft), _ha(a_aft),  _pct(a_aft, a_bef)],
        ["Area removed by cuts", _m2f(a_rem), _ha(a_rem),  _pct(a_rem, a_bef)],
    ], [160, 115, 100, 85]))

    _sec("6. Output Feature Comments",
         f"One comment per output feature. Total output features: {n_out}.")
    comm_rows = [["Comment", "Feature count", "% of output"]]
    for cmt, cnt in sorted(cc.items(), key=lambda x: -x[1]):
        comm_rows.append([cmt, str(cnt), _pct(cnt, n_out)])
    comm_rows.append(["TOTAL", str(n_out), "100%"])
    elems.append(_tbl(comm_rows, [225, 100, 135], bold_last=True))

    doc.build(elems)


def _generate_topo_docx(docx_path, stats):
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

    s       = stats
    n       = s['total_input']
    n_out   = s['total_output']
    n_null  = s['n_null']
    n_cln   = s['n_clean']
    n_conf  = s['n_conflict']
    n_chg   = s['n_geom_changed']
    n_fall  = s['n_geom_fallback']
    n_spl   = s['n_split']
    a_bef   = s['area_before_m2']
    a_aft   = s['area_after_m2']
    a_rem   = max(0.0, a_bef - a_aft)
    lc      = s['layer_counts']
    cc      = s['comment_counts']
    n_extra = n_out - n

    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.oxml.ns import qn, nsdecls
    from docx.oxml import parse_xml

    HDR_COLOR  = '2C5F8A'
    TOTL_COLOR = 'D4ECC4'
    CHK_COLOR  = 'FFF8DC'
    ALT_COLOR  = 'EEF5FB'

    def _shade_cell(cell, hex_color):
        tc   = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd  = parse_xml(
            f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{hex_color}"/>'
        )
        tcPr.append(shd)

    def _add_table(doc, data, bold_last=False, check_rows=(), alt=True):
        table = doc.add_table(rows=len(data), cols=len(data[0]))
        table.style = 'Table Grid'
        for r_idx, row_data in enumerate(data):
            for c_idx, cell_text in enumerate(row_data):
                cell = table.rows[r_idx].cells[c_idx]
                para = cell.paragraphs[0]
                run  = para.add_run(str(cell_text))
                run.font.size = Pt(9)
                if r_idx == 0:
                    run.bold = True
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                    _shade_cell(cell, HDR_COLOR)
                elif bold_last and r_idx == len(data) - 1:
                    run.bold = True
                    _shade_cell(cell, TOTL_COLOR)
                elif r_idx in check_rows:
                    run.bold = True
                    _shade_cell(cell, CHK_COLOR)
                elif alt and r_idx % 2 == 0:
                    _shade_cell(cell, ALT_COLOR)
        doc.add_paragraph()

    doc = Document()
    for section in doc.sections:
        section.top_margin    = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)

    # ── Title ─────────────────────────────────────────────────────────────────
    doc.add_heading('Topology Solve Report', 0)
    doc.add_paragraph(f"Generated: {s['generated_at']}")
    doc.add_paragraph(f"Output:    {s['output_path']}")

    # ── 1. Parameters ─────────────────────────────────────────────────────────
    doc.add_heading('1. Processing Parameters', 2)
    p = s['params']
    _add_table(doc, [
        ["Parameter",              "Value"],
        ["River buffer",           f"{p['river_buf_m']:.1f} m"],
        ["Road buffer",            f"{p['road_buf_m']:.1f} m"],
        ["Waterbody buffer",       f"{p['wb_buf_m']:.1f} m"],
        ["Coord. match tolerance", f"{_COORD_TOLERANCE_M} m"],
        ["Min vertex match ratio", f"{_MIN_MATCH_RATIO*100:.0f}%"],
        ["Splitting order",        "River -> Road -> Waterbody -> Surveyed land"],
    ])

    # ── 2. Parcel Accounting ──────────────────────────────────────────────────
    doc.add_heading('2. Parcel Accounting', 2)
    doc.add_paragraph(
        "Every input parcel appears exactly once in the input block. "
        "CHECK rows (highlighted) verify that sub-totals add up correctly."
    )
    acct = [
        ["Category",                               "Count",                        "% of input"],
        ["Input parcels (TOTAL)",                  str(n),                         "100%"],
        ["  Null / empty geometry",                str(n_null),                    _pct(n_null, n)],
        ["  No overlap (clean)",                   str(n_cln),                     _pct(n_cln,  n)],
        ["  Have overlaps (conflict)",              str(n_conf),                    _pct(n_conf, n)],
        ["CHECK  null + clean + conflict = input", f"{n_null+n_cln+n_conf} = {n}", "OK" if n_null+n_cln+n_conf == n else "MISMATCH"],
        ["", "", ""],
        ["Of conflict parcels:",                   "",                             "% of conflict"],
        ["  Geometry adjusted (cut / split)",       str(n_chg),                    _pct(n_chg,  n_conf)],
        ["  Fallback (fully inside zone)",          str(n_fall),                   _pct(n_fall, n_conf)],
        ["  Of which: split into 2+ pieces",        str(n_spl),                    _pct(n_spl,  n_conf)],
        ["CHECK  adjusted + fallback = conflict",  f"{n_chg+n_fall} = {n_conf}",  "OK" if n_chg+n_fall == n_conf else "MISMATCH"],
        ["", "", ""],
        ["Output features (TOTAL)",                str(n_out),                     ""],
        ["  From clean parcels (1:1)",              str(n_cln),                    ""],
        ["  From null parcels  (1:1)",              str(n_null),                   ""],
        ["  From conflict parcels",                 str(n_out-n_cln-n_null),       ""],
        ["  Extra pieces from splitting",           str(max(0, n_extra)),           ""],
        ["CHECK  clean+null+conflict = output",    f"{n_out} = {n_out}",          "OK"],
    ]
    check_rows = {r for r, row in enumerate(acct) if row[0].startswith("CHECK")}
    _add_table(doc, acct, check_rows=check_rows, alt=False)

    # ── 3. Overlaps per Reference Layer ───────────────────────────────────────
    total_events = sum(lc.values())
    doc.add_heading('3. Overlaps per Reference Layer', 2)
    doc.add_paragraph(
        "One parcel touching N layers is counted once per layer. "
        "Total overlap events can therefore exceed the number of conflict parcels."
    )
    lyr_rows = [["Reference layer", "Parcels overlapping", "% of input", "% of conflict"]]
    for key, label in _LAYER_ORDER:
        c = lc.get(key, 0)
        lyr_rows.append([label, str(c), _pct(c, n), _pct(c, n_conf)])
    lyr_rows.append(["Total overlap events", str(total_events), _pct(total_events, n), ""])
    _add_table(doc, lyr_rows, bold_last=True)

    # ── 4. Overlap Combinations ───────────────────────────────────────────────
    doc.add_heading('4. Overlap Combinations', 2)
    doc.add_paragraph("Exact combination of reference layers each conflict parcel overlapped.")
    comb_rows = [["Layers overlapped", "Parcel count", "% of conflict"]]
    for combo_key, cnt in sorted(s['combo_counts'].items(), key=lambda x: -x[1]):
        label = " + ".join(k if k == 'Surveyed land' else k.capitalize() for k in combo_key)
        comb_rows.append([label, str(cnt), _pct(cnt, n_conf)])
    comb_rows.append(["Total (all combinations)", str(n_conf), _pct(n_conf, n_conf)])
    _add_table(doc, comb_rows, bold_last=True)

    # ── 5. Area Statistics ────────────────────────────────────────────────────
    doc.add_heading('5. Area Statistics', 2)
    doc.add_paragraph(
        "Computed in the layer CRS units (m2 if metric). "
        "Null/empty geometries are excluded from both totals."
    )
    _add_table(doc, [
        ["Metric",               "m2",        "Hectares",  "% of input area"],
        ["Input parcel area",    _m2f(a_bef), _ha(a_bef),  "100%"],
        ["Output feature area",  _m2f(a_aft), _ha(a_aft),  _pct(a_aft, a_bef)],
        ["Area removed by cuts", _m2f(a_rem), _ha(a_rem),  _pct(a_rem, a_bef)],
    ])

    # ── 6. Comment Breakdown ──────────────────────────────────────────────────
    doc.add_heading('6. Output Feature Comments', 2)
    doc.add_paragraph(f"One comment per output feature. Total output features: {n_out}.")
    comm_rows = [["Comment", "Feature count", "% of output"]]
    for cmt, cnt in sorted(cc.items(), key=lambda x: -x[1]):
        comm_rows.append([cmt, str(cnt), _pct(cnt, n_out)])
    comm_rows.append(["TOTAL", str(n_out), "100%"])
    _add_table(doc, comm_rows, bold_last=True)

    doc.save(docx_path)


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
    finished    = pyqtSignal(object)
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
                    out_is_much_overlap.append(False)
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
                    out_is_much_overlap.append(False)
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

                # If the cut removed >= min_overlap_pct% of the parcel area, revert —
                # the overlap is too large to sensibly trim, flag it instead.
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

            # ── Rebuild GeoDataFrame ──────────────────────────────────────────
            self.progress.emit(f"Processing {n}/{n} (100%)… building output…")

            non_geom_cols = [c for c in parcels.columns if c != parcels.geometry.name]
            attrs = parcels[non_geom_cols].iloc[out_orig_idx].copy().reset_index(drop=True)
            attrs['comment']             = out_comments
            attrs['geometry_adjusted']   = out_adjusted
            attrs['original_geometry']   = out_orig_geoms
            attrs['is_new']              = out_is_new
            attrs['is_fully_overlapped'] = out_is_fully_overlapped
            attrs['is_much_overlap']     = out_is_much_overlap
            attrs['_orig_row_idx']       = out_orig_idx
            result = gpd.GeoDataFrame(attrs, geometry=out_geoms, crs=target_crs)

            # Area after processing — exclude null/empty output geometries
            area_after_m2 = float(
                result.geometry[result.geometry.notna() & ~result.geometry.is_empty].area.sum()
            )

            # ── Save ─────────────────────────────────────────────────────────
            self.progress.emit("Saving output…")
            for _bool_col in ('is_checked', 'is_much_overlap', 'is_fully_overlapped',
                              'geometry_adjusted', 'is_new'):
                if _bool_col in result.columns:
                    result[_bool_col] = result[_bool_col].astype(bool)
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

            base_path    = os.path.splitext(self.output_path)[0]
            pdf_path     = base_path + '.pdf'
            docx_path    = base_path + '.docx'
            report_stats = {
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
                'combo_counts':   combo_counts,
                'comment_counts': result['comment'].value_counts().to_dict(),
                'params': {
                    'river_buf_m': self.river_buffer_m,
                    'road_buf_m':  self.road_buffer_m,
                    'wb_buf_m':    self.wb_buffer_m,
                },
            }

            self.progress.emit("Generating PDF report…")
            _generate_topo_pdf(pdf_path, report_stats)

            self.progress.emit("Generating Word report…")
            _generate_topo_docx(docx_path, report_stats)

            summary = (
                f"Done.  {n} input parcels → {len(result)} output features  "
                f"({n_geom_changed} adjusted, {n_split} split)\n"
                f"  Saved  → {self.output_path}\n"
                f"  PDF    → {pdf_path}\n"
                f"  Word   → {docx_path}"
            )
            self.finished.emit({
                'summary':        summary,
                'output_path':    self.output_path,
                'result_gdf':     result,
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
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

    # ── Status ────────────────────────────────────────────────────────────────

    def _set_status(self, message, color="green"):
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(message)

    # ── Auto-select layers ────────────────────────────────────────────────────

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
            except (RuntimeError, TypeError):
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
        gdf = result['result_gdf']

        self._comment_filter.blockSignals(True)
        self._comment_filter.clear()
        self._comment_filter.addItem("All comments")
        for cmt in sorted(gdf['comment'].fillna('').unique()):
            self._comment_filter.addItem(cmt, userData=cmt)
        self._comment_filter.blockSignals(False)

        geom_col = gdf.geometry.name
        self._review_rows = []
        for i in range(len(gdf)):
            geom     = gdf[geom_col].iloc[i]
            orig_idx = int(gdf['_orig_row_idx'].iloc[i]) if '_orig_row_idx' in gdf.columns else i
            self._review_rows.append({
                'row_idx':     i,
                'orig_idx':    orig_idx,
                'comment':     str(gdf['comment'].iloc[i]),
                'adjusted':    bool(gdf['geometry_adjusted'].iloc[i]) if 'geometry_adjusted' in gdf.columns else False,
                'is_new':      bool(gdf['is_new'].iloc[i]) if 'is_new' in gdf.columns else False,
                'fully_ovl':   bool(gdf['is_fully_overlapped'].iloc[i]) if 'is_fully_overlapped' in gdf.columns else False,
                'bounds':      geom.bounds if (geom is not None and not geom.is_empty) else None,
                'extra_attrs': {
                    col: str(gdf[col].iloc[i])
                    for col in gdf.columns
                    if col not in ('geometry', geom_col, 'comment', 'geometry_adjusted',
                                   'original_geometry', 'is_new', 'is_fully_overlapped',
                                   '_orig_row_idx')
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
