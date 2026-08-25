# -*- coding: utf-8 -*-
"""
Parcel Correction & Overlap Classification Tool

Splits parcels that straddle reference-layer boundaries (roads, rivers,
waterbodies, surveyed land), tags every resulting polygon with an overlap
comment, saves a corrected GeoPackage, and generates an HTML report.
"""
import warnings
warnings.filterwarnings("ignore")

import os
from datetime import datetime

from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject, Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QPushButton, QComboBox, QCheckBox, QSplitter,
    QTreeWidget, QTreeWidgetItem, QTextBrowser, QGroupBox,
    QLineEdit, QSizePolicy, QDoubleSpinBox,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.core import (
    QgsMapLayerProxyModel, QgsProject, QgsVectorLayer,
    QgsRectangle, QgsFeatureRequest,
)

# ── Layer ordering — fixes comment text and report ordering ──────────────────
LAYER_NAMES = ['road', 'river', 'waterbody', 'Surveyed land']
FLAG_KEYS   = ['road', 'river', 'waterbody', 'surveyed_land']

_MAX_ITER      = 6    # splitting iterations before giving up
_MAX_FRAGMENTS = 200  # safety cap on fragments per parcel


def _get_iface():
    try:
        from qgis.utils import iface
        return iface
    except Exception:
        return None


# ── Comment / flag helpers ────────────────────────────────────────────────────

def _comment_from_flags(flags_str: str) -> str:
    """Generate human-readable comment from comma-separated overlap flags string."""
    if not flags_str:
        return "Plot is fine."
    keys_present = [k for k in FLAG_KEYS if k in flags_str.split(',')]
    ordered = [LAYER_NAMES[FLAG_KEYS.index(k)] for k in keys_present]
    n = len(ordered)
    if n == 1:
        return f"Overlap {ordered[0]}."
    if n == 2:
        return f"Overlap {ordered[0]} and {ordered[1]}."
    if n == 3:
        return f"Overlap {ordered[0]}, {ordered[1]} and {ordered[2]}."
    return f"Overlap {ordered[0]}, {ordered[1]}, {ordered[2]} and {ordered[3]}."


def _flags_from_set(overlap_set: set) -> str:
    return ','.join(k for k in FLAG_KEYS if k in overlap_set)


# ── Source reader (handles GeoPackage layer URIs) ─────────────────────────────

def _read_source(source):
    import geopandas as gpd
    if '|layername=' in source:
        path, rest = source.split('|', 1)
        layername = rest.split('layername=', 1)[1].split('|')[0]
        return gpd.read_file(path, layer=layername)
    return gpd.read_file(source)


def _safe_to_crs(gdf, target_crs):
    """Reproject gdf; if it has no CRS (naive geometries), assign target_crs directly."""
    if gdf.crs is None:
        return gdf.set_crs(target_crs)
    return gdf.to_crs(target_crs)


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _geom_type_of(gdf) -> str:
    """Return 'line' or 'polygon' for the dominant geometry type in gdf."""
    for g in gdf.geometry:
        if g is None or g.is_empty:
            continue
        t = g.geom_type.lower()
        if 'line' in t:
            return 'line'
        return 'polygon'
    return 'polygon'


def _repair(geom):
    try:
        fixed = geom.buffer(0)
        return fixed if not fixed.is_empty else geom
    except Exception:
        return geom


def _split_by_polygon(poly, ref_poly):
    """Split poly into inside/outside parts relative to ref_poly."""
    parts = []
    try:
        inside  = poly.intersection(ref_poly)
        outside = poly.difference(ref_poly)
    except Exception:
        try:
            poly     = _repair(poly)
            ref_poly = _repair(ref_poly)
            inside  = poly.intersection(ref_poly)
            outside = poly.difference(ref_poly)
        except Exception:
            return [poly]
    for g in (inside, outside):
        if g is None or g.is_empty:
            continue
        if hasattr(g, 'geoms'):
            parts.extend(p for p in g.geoms if p.area > 1e-12)
        elif g.area > 1e-12:
            parts.append(g)
    return parts if len(parts) >= 2 else [poly]


def _split_by_line(poly, line):
    """Split poly along a line (shapely.ops.split with buffer fallback)."""
    from shapely.ops import split as shp_split
    try:
        result = shp_split(_repair(poly), line)
        parts  = [g for g in result.geoms if g.area > 1e-12]
        if len(parts) >= 2:
            return parts
    except Exception:
        pass
    # Fallback: thin buffer around the line acts as a polygon splitter
    try:
        eps  = max(poly.length * 1e-6, 1e-4)
        thin = line.buffer(eps)
        return _split_by_polygon(poly, thin)
    except Exception:
        return [poly]


def _has_area_overlap(poly, ref_geom, gtype: str) -> bool:
    """True if poly has a meaningful intersection with ref_geom."""
    try:
        if gtype == 'line':
            return (poly.crosses(ref_geom) or
                    (poly.intersects(ref_geom) and not poly.touches(ref_geom)))
        return poly.intersection(ref_geom).area > 1e-10
    except Exception:
        return False


def _straddles_boundary(poly, ref_geom, gtype: str) -> bool:
    """
    True if poly crosses the boundary of ref_geom — i.e. it is partly inside
    and partly outside (the condition that requires splitting).
    """
    try:
        if gtype == 'line':
            return (poly.crosses(ref_geom) or
                    (poly.intersects(ref_geom) and not poly.touches(ref_geom)))
        inter_area = poly.intersection(ref_geom).area
        return inter_area > 1e-10 and inter_area < poly.area * (1.0 - 1e-9)
    except Exception:
        return False


def _process_parcel(parcel_geom, ordered_refs):
    """
    Iteratively split a parcel until no fragment straddles any reference boundary.
    ordered_refs: list of (flag_key, ref_gdf, geom_type_str)
    Returns list of (shapely_geom, overlap_set) tuples.
    """
    frags = [_repair(parcel_geom)]

    for _iteration in range(_MAX_ITER):
        changed = False

        for flag_key, ref_gdf, gtype in ordered_refs:
            next_frags = []
            for frag in frags:
                if len(next_frags) + len(frags) > _MAX_FRAGMENTS:
                    next_frags.append(frag)
                    continue

                cands = list(ref_gdf.sindex.intersection(frag.bounds))
                split_done = False

                for ci in cands:
                    ref_geom = ref_gdf.geometry.iloc[ci]
                    if _straddles_boundary(frag, ref_geom, gtype):
                        parts = (_split_by_line(frag, ref_geom)
                                 if gtype == 'line'
                                 else _split_by_polygon(frag, ref_geom))
                        if len(parts) >= 2:
                            next_frags.extend(parts)
                            changed = True
                            split_done = True
                            break

                if not split_done:
                    next_frags.append(frag)

            frags = next_frags

        if not changed:
            break  # stable — no fragment crosses any boundary

    # Final overlap detection
    result = []
    for frag in frags:
        overlap_set = set()
        for flag_key, ref_gdf, gtype in ordered_refs:
            cands = list(ref_gdf.sindex.intersection(frag.bounds))
            for ci in cands:
                if _has_area_overlap(frag, ref_gdf.geometry.iloc[ci], gtype):
                    overlap_set.add(flag_key)
                    break
        result.append((frag, overlap_set))

    return result


# ── PDF Report generator ──────────────────────────────────────────────────────

def _generate_pdf_report(pdf_path, count_before, count_after, split_count,
                         layer_counts, combo_counts):
    """Save a PDF report alongside the output GeoPackage.
    Falls back to HTML if reportlab is not installed."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer)
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm

        styles = getSampleStyleSheet()
        doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                                   leftMargin=2*cm, rightMargin=2*cm,
                                   topMargin=2*cm, bottomMargin=2*cm)

        def _tbl(headers, rows, widths=None):
            if widths is None:
                widths = [13*cm, 3.5*cm]
            data = [headers] + rows
            t  = Table(data, colWidths=widths)
            ts = TableStyle([
                ('BACKGROUND',    (0, 0), (-1, 0), colors.HexColor('#2c5282')),
                ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
                ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE',      (0, 0), (-1,-1), 9),
                ('LEFTPADDING',   (0, 0), (-1,-1), 7),
                ('RIGHTPADDING',  (0, 0), (-1,-1), 7),
                ('TOPPADDING',    (0, 0), (-1,-1), 4),
                ('BOTTOMPADDING', (0, 0), (-1,-1), 4),
                ('GRID',          (0, 0), (-1,-1), 0.5, colors.HexColor('#a0aec0')),
            ])
            for ri in range(1, len(data)):
                bg = colors.HexColor('#edf2f7') if ri % 2 == 0 else colors.white
                ts.add('BACKGROUND', (0, ri), (-1, ri), bg)
            t.setStyle(ts)
            return t

        story = []
        story.append(Paragraph("Parcel Correction &amp; Overlap Report", styles['Title']))
        story.append(Paragraph(f"Generated: {now}", styles['Normal']))
        story.append(Spacer(1, 0.5*cm))

        story.append(Paragraph("Processing Summary", styles['Heading2']))
        story.append(_tbl(
            ['Metric', 'Count'],
            [
                ['Parcels before processing',  str(count_before)],
                ['Parcels after processing',   str(count_after)],
                ['New parcels from splitting', str(split_count)],
            ]
        ))
        story.append(Spacer(1, 0.4*cm))

        story.append(Paragraph("Per-Layer Overlap Counts", styles['Heading2']))
        story.append(Paragraph(
            "A parcel touching multiple layers is counted once per layer (not mutually exclusive).",
            styles['Normal']
        ))
        story.append(Spacer(1, 0.2*cm))
        story.append(_tbl(
            ['Layer', 'Parcels Overlapping'],
            [
                ['Road',          str(layer_counts.get('road',          0))],
                ['River',         str(layer_counts.get('river',         0))],
                ['Waterbody',     str(layer_counts.get('waterbody',     0))],
                ['Surveyed land', str(layer_counts.get('surveyed_land', 0))],
            ]
        ))
        story.append(Spacer(1, 0.4*cm))

        story.append(Paragraph("Combination Breakdown", styles['Heading2']))
        combo_rows = []
        for flags_str, cnt in sorted(combo_counts.items(), key=lambda x: -x[1]):
            label = _comment_from_flags(flags_str) if flags_str else "Plot is fine."
            combo_rows.append([label, str(cnt)])
        story.append(_tbl(['Overlap Comment', 'Count'], combo_rows))

        doc.build(story)
        return pdf_path

    except ImportError:
        # reportlab not installed — write HTML and ask user to print to PDF
        html_path = os.path.splitext(pdf_path)[0] + '.html'
        combo_rows_html = ""
        for flags_str, cnt in sorted(combo_counts.items(), key=lambda x: -x[1]):
            label = _comment_from_flags(flags_str) if flags_str else "Plot is fine."
            combo_rows_html += f"<tr><td>{label}</td><td>{cnt}</td></tr>\n"
        html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Parcel Correction Report</title>
<style>body{{font-family:Arial,sans-serif;margin:40px}}
table{{border-collapse:collapse;width:100%;max-width:700px;margin-bottom:20px}}
th,td{{border:1px solid #ccc;padding:8px 12px}}th{{background:#2c5282;color:#fff}}
tr:nth-child(even){{background:#f0f4f8}}</style></head><body>
<h1>Parcel Correction &amp; Overlap Report</h1>
<p><em>Generated: {now} — open in browser and print to PDF (reportlab not installed)</em></p>
<h2>Summary</h2><table><tr><th>Metric</th><th>Count</th></tr>
<tr><td>Parcels before</td><td>{count_before}</td></tr>
<tr><td>Parcels after</td><td>{count_after}</td></tr>
<tr><td>New from splitting</td><td>{split_count}</td></tr></table>
<h2>Per-Layer Counts</h2><table><tr><th>Layer</th><th>Count</th></tr>
<tr><td>Road</td><td>{layer_counts.get('road',0)}</td></tr>
<tr><td>River</td><td>{layer_counts.get('river',0)}</td></tr>
<tr><td>Waterbody</td><td>{layer_counts.get('waterbody',0)}</td></tr>
<tr><td>Surveyed land</td><td>{layer_counts.get('surveyed_land',0)}</td></tr></table>
<h2>Combination Breakdown</h2><table><tr><th>Comment</th><th>Count</th></tr>
{combo_rows_html}</table></body></html>"""
        with open(html_path, 'w', encoding='utf-8') as fh:
            fh.write(html)
        return html_path


# ── Background worker ─────────────────────────────────────────────────────────

class _Worker(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(object)   # dict payload
    error    = pyqtSignal(str)

    def __init__(self, parcels_src, ref_sources, output_path, report_path, buf_distances):
        super().__init__()
        self.parcels_src   = parcels_src
        # ref_sources: list of (flag_key, layer_label, source_string)
        self.ref_sources   = ref_sources
        self.output_path   = output_path
        self.report_path   = report_path
        # buf_distances: dict {flag_key: metres}  e.g. {'road': 5, 'waterbody': 30}
        self.buf_distances = buf_distances

    def run(self):
        try:
            import geopandas as gpd

            # ── Load primary parcels ──────────────────────────────────────────
            self.progress.emit("Loading parcel layer…")
            parcels    = _read_source(self.parcels_src)
            target_crs = parcels.crs
            if target_crs is None:
                self.error.emit("Parcel layer has no CRS. Assign a CRS before running.")
                return

            # Reserve column names — rename originals if they collide
            reserved = {'comment', 'overlap_flags', 'is_split_result', 'parent_parcel_id'}
            rename_map = {c: f'{c}_orig' for c in parcels.columns if c in reserved}
            if rename_map:
                parcels = parcels.rename(columns=rename_map)

            # Add an internal tracking ID before any explode
            parcels = parcels.copy()
            parcels['_pc_orig_id'] = range(len(parcels))

            # Explode MultiPolygon → simple polygons (preserves _pc_orig_id)
            parcels = parcels.explode(index_parts=False).reset_index(drop=True)
            count_before = int(parcels['_pc_orig_id'].nunique())

            # ── Load reference layers ─────────────────────────────────────────
            ordered_refs = []  # (flag_key, ref_gdf, gtype)
            for flag_key, label, src in self.ref_sources:
                if not src:
                    continue
                self.progress.emit(f"Loading {label}…")
                gdf   = _safe_to_crs(_read_source(src), target_crs)
                gdf   = gdf[~gdf.geometry.isna()].copy()
                gdf.geometry = gdf.geometry.apply(_repair)
                gtype = _geom_type_of(gdf)

                buf_m = float(self.buf_distances.get(flag_key, 0.0))
                if buf_m > 0:
                    self.progress.emit(f"  Buffering {label} by {buf_m} m…")
                    gdf = gdf.copy()
                    gdf.geometry = gdf.geometry.buffer(buf_m)
                    gdf = gdf[~gdf.geometry.is_empty].copy()
                    gtype = 'polygon'  # buffered geometry is always a polygon zone

                buf_note = f", buffer = {buf_m} m" if buf_m > 0 else ""
                self.progress.emit(f"  {label}: {len(gdf)} features, type = {gtype}{buf_note}")
                ordered_refs.append((flag_key, gdf, gtype))

            if not ordered_refs:
                self.error.emit("No reference layers provided.")
                return

            # ── Main processing loop ──────────────────────────────────────────
            n = len(parcels)
            self.progress.emit(f"Processing {n} parcels…")
            report_step = max(1, n // 20)

            rows = []          # list of attribute dicts (including 'geometry')
            geom_col = parcels.geometry.name

            for i in range(n):
                if i % report_step == 0:
                    self.progress.emit(f"Processing {i}/{n}  ({100 * i // n}%)…")

                parcel_geom = parcels[geom_col].iloc[i]
                orig_id     = str(parcels['_pc_orig_id'].iloc[i])
                base_attrs  = {
                    col: parcels[col].iloc[i]
                    for col in parcels.columns
                    if col not in ('geometry', geom_col, '_pc_orig_id')
                }

                if parcel_geom is None or parcel_geom.is_empty:
                    base_attrs.update({
                        'parent_parcel_id': orig_id,
                        'is_split_result':  False,
                        'overlap_flags':    '',
                        'comment':          'Plot is fine.',
                        'geometry':         parcel_geom,
                    })
                    rows.append(base_attrs)
                    continue

                fragments = _process_parcel(parcel_geom, ordered_refs)
                is_split  = len(fragments) > 1

                for frag_idx, (frag_geom, overlap_set) in enumerate(fragments):
                    flags_str = _flags_from_set(overlap_set)
                    attrs = base_attrs.copy()
                    attrs.update({
                        'parent_parcel_id': orig_id,
                        'is_split_result':  is_split and frag_idx > 0,
                        'overlap_flags':    flags_str,
                        'comment':          _comment_from_flags(flags_str),
                        'geometry':         frag_geom,
                    })
                    rows.append(attrs)

            # ── Build output GeoDataFrame ─────────────────────────────────────
            self.progress.emit("Building output layer…")
            result_gdf = gpd.GeoDataFrame(rows, crs=target_crs)
            result_gdf = result_gdf.reset_index(drop=True)

            count_after = len(result_gdf)
            split_count = int(result_gdf['is_split_result'].sum())

            # Per-layer counts (independent, not mutually exclusive)
            layer_counts = {
                k: int(result_gdf['overlap_flags'].str.contains(k, na=False).sum())
                for k in FLAG_KEYS
            }
            combo_counts = result_gdf['overlap_flags'].fillna('').value_counts().to_dict()

            # ── Save output ───────────────────────────────────────────────────
            self.progress.emit("Saving GeoPackage…")
            result_gdf.to_file(self.output_path, driver="GPKG")

            # ── Generate PDF report (same folder as output GPKG) ─────────────
            actual_report_path = None
            if self.report_path:
                self.progress.emit("Generating PDF report…")
                actual_report_path = _generate_pdf_report(
                    pdf_path     = self.report_path,
                    count_before = count_before,
                    count_after  = count_after,
                    split_count  = split_count,
                    layer_counts = layer_counts,
                    combo_counts = combo_counts,
                )

            self.finished.emit({
                'output_path':        self.output_path,
                'report_path':        actual_report_path,
                'count_before':       count_before,
                'count_after':        count_after,
                'split_count':        split_count,
                'layer_counts':       layer_counts,
                'combo_counts':       combo_counts,
                'result_gdf':         result_gdf,
            })

        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── Dock widget ───────────────────────────────────────────────────────────────

class ParcelCorrectionDock(QDockWidget):
    """
    Dockable UI for the Parcel Correction & Overlap Classification Tool.
    Tab 1 — Process: configure inputs and run.
    Tab 2 — Review: inspect results, filter, zoom to parcels.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Parcel Correction & Overlap Tool")
        self._thread     = None
        self._worker     = None
        self._result     = None   # dict from finished signal
        self._out_layer  = None   # QgsVectorLayer loaded after run
        self._review_rows = []    # list of dicts for the review tab

        root   = QWidget()
        vbox   = QVBoxLayout(root)
        vbox.setContentsMargins(6, 6, 6, 6)
        vbox.setSpacing(4)

        self._tabs = QTabWidget()
        vbox.addWidget(self._tabs)

        self._tabs.addTab(self._build_process_tab(), "Process")
        self._review_tab = self._build_review_tab()
        self._tabs.addTab(self._review_tab, "Review")
        self._tabs.setTabEnabled(1, False)

        self.setWidget(root)

    # ── Process tab ───────────────────────────────────────────────────────────

    def _build_process_tab(self):
        w      = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(5)
        layout.setContentsMargins(6, 6, 6, 6)

        H = 26  # uniform widget height

        def layer_row(label_text):
            """Primary parcels row — no buffer spinner."""
            row   = QHBoxLayout()
            lbl   = QLabel(label_text)
            lbl.setFixedWidth(110)
            combo = QgsMapLayerComboBox()
            combo.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
            combo.setFixedHeight(H)
            row.addWidget(lbl)
            row.addWidget(combo)
            layout.addLayout(row)
            return combo

        def ref_layer_row(label_text, default_buf=0.0):
            """Reference layer row — combo + buffer distance spinner."""
            row   = QHBoxLayout()
            lbl   = QLabel(label_text)
            lbl.setFixedWidth(110)
            combo = QgsMapLayerComboBox()
            combo.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
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

        self.cmb_parcels                        = layer_row("Parcels (primary):")
        self.cmb_roads,     self.spn_road_buf   = ref_layer_row("Roads:",          default_buf=0.0)
        self.cmb_rivers,    self.spn_river_buf  = ref_layer_row("Rivers:",         default_buf=0.0)
        self.cmb_waterbody, self.spn_wb_buf     = ref_layer_row("Waterbodies:",    default_buf=0.0)
        self.cmb_surveyed,  self.spn_surv_buf   = ref_layer_row("Surveyed land:",  default_buf=0.0)

        def file_row(label_text, save=True, filt="GeoPackage (*.gpkg)"):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(110)
            fw  = QgsFileWidget()
            fw.setStorageMode(
                QgsFileWidget.StorageMode.SaveFile if save
                else QgsFileWidget.StorageMode.GetFile
            )
            fw.setFilter(filt)
            row.addWidget(lbl)
            row.addWidget(fw)
            layout.addLayout(row)
            return fw

        self.wgt_output = file_row("Output (.gpkg):", save=True, filt="GeoPackage (*.gpkg)")
        # Report is saved automatically as a PDF next to the output GPKG

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

        return w

    # ── Review tab ────────────────────────────────────────────────────────────

    def _build_review_tab(self):
        w      = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 6, 6, 6)

        # Filter row
        filter_group = QGroupBox("Filter")
        fg_layout    = QHBoxLayout(filter_group)
        fg_layout.setSpacing(6)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search comment…")
        self._search_box.textChanged.connect(self._apply_filter)
        fg_layout.addWidget(QLabel("Search:"))
        fg_layout.addWidget(self._search_box)

        self._overlap_filter = QComboBox()
        self._overlap_filter.addItem("All overlaps")
        self._overlap_filter.currentIndexChanged.connect(self._apply_filter)
        fg_layout.addWidget(QLabel("Overlap:"))
        fg_layout.addWidget(self._overlap_filter)

        self._split_only = QCheckBox("Split parcels only")
        self._split_only.stateChanged.connect(self._apply_filter)
        fg_layout.addWidget(self._split_only)

        layout.addWidget(filter_group)

        # Splitter: list left, detail right
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["ID / Parcel", "Comment"])
        self._tree.setColumnWidth(0, 100)
        self._tree.itemClicked.connect(self._on_item_clicked)
        splitter.addWidget(self._tree)

        detail_w = QWidget()
        detail_l = QVBoxLayout(detail_w)
        detail_l.setContentsMargins(4, 4, 4, 4)
        self._detail = QTextBrowser()
        self._detail.setOpenExternalLinks(True)
        detail_l.addWidget(self._detail)

        btn_row = QHBoxLayout()
        self._zoom_btn = QPushButton("Zoom to in map")
        self._zoom_btn.setEnabled(False)
        self._zoom_btn.clicked.connect(self._zoom_to_selected)
        btn_row.addWidget(self._zoom_btn)
        btn_row.addStretch()
        detail_l.addLayout(btn_row)
        splitter.addWidget(detail_w)

        splitter.setSizes([220, 280])
        layout.addWidget(splitter, 1)

        return w

    # ── Status helper ─────────────────────────────────────────────────────────

    def _set_status(self, msg, color="green"):
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(msg)

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        parcels_layer = self.cmb_parcels.currentLayer()
        if not parcels_layer:
            self._set_status("Parcels layer is required!", "red"); return

        output_path = self.wgt_output.filePath()
        if not output_path:
            self._set_status("Output path is required!", "red"); return

        # Build ref_sources list and buffer distances (skip empty combos)
        ref_sources   = []
        buf_distances = {}
        pairs = [
            ('road',          'Roads',         self.cmb_roads,     self.spn_road_buf),
            ('river',         'Rivers',        self.cmb_rivers,    self.spn_river_buf),
            ('waterbody',     'Waterbodies',   self.cmb_waterbody, self.spn_wb_buf),
            ('surveyed_land', 'Surveyed Land', self.cmb_surveyed,  self.spn_surv_buf),
        ]
        for flag_key, label, combo, spn in pairs:
            layer = combo.currentLayer()
            if layer:
                ref_sources.append((flag_key, label, layer.source()))
                buf_distances[flag_key] = spn.value()

        if not ref_sources:
            self._set_status("At least one reference layer is required!", "red"); return

        # PDF saved automatically next to the output GeoPackage
        report_path = os.path.splitext(output_path)[0] + '.pdf'

        self._run_btn.setEnabled(False)
        self._set_status("Running…", "orange")

        self._thread = QThread()
        self._worker = _Worker(
            parcels_src   = parcels_layer.source(),
            ref_sources   = ref_sources,
            output_path   = output_path,
            report_path   = report_path,
            buf_distances = buf_distances,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda m: self._set_status(m, "orange"))
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(lambda: self._run_btn.setEnabled(True))
        self._thread.start()

    # ── Finished handler ──────────────────────────────────────────────────────

    def _on_finished(self, result: dict):
        self._result = result
        cb = result['count_before']
        ca = result['count_after']
        sc = result['split_count']
        lc = result['layer_counts']
        rp = result.get('report_path') or 'not generated'

        summary = (
            f"Done.  {cb} → {ca} parcels  ({sc} new from splits)\n"
            f"  Road: {lc.get('road',0)}  River: {lc.get('river',0)}  "
            f"Waterbody: {lc.get('waterbody',0)}  Surveyed Land: {lc.get('surveyed_land',0)}\n"
            f"  Output → {result['output_path']}\n"
            f"  Report → {rp}"
        )
        self._set_status(summary, "green")
        self._load_output(result['output_path'])
        self._populate_review(result)

    def _on_error(self, msg):
        self._set_status(f"Error:\n{msg}", "red")

    # ── Load output into QGIS ─────────────────────────────────────────────────

    def _load_output(self, path):
        if not path or not os.path.exists(path):
            return
        layer_name = os.path.splitext(os.path.basename(path))[0]
        uri   = f"{path}|layername={layer_name}"
        layer = QgsVectorLayer(uri, layer_name, "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            self._out_layer = layer

    # ── Populate review tab ───────────────────────────────────────────────────

    def _populate_review(self, result: dict):
        gdf = result['result_gdf']

        # Build overlap filter dropdown
        self._overlap_filter.blockSignals(True)
        self._overlap_filter.clear()
        self._overlap_filter.addItem("All overlaps")
        unique_flags = sorted(gdf['overlap_flags'].fillna('').unique())
        for f in unique_flags:
            label = "Plot is fine" if not f else _comment_from_flags(f).rstrip('.')
            self._overlap_filter.addItem(label, userData=f)
        self._overlap_filter.blockSignals(False)

        # Store review rows (convert to plain dicts for thread safety)
        geom_col = gdf.geometry.name
        self._review_rows = []
        for i in range(len(gdf)):
            geom = gdf[geom_col].iloc[i]
            self._review_rows.append({
                'row_idx':         i,
                'parent_id':       str(gdf['parent_parcel_id'].iloc[i]),
                'is_split':        bool(gdf['is_split_result'].iloc[i]),
                'overlap_flags':   str(gdf['overlap_flags'].iloc[i]) if gdf['overlap_flags'].iloc[i] else '',
                'comment':         str(gdf['comment'].iloc[i]),
                'bounds':          geom.bounds if (geom is not None and not geom.is_empty) else None,
                'extra_attrs':     {
                    col: str(gdf[col].iloc[i])
                    for col in gdf.columns
                    if col not in ('geometry', geom_col, 'comment', 'overlap_flags',
                                   'is_split_result', 'parent_parcel_id')
                },
            })

        self._tabs.setTabEnabled(1, True)
        self._apply_filter()
        self._tabs.setCurrentIndex(1)

    def _apply_filter(self):
        self._tree.clear()
        self._zoom_btn.setEnabled(False)
        self._detail.clear()

        search_text  = self._search_box.text().lower()
        split_only   = self._split_only.isChecked()
        filter_flags = self._overlap_filter.currentData()  # None = "All overlaps" item

        # Group by parent_parcel_id
        groups = {}
        for row in self._review_rows:
            if split_only and not row['is_split']:
                continue
            if search_text and search_text not in row['comment'].lower():
                continue
            if filter_flags is not None and row['overlap_flags'] != filter_flags:
                continue
            pid = row['parent_id']
            groups.setdefault(pid, []).append(row)

        for pid, rows in sorted(groups.items()):
            parent_item = QTreeWidgetItem(self._tree, [f"Parcel {pid}", ""])
            parent_item.setExpanded(len(rows) > 1)
            parent_item.setData(0, Qt.ItemDataRole.UserRole, None)

            if len(rows) == 1 and not rows[0]['is_split']:
                # Single unsplit parcel — put data on the parent item
                r = rows[0]
                parent_item.setText(1, r['comment'])
                parent_item.setData(0, Qt.ItemDataRole.UserRole, r)
            else:
                for idx, r in enumerate(rows):
                    label  = f"Fragment {idx + 1}" if r['is_split'] else f"Parcel {pid}"
                    child  = QTreeWidgetItem(parent_item, [label, r['comment']])
                    child.setData(0, Qt.ItemDataRole.UserRole, r)

        self._tree.resizeColumnToContents(0)

    def _on_item_clicked(self, item, _col):
        row = item.data(0, Qt.ItemDataRole.UserRole)
        if row is None:
            self._zoom_btn.setEnabled(False)
            self._detail.clear()
            return

        self._zoom_btn.setEnabled(row['bounds'] is not None)

        # Build detail HTML
        split_tag = ("&#x2714; is a split fragment" if row['is_split']
                     else "&#x2014; original parcel")
        flags_html = row['overlap_flags'] if row['overlap_flags'] else "<em>none</em>"
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
          <tr><td>Parent ID</td><td>{row['parent_id']}</td></tr>
          <tr><td>Split status</td><td>{split_tag}</td></tr>
          <tr><td>Overlap flags</td><td>{flags_html}</td></tr>
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
        iface = _get_iface()
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
