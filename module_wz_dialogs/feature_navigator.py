# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtCore import Qt, QRectF
from qgis.PyQt.QtGui import QFont, QColor, QFontMetricsF
from qgis.PyQt.QtWidgets import (
    QDockWidget, QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton,
    QLineEdit, QCheckBox, QFrame, QSizePolicy, QFileDialog,
    QApplication, QSplitter, QScrollArea, QFormLayout, QComboBox,
)

from qgis.gui import (
    QgsMapLayerComboBox, QgsExpressionBuilderDialog,
    QgsRubberBand, QgsMapCanvasItem, QgsFieldComboBox,
)

from ..core import style as _style
from qgis.core import (
    QgsFeatureRequest,
    QgsMapLayerProxyModel,
    QgsVectorLayer,
    QgsSpatialIndex,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsCoordinateTransform,
    QgsProject,
    QgsDistanceArea,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
)

_LABEL_FONT = QFont('Arial', 7)
_LABEL_PAD  = 4.0
_LABEL_H    = 15.0


class _DistLabelItem(QgsMapCanvasItem):
    """Floating distance label pinned to a map coordinate.

    Automatically repositions when the map extent changes because QGIS calls
    updatePosition() on all QgsMapCanvasItem instances on every map render.
    """

    def __init__(self, canvas, map_point, text):
        super().__init__(canvas)
        self._map_point = map_point   # QgsPointXY in canvas/map CRS
        self._text = text
        fm = QFontMetricsF(_LABEL_FONT)
        self._w = fm.boundingRect(text).width() + _LABEL_PAD * 2
        self.updatePosition()

    def updatePosition(self):
        self.setPos(self.toCanvasCoordinates(self._map_point))

    def boundingRect(self):
        return QRectF(-self._w / 2, -_LABEL_H / 2, self._w, _LABEL_H)

    def paint(self, painter, option=None, widget=None):
        rect = QRectF(-self._w / 2, -_LABEL_H / 2, self._w, _LABEL_H)
        painter.setBrush(QColor(255, 255, 200, 220))
        painter.setPen(QColor(120, 120, 120, 200))
        painter.drawRoundedRect(rect, 2, 2)
        painter.setPen(Qt.black)
        painter.setFont(_LABEL_FONT)
        painter.drawText(rect, Qt.AlignCenter, self._text)



class FeatureNavigatorDock(QDockWidget):
    """
    Navigate through filtered features one by one using ◀ / ▶ buttons (or
    the Left / Right arrow keys while the dock is focused).

    Workflow
    --------
    1. Pick a vector layer.
    2. Type (or build) a filter expression.
    3. Click **Apply Filter** — matching features are loaded.
    4. Step through them with ◀ / ▶; the canvas zooms to each feature and
       an orange rubber band highlights it.

    Vertex → Polygon Distance
    -------------------------
    Enable the checkbox at the bottom, pick a polygon layer, and each vertex
    of the current feature gets a line drawn to the nearest polygon edge that
    is within 4 m.  A distance label floats at the midpoint of each line.
    """

    _MAX_VERTICES  = 200    # cap to prevent lag on dense geometries
    _MAX_DIST_M    = 4.0    # lines longer than this are not drawn

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.setWindowTitle('Feature Navigator')
        self.setMinimumWidth(310)
        self.setFocusPolicy(Qt.ClickFocus)

        self._fids: list = []
        self._index: int = -1

        # distance-to-polygon state
        self._dist_rubber_band = None     # single QgsRubberBand for all lines
        self._label_items: list = []      # list of _DistLabelItem
        self._poly_spatial_index = None
        self._poly_boundaries: dict = {}  # fid -> QgsGeometry (boundary lines)
        self._poly_layer_id = None

        self._build_ui()

    def closeEvent(self, event):
        self._clear_dist_overlays()
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QWidget()
        root.setFocusPolicy(Qt.ClickFocus)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(6)
        vbox.setContentsMargins(8, 8, 8, 8)

        # ── Layer ───────────────────────────────────────────────────────
        layer_row = QHBoxLayout()
        layer_row.addWidget(QLabel('Layer:'))
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        layer_row.addWidget(self.layer_combo)
        vbox.addLayout(layer_row)

        # ── Expression ──────────────────────────────────────────────────
        expr_row = QHBoxLayout()
        self.expr_edit = QLineEdit()
        self.expr_edit.setPlaceholderText('Filter expression  (empty = all features)')
        self.expr_edit.returnPressed.connect(self._apply_filter)
        self.expr_btn = QPushButton('…')
        self.expr_btn.setFixedWidth(30)
        self.expr_btn.setToolTip('Open expression builder')
        self.expr_btn.clicked.connect(self._open_expr_builder)
        expr_row.addWidget(self.expr_edit)
        expr_row.addWidget(self.expr_btn)
        vbox.addLayout(expr_row)

        self.apply_btn = QPushButton('Apply Filter')
        self.apply_btn.clicked.connect(self._apply_filter)
        vbox.addWidget(self.apply_btn)

        # ── Separator ───────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep)

        # ── Navigation ──────────────────────────────────────────────────
        nav_row = QHBoxLayout()

        self.prev_btn = QPushButton('◀  Prev')
        self.prev_btn.setEnabled(False)
        self.prev_btn.setMinimumWidth(80)
        self.prev_btn.clicked.connect(self._go_prev)

        self.counter_lbl = QLabel('—')
        self.counter_lbl.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setBold(True)
        self.counter_lbl.setFont(font)
        self.counter_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.next_btn = QPushButton('Next  ▶')
        self.next_btn.setEnabled(False)
        self.next_btn.setMinimumWidth(80)
        self.next_btn.clicked.connect(self._go_next)

        nav_row.addWidget(self.prev_btn)
        nav_row.addWidget(self.counter_lbl)
        nav_row.addWidget(self.next_btn)
        vbox.addLayout(nav_row)

        hint = QLabel('Tip: Left / Right arrow keys also navigate')
        hint.setStyleSheet('color: gray; font-size: 10px;')
        hint.setAlignment(Qt.AlignCenter)
        vbox.addWidget(hint)

        # ── Options ─────────────────────────────────────────────────────
        self.zoom_chk = QCheckBox('Zoom to feature')
        self.zoom_chk.setChecked(True)
        vbox.addWidget(self.zoom_chk)

        self.select_chk = QCheckBox('Select feature on layer')
        self.select_chk.setChecked(True)
        vbox.addWidget(self.select_chk)

        # ── Separator ───────────────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep2)

        # ── Vertex → Polygon distance ────────────────────────────────────
        self.dist_chk = QCheckBox('Vertex → polygon distances  (≤ 4 m)')
        self.dist_chk.setChecked(False)
        self.dist_chk.stateChanged.connect(self._on_dist_toggle)
        vbox.addWidget(self.dist_chk)

        poly_row = QHBoxLayout()
        poly_row.addWidget(QLabel('Polygon layer:'))
        self.poly_layer_combo = QgsMapLayerComboBox()
        self.poly_layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.poly_layer_combo.layerChanged.connect(self._on_poly_layer_changed)
        poly_row.addWidget(self.poly_layer_combo)
        vbox.addLayout(poly_row)

        # ── Advanced ─────────────────────────────────────────────────────
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.HLine)
        sep3.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep3)

        self.adv_btn = QPushButton('Advanced')
        self.adv_btn.setToolTip('Open full-screen view with PDF viewer and attribute editor')
        self.adv_btn.clicked.connect(self._open_advanced)
        vbox.addWidget(self.adv_btn)

        self.setWidget(root)

    # ------------------------------------------------------------------ #
    #  Keyboard navigation                                                 #
    # ------------------------------------------------------------------ #

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self._go_prev()
        elif event.key() == Qt.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    #  Slots                                                               #
    # ------------------------------------------------------------------ #

    def _on_layer_changed(self, _layer):
        self._fids = []
        self._index = -1
        self.counter_lbl.setText('—')
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self._clear_dist_overlays()

    def _on_dist_toggle(self, state):
        if not state:
            self._clear_dist_overlays()
        else:
            self._update_vertex_distances()

    def _on_poly_layer_changed(self, _layer):
        self._invalidate_poly_cache()
        if self.dist_chk.isChecked():
            self._update_vertex_distances()

    def _open_expr_builder(self):
        layer = self.layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer):
            return
        dlg = QgsExpressionBuilderDialog(layer, self.expr_edit.text(), self)
        if dlg.exec():
            self.expr_edit.setText(dlg.expressionText().strip())

    def _apply_filter(self):
        layer = self.layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer):
            self.counter_lbl.setText('Select a vector layer')
            return

        expr_text = self.expr_edit.text().strip()
        total = layer.featureCount()

        if not expr_text:
            self._fids = []
            for i, f in enumerate(layer.getFeatures()):
                self._fids.append(f.id())
                if i % 500 == 0:
                    self.counter_lbl.setText(f'Loading… {i} / {total}')
                    QApplication.processEvents()
        else:
            expr = QgsExpression(expr_text)
            if expr.hasParserError():
                self.counter_lbl.setText(f'Expr error: {expr.parserErrorString()}')
                return

            ctx = QgsExpressionContext()
            ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            expr.prepare(ctx)

            self._fids = []
            for i, feat in enumerate(layer.getFeatures()):
                ctx.setFeature(feat)
                result = expr.evaluate(ctx)
                if not expr.hasEvalError() and result:
                    self._fids.append(feat.id())
                if i % 500 == 0:
                    self.counter_lbl.setText(f'Filtering… {i} / {total}')
                    QApplication.processEvents()

        if not self._fids:
            self.counter_lbl.setText('0 features found')
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return

        self._index = 0
        self._show_current()

    # ------------------------------------------------------------------ #
    #  Navigation helpers                                                  #
    # ------------------------------------------------------------------ #

    def _go_prev(self):
        if self._fids and self._index > 0:
            self._index -= 1
            self._show_current()

    def _go_next(self):
        if self._fids and self._index < len(self._fids) - 1:
            self._index += 1
            self._show_current()

    def _update_nav_buttons(self):
        total = len(self._fids)
        self.prev_btn.setEnabled(self._index > 0)
        self.next_btn.setEnabled(self._index < total - 1)
        if total:
            self.counter_lbl.setText(f'Feature {self._index + 1} of {total}')
        else:
            self.counter_lbl.setText('No features')

    def _show_current(self):
        layer = self.layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer) or self._index < 0 or not self._fids:
            return

        fid = self._fids[self._index]
        feat = layer.getFeature(fid)

        if self.select_chk.isChecked():
            layer.selectByIds([fid])

        geom = feat.geometry()
        if geom and not geom.isEmpty():
            if self.zoom_chk.isChecked():
                layer_crs  = layer.crs()
                canvas_crs = self.canvas.mapSettings().destinationCrs()
                display_geom = QgsGeometry(geom)
                if layer_crs != canvas_crs:
                    xform = QgsCoordinateTransform(layer_crs, canvas_crs, QgsProject.instance())
                    display_geom.transform(xform)
                bbox = display_geom.boundingBox()
                size = max(bbox.width(), bbox.height(), 1.0)
                bbox.grow(size * 0.25)
                self.canvas.setExtent(bbox)
                self.canvas.refresh()

        self._update_nav_buttons()
        self._update_vertex_distances()

    # ------------------------------------------------------------------ #
    #  Advanced window                                                     #
    # ------------------------------------------------------------------ #

    def _open_advanced(self):
        if not isinstance(self.layer_combo.currentLayer(), QgsVectorLayer):
            return
        if not hasattr(self, '_adv_win') or not self._adv_win.isVisible():
            self._adv_win = AdvancedNavigatorWindow(
                canvas=self.canvas,
                layer=self.layer_combo.currentLayer(),
                fids=list(self._fids),
                index=self._index,
                expr=self.expr_edit.text(),
                zoom=self.zoom_chk.isChecked(),
                select=self.select_chk.isChecked(),
                parent=None,
            )
        self._adv_win.show()
        self._adv_win.raise_()
        self._adv_win.activateWindow()

    # ------------------------------------------------------------------ #
    #  Vertex → polygon distance                                           #
    # ------------------------------------------------------------------ #

    def _clear_dist_overlays(self):
        if self._dist_rubber_band is not None:
            self.canvas.scene().removeItem(self._dist_rubber_band)
            self._dist_rubber_band = None
        for item in self._label_items:
            self.canvas.scene().removeItem(item)
        self._label_items.clear()

    def _invalidate_poly_cache(self):
        self._poly_spatial_index = None
        self._poly_boundaries.clear()
        self._poly_layer_id = None

    @staticmethod
    def _extract_boundary(geom):
        """Return boundary edges of geom as a MultiLineString QgsGeometry.

        QgsGeometry.boundary() is absent from QGIS 3.x Python bindings,
        so rings are extracted manually.
        """
        lines = []
        wkb   = geom.wkbType()
        gtype = QgsWkbTypes.geometryType(wkb)

        if gtype == QgsWkbTypes.PolygonGeometry:
            polys = geom.asMultiPolygon() if QgsWkbTypes.isMultiType(wkb) else [geom.asPolygon()]
            for poly in polys:
                for ring in poly:
                    if len(ring) >= 2:
                        lines.append(ring)

        elif gtype == QgsWkbTypes.LineGeometry:
            if QgsWkbTypes.isMultiType(wkb):
                lines.extend(geom.asMultiPolyline())
            else:
                lines.append(geom.asPolyline())

        return QgsGeometry.fromMultiPolylineXY(lines) if lines else None

    def _build_poly_cache(self, poly_layer):
        """Index every feature's boundary geometry for fast nearest-edge queries."""
        self._poly_spatial_index = QgsSpatialIndex()
        self._poly_boundaries = {}
        for feat in poly_layer.getFeatures():
            geom = feat.geometry()
            if geom and not geom.isEmpty():
                self._poly_spatial_index.insertFeature(feat)
                boundary = self._extract_boundary(geom)
                if boundary and not boundary.isEmpty():
                    self._poly_boundaries[feat.id()] = boundary
        self._poly_layer_id = poly_layer.id()

    def _update_vertex_distances(self):  # noqa: C901
        """
        For every unique vertex of the current feature, find the nearest point
        on any polygon boundary edge.  Lines ≤ 4 m get drawn on the canvas
        with a floating distance label at their midpoint.

        Performance strategy
        --------------------
        - One QgsRubberBand (MultiLineString) → single scene item.
        - QgsSpatialIndex.nearestNeighbor() → O(log n) candidate lookup.
        - Polygon boundary cache → built once, reused across navigation steps.
        - QgsDistanceArea → ellipsoidal metres for the 4 m threshold and label.
        """
        self._clear_dist_overlays()

        if not self.dist_chk.isChecked():
            return

        poly_layer = self.poly_layer_combo.currentLayer()
        if not isinstance(poly_layer, QgsVectorLayer):
            return

        layer = self.layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer) or self._index < 0 or not self._fids:
            return

        fid  = self._fids[self._index]
        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if not geom or geom.isEmpty():
            return

        if poly_layer.id() != self._poly_layer_id:
            self._build_poly_cache(poly_layer)

        if not self._poly_spatial_index or not self._poly_boundaries:
            return

        # CRS setup
        feat_crs   = layer.crs()
        poly_crs   = poly_layer.crs()
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        project    = QgsProject.instance()

        to_poly        = QgsCoordinateTransform(feat_crs, poly_crs,   project)
        poly_to_canvas = QgsCoordinateTransform(poly_crs, canvas_crs, project)
        feat_to_canvas = QgsCoordinateTransform(feat_crs, canvas_crs, project)
        same_crs       = feat_crs == poly_crs

        da = QgsDistanceArea()
        da.setSourceCrs(poly_crs, project.transformContext())
        da.setEllipsoid(project.ellipsoid())

        # Unique vertices — skip the duplicate ring-closing point
        seen = set()
        unique_verts = []
        for v in geom.vertices():
            key = (round(v.x(), 9), round(v.y(), 9))
            if key not in seen:
                seen.add(key)
                unique_verts.append(v)
        unique_verts = unique_verts[:self._MAX_VERTICES]

        line_segments = []   # [[QgsPointXY, QgsPointXY], ...]  in canvas CRS
        label_data    = []   # (mid_canvas QgsPointXY, text str)

        for vertex in unique_verts:
            vx, vy = vertex.x(), vertex.y()

            try:
                v_canvas_pt = feat_to_canvas.transform(QgsPointXY(vx, vy))
                v_poly_pt   = (QgsPointXY(vx, vy) if same_crs
                               else to_poly.transform(QgsPointXY(vx, vy)))
            except Exception:  # nosec B112
                continue

            candidates = self._poly_spatial_index.nearestNeighbor(v_poly_pt, 3)
            if not candidates:
                continue

            v_geom    = QgsGeometry.fromPointXY(v_poly_pt)
            min_dist  = float('inf')
            nearest_poly_pt = None

            for cid in candidates:
                boundary = self._poly_boundaries.get(cid)
                if boundary is None:
                    continue
                d = v_geom.distance(boundary)
                if d < min_dist:
                    min_dist = d
                    np_geom  = boundary.nearestPoint(v_geom)
                    if np_geom and not np_geom.isEmpty():
                        nearest_poly_pt = np_geom.asPoint()

            if nearest_poly_pt is None:
                continue

            # Skip zero distances (vertex sits on the polygon boundary)
            # and anything beyond the 4 m threshold
            dist_m = da.measureLine(v_poly_pt, nearest_poly_pt)
            if dist_m <= 0.0 or dist_m > self._MAX_DIST_M:
                continue

            try:
                np_canvas_pt = poly_to_canvas.transform(nearest_poly_pt)
            except Exception:  # nosec B112
                continue

            line_segments.append([v_canvas_pt, np_canvas_pt])

            mid_canvas = QgsPointXY(
                (v_canvas_pt.x() + np_canvas_pt.x()) / 2,
                (v_canvas_pt.y() + np_canvas_pt.y()) / 2,
            )
            label_data.append((mid_canvas, f'{dist_m:.2f}m'))

        if not line_segments:
            return

        # Single rubber band for all distance lines
        ml_geom = QgsGeometry.fromMultiPolylineXY(line_segments)
        rb = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        rb.setToGeometry(ml_geom, None)   # geometry already in canvas CRS
        rb.setColor(_style.RB_RED)
        rb.setWidth(_style.RB_WIDTH)
        rb.setLineStyle(_style.RB_LINE_STYLE)
        self._dist_rubber_band = rb

        # One floating label per line at the midpoint
        for mid_pt, text in label_data:
            self._label_items.append(_DistLabelItem(self.canvas, mid_pt, text))

        self.canvas.refresh()


# ======================================================================== #
#  Embedded PDF viewer (PyMuPDF / fitz)                                     #
# ======================================================================== #

class _FitzPdfViewer(QWidget):
    """Renders PDF pages as images using PyMuPDF. API mimics QWebEngineView."""

    _ZOOM = 2.0   # render scale — increase for sharper text

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc      = None
        self._page_idx = 0
        self._build_ui()

    def _build_ui(self):
        from qgis.PyQt.QtWidgets import QScrollArea as _SA
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(2)

        nav = QHBoxLayout()
        self._prev_page = QPushButton('◀')
        self._prev_page.setFixedWidth(32)
        self._prev_page.clicked.connect(self._go_prev)
        self._page_lbl = QLabel('No PDF loaded')
        self._page_lbl.setAlignment(Qt.AlignCenter)
        self._next_page = QPushButton('▶')
        self._next_page.setFixedWidth(32)
        self._next_page.clicked.connect(self._go_next)
        nav.addWidget(self._prev_page)
        nav.addWidget(self._page_lbl)
        nav.addWidget(self._next_page)
        vbox.addLayout(nav)

        self._scroll = _SA()
        self._scroll.setWidgetResizable(True)
        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignCenter)
        self._scroll.setWidget(self._img_lbl)
        vbox.addWidget(self._scroll, 1)

    # QWebEngineView-compatible entry point
    def setUrl(self, url):
        self._load(url.toLocalFile())

    def _load(self, path):
        try:
            import fitz
            self._doc      = fitz.open(path)
            self._page_idx = 0
            self._render()
        except ImportError:
            self._page_lbl.setText('PyMuPDF not installed')
            self._img_lbl.setText(
                'Install PyMuPDF (pip install pymupdf) to view PDFs inline.')
        except Exception as e:
            self._page_lbl.setText('Error')
            self._img_lbl.setText(str(e))

    def _render(self):
        if self._doc is None:
            return
        import fitz
        from qgis.PyQt.QtGui import QImage, QPixmap
        total = self._doc.page_count
        self._page_lbl.setText(f'Page {self._page_idx + 1} / {total}')
        self._prev_page.setEnabled(self._page_idx > 0)
        self._next_page.setEnabled(self._page_idx < total - 1)

        page = self._doc[self._page_idx]
        mat  = fitz.Matrix(self._ZOOM, self._ZOOM)
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        img  = QImage(bytes(pix.samples), pix.width, pix.height,
                      pix.stride, QImage.Format_RGB888)
        self._img_lbl.setPixmap(QPixmap.fromImage(img))

    def _go_prev(self):
        if self._page_idx > 0:
            self._page_idx -= 1
            self._render()

    def _go_next(self):
        if self._doc and self._page_idx < self._doc.page_count - 1:
            self._page_idx += 1
            self._render()


# ======================================================================== #
#  Full-screen advanced view                                                #
# ======================================================================== #

class AdvancedNavigatorWindow(QDialog):
    """
    Full-screen three-panel view:
      Left   — navigation controls + PDF file list for current feature folder
      Middle — embedded PDF viewer
      Right  — attribute editor backed by a user-chosen CSV / Excel file
    """

    def __init__(self, canvas, layer, fids, index,
                 expr='', zoom=True, select=True, parent=None):
        super().__init__(parent)
        self.canvas  = canvas
        self._layer  = layer
        self._fids   = list(fids)
        self._index  = index

        self._data_file     = ''
        self._data_id_col   = ''
        self._data_headers  = []
        self._data_all_rows = []   # list of dicts — full file in memory
        self._data_by_id    = {}   # id_value -> list[int] (row indices)
        self._attr_edits    = {}   # col -> QLineEdit
        self._attr_match_idx = 0  # which duplicate match is currently shown

        from qgis.PyQt.QtCore import QTimer
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(3000)
        self._save_timer.timeout.connect(self._save_attributes)

        self.setWindowTitle('Feature Navigator — Advanced')
        self.setWindowFlags(Qt.Window)
        self._build_ui()

        # ── carry over dock settings ──────────────────────────────────────
        self._expr_edit.setText(expr)
        self._zoom_chk.setChecked(zoom)
        self._select_chk.setChecked(select)

        self._update_nav_buttons()
        if self._fids and self._index >= 0:
            self._show_current()
        self.showMaximized()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root_vbox = QVBoxLayout(self)
        root_vbox.setSpacing(4)
        root_vbox.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_middle_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([280, 700, 320])
        root_vbox.addWidget(splitter, 1)

    # ── Left panel ───────────────────────────────────────────────────────

    def _build_left_panel(self):
        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setSpacing(6)
        vbox.setContentsMargins(6, 6, 6, 6)

        # ── Layer ────────────────────────────────────────────────────────
        lr = QHBoxLayout()
        lr.addWidget(QLabel('Layer:'))
        self._layer_combo = QgsMapLayerComboBox()
        self._layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self._layer_combo.setLayer(self._layer)
        self._layer_combo.layerChanged.connect(self._on_layer_changed)
        lr.addWidget(self._layer_combo)
        vbox.addLayout(lr)

        # ── Expression ───────────────────────────────────────────────────
        er = QHBoxLayout()
        self._expr_edit = QLineEdit()
        self._expr_edit.setPlaceholderText('Filter expression  (empty = all features)')
        self._expr_edit.returnPressed.connect(self._apply_filter)
        er.addWidget(self._expr_edit)
        eb = QPushButton('…'); eb.setFixedWidth(30)
        eb.setToolTip('Open expression builder')
        eb.clicked.connect(self._open_expr_builder)
        er.addWidget(eb)
        vbox.addLayout(er)

        self._apply_btn = QPushButton('Apply Filter')
        self._apply_btn.clicked.connect(self._apply_filter)
        vbox.addWidget(self._apply_btn)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.HLine); sep1.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep1)

        # ── Navigation ───────────────────────────────────────────────────
        nav_row = QHBoxLayout()
        self._prev_btn = QPushButton('◀  Prev')
        self._prev_btn.setEnabled(False)
        self._prev_btn.setMinimumWidth(80)
        self._prev_btn.clicked.connect(self._go_prev)

        self._counter_lbl = QLabel('—')
        self._counter_lbl.setAlignment(Qt.AlignCenter)
        f = QFont(); f.setBold(True)
        self._counter_lbl.setFont(f)
        self._counter_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self._next_btn = QPushButton('Next  ▶')
        self._next_btn.setEnabled(False)
        self._next_btn.setMinimumWidth(80)
        self._next_btn.clicked.connect(self._go_next)

        nav_row.addWidget(self._prev_btn)
        nav_row.addWidget(self._counter_lbl)
        nav_row.addWidget(self._next_btn)
        vbox.addLayout(nav_row)

        hint = QLabel('Tip: Left / Right arrow keys also navigate')
        hint.setStyleSheet('color: gray; font-size: 10px;')
        hint.setAlignment(Qt.AlignCenter)
        vbox.addWidget(hint)

        self._zoom_chk = QCheckBox('Zoom to feature')
        self._zoom_chk.setChecked(True)
        vbox.addWidget(self._zoom_chk)

        self._select_chk = QCheckBox('Select feature on layer')
        self._select_chk.setChecked(True)
        vbox.addWidget(self._select_chk)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine); sep2.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep2)

        # ── PDF list ─────────────────────────────────────────────────────
        vbox.addWidget(QLabel('PDF files in feature folder:'))
        self._pdf_scroll = QScrollArea()
        self._pdf_scroll.setWidgetResizable(True)
        vbox.addWidget(self._pdf_scroll, 1)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.HLine); sep3.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep3)

        # ── Folder settings ───────────────────────────────────────────────
        vbox.addWidget(QLabel('Feature folders base:'))
        fr = QHBoxLayout()
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText('Base folder…')
        self._folder_edit.textChanged.connect(lambda _: self._refresh_pdf_list())
        fr.addWidget(self._folder_edit)
        b = QPushButton('…'); b.setFixedWidth(28); b.clicked.connect(self._browse_folder)
        fr.addWidget(b)
        vbox.addLayout(fr)

        ir = QHBoxLayout()
        ir.addWidget(QLabel('ID field:'))
        self._id_combo = QgsFieldComboBox()
        self._id_combo.setLayer(self._layer)
        self._id_combo.fieldChanged.connect(lambda _: self._refresh_pdf_list())
        ir.addWidget(self._id_combo)
        vbox.addLayout(ir)

        pr = QHBoxLayout()
        pr.addWidget(QLabel('Prefix:'))
        self._prefix_edit = QLineEdit()
        self._prefix_edit.setPlaceholderText('optional')
        self._prefix_edit.textChanged.connect(lambda _: self._refresh_pdf_list())
        pr.addWidget(self._prefix_edit)
        vbox.addLayout(pr)

        return w

    # ── Middle panel ─────────────────────────────────────────────────────

    def _build_middle_panel(self):
        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setContentsMargins(4, 4, 4, 0)
        vbox.setSpacing(4)

        self._reason_lbl = QLabel('')
        self._reason_lbl.setWordWrap(True)
        self._reason_lbl.setStyleSheet(
            'font-weight: bold; font-size: 12px; '
            'padding: 4px 6px; '
            'background: #f0f4ff; '
            'border-radius: 4px;'
        )
        self._reason_lbl.setVisible(False)
        vbox.addWidget(self._reason_lbl)

        vbox.addWidget(self._make_pdf_viewer(), 1)
        return w

    def _make_pdf_viewer(self):
        try:
            from qgis.PyQt.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings
            self._web_view = QWebEngineView()
            s = self._web_view.settings()
            s.setAttribute(QWebEngineSettings.PluginsEnabled, True)
            try:
                s.setAttribute(QWebEngineSettings.PdfViewerEnabled, True)
            except AttributeError:
                pass
        except ImportError:
            self._web_view = _FitzPdfViewer()
        return self._web_view

    # ── Right panel ──────────────────────────────────────────────────────

    def _build_right_panel(self):
        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setSpacing(5)
        vbox.setContentsMargins(4, 4, 4, 4)

        vbox.addWidget(QLabel('Data file (CSV / Excel):'))
        dfr = QHBoxLayout()
        self._data_file_edit = QLineEdit()
        self._data_file_edit.setPlaceholderText('Pick CSV or Excel file…')
        dfr.addWidget(self._data_file_edit)
        db = QPushButton('…'); db.setFixedWidth(28); db.clicked.connect(self._browse_data_file)
        dfr.addWidget(db)
        vbox.addLayout(dfr)

        idr = QHBoxLayout()
        idr.addWidget(QLabel('ID column:'))
        self._data_id_combo = QComboBox()
        idr.addWidget(self._data_id_combo)
        load_btn = QPushButton('Load'); load_btn.clicked.connect(self._load_data_file)
        idr.addWidget(load_btn)
        vbox.addLayout(idr)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setFrameShadow(QFrame.Sunken)
        vbox.addWidget(sep)

        self._attr_scroll = QScrollArea()
        self._attr_scroll.setWidgetResizable(True)
        vbox.addWidget(self._attr_scroll, 1)

        self._attr_status = QLabel('')
        self._attr_status.setStyleSheet('color: gray; font-size: 10px;')
        self._attr_status.setWordWrap(True)
        vbox.addWidget(self._attr_status)

        return w

    # ------------------------------------------------------------------ #
    #  Navigation                                                          #
    # ------------------------------------------------------------------ #

    def _refresh_reason_label(self):
        expr_text = self._expr_edit.text().strip()
        if not expr_text or not isinstance(self._layer, QgsVectorLayer) \
                or self._index < 0 or not self._fids:
            self._reason_lbl.setVisible(False)
            return

        expr = QgsExpression(expr_text)
        cols = [c for c in expr.referencedColumns()
                if c != QgsFeatureRequest.ALL_ATTRIBUTES]
        if not cols:
            self._reason_lbl.setVisible(False)
            return

        feat  = self._layer.getFeature(self._fids[self._index])
        parts = []
        for col in cols:
            val = feat.attribute(col)
            if val is not None and str(val).strip():
                parts.append(f'{col}:  {val}')

        if parts:
            self._reason_lbl.setText('  |  '.join(parts))
            self._reason_lbl.setVisible(True)
        else:
            self._reason_lbl.setVisible(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self._go_prev()
        elif event.key() == Qt.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)

    def _on_layer_changed(self, layer):
        self._layer = layer
        self._fids  = []
        self._index = -1
        self._counter_lbl.setText('—')
        self._prev_btn.setEnabled(False)
        self._next_btn.setEnabled(False)
        self._id_combo.setLayer(layer)
        self._refresh_pdf_list()

    def _open_expr_builder(self):
        if not isinstance(self._layer, QgsVectorLayer):
            return
        dlg = QgsExpressionBuilderDialog(self._layer, self._expr_edit.text(), self)
        if dlg.exec():
            self._expr_edit.setText(dlg.expressionText().strip())

    def _apply_filter(self):
        layer = self._layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer):
            self._counter_lbl.setText('Select a layer')
            return
        self._layer = layer
        expr_text = self._expr_edit.text().strip()
        total = layer.featureCount()

        if not expr_text:
            self._fids = []
            for i, f in enumerate(layer.getFeatures()):
                self._fids.append(f.id())
                if i % 500 == 0:
                    self._counter_lbl.setText(f'Loading… {i} / {total}')
                    QApplication.processEvents()
        else:
            expr = QgsExpression(expr_text)
            if expr.hasParserError():
                self._counter_lbl.setText(f'Expr error: {expr.parserErrorString()}')
                return
            ctx = QgsExpressionContext()
            ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            expr.prepare(ctx)
            self._fids = []
            for i, feat in enumerate(layer.getFeatures()):
                ctx.setFeature(feat)
                if not expr.hasEvalError() and expr.evaluate(ctx):
                    self._fids.append(feat.id())
                if i % 500 == 0:
                    self._counter_lbl.setText(f'Filtering… {i} / {total}')
                    QApplication.processEvents()

        if not self._fids:
            self._counter_lbl.setText('0 features found')
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)
            return
        self._index = 0
        self._show_current()

    def _go_prev(self):
        if self._fids and self._index > 0:
            self._index -= 1
            self._show_current()

    def _go_next(self):
        if self._fids and self._index < len(self._fids) - 1:
            self._index += 1
            self._show_current()

    def _update_nav_buttons(self):
        total = len(self._fids)
        self._prev_btn.setEnabled(self._index > 0)
        self._next_btn.setEnabled(self._index < total - 1)
        self._counter_lbl.setText(
            f'Feature {self._index + 1} of {total}' if total else '—')

    def _show_current(self):
        self._attr_match_idx = 0
        self._update_nav_buttons()
        self._refresh_reason_label()
        self._refresh_pdf_list()
        self._refresh_attributes()
        if not isinstance(self._layer, QgsVectorLayer) or self._index < 0 or not self._fids:
            return
        fid  = self._fids[self._index]
        feat = self._layer.getFeature(fid)
        if self._select_chk.isChecked():
            self._layer.selectByIds([fid])
        geom = feat.geometry()
        if geom and not geom.isEmpty() and self._zoom_chk.isChecked():
            layer_crs  = self._layer.crs()
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            dg = QgsGeometry(geom)
            if layer_crs != canvas_crs:
                dg.transform(QgsCoordinateTransform(
                    layer_crs, canvas_crs, QgsProject.instance()))
            bbox = dg.boundingBox()
            bbox.grow(max(bbox.width(), bbox.height(), 1.0) * 0.25)
            self.canvas.setExtent(bbox)
            self.canvas.refresh()

    # ------------------------------------------------------------------ #
    #  PDF list                                                            #
    # ------------------------------------------------------------------ #

    def _current_feature_folder(self):
        if not isinstance(self._layer, QgsVectorLayer) or self._index < 0 or not self._fids:
            return None
        field = self._id_combo.currentField()
        if not field:
            return None
        val = self._layer.getFeature(self._fids[self._index]).attribute(field)
        if val is None:
            return None
        base = self._folder_edit.text().strip()
        if not base:
            return None
        return os.path.join(base, f'{self._prefix_edit.text()}{val}')

    def _refresh_pdf_list(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(3)
        layout.setContentsMargins(2, 2, 2, 2)

        folder = self._current_feature_folder()
        if folder and os.path.isdir(folder):
            pdfs = sorted(f for f in os.listdir(folder) if f.lower().endswith('.pdf'))
            if pdfs:
                for name in pdfs:
                    path = os.path.join(folder, name)
                    btn = QPushButton(name)
                    btn.setStyleSheet('text-align: left; padding: 3px 6px;')
                    btn.setToolTip(path)
                    btn.clicked.connect(lambda _c, p=path: self._open_pdf(p))
                    layout.addWidget(btn)
            else:
                layout.addWidget(self._gray('No PDF files in folder.'))
        else:
            layout.addWidget(self._gray(
                'Folder not found.' if folder else 'Configure folder settings.'))

        layout.addStretch()
        self._pdf_scroll.setWidget(container)

    @staticmethod
    def _gray(text):
        lbl = QLabel(text)
        lbl.setStyleSheet('color: gray; font-size: 10px;')
        return lbl

    def _open_pdf(self, path):
        from qgis.PyQt.QtCore import QUrl
        self._web_view.setUrl(QUrl.fromLocalFile(path))

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, 'Select base folder', self._folder_edit.text())
        if folder:
            self._folder_edit.setText(folder)
            self._refresh_pdf_list()

    # ------------------------------------------------------------------ #
    #  Attribute editor                                                    #
    # ------------------------------------------------------------------ #

    def _go_prev_match(self):
        if self._attr_match_idx > 0:
            self._attr_match_idx -= 1
            self._refresh_attributes()

    def _go_next_match(self):
        feat_id = self._current_feat_id()
        if feat_id and self._attr_match_idx < len(self._data_by_id.get(feat_id, [])) - 1:
            self._attr_match_idx += 1
            self._refresh_attributes()

    def _current_feat_id(self):
        if not isinstance(self._layer, QgsVectorLayer) or self._index < 0 or not self._fids:
            return None
        field = self._id_combo.currentField()
        if not field:
            return None
        return str(self._layer.getFeature(self._fids[self._index]).attribute(field) or '')

    def _browse_data_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select data file', '', 'CSV / Excel (*.csv *.xlsx *.xls)')
        if not path:
            return
        self._data_file_edit.setText(path)
        self._data_id_combo.clear()
        self._data_id_combo.addItems(self._read_file_headers(path))

    def _read_file_headers(self, path):
        try:
            if path.lower().endswith('.csv'):
                import csv
                with open(path, newline='', encoding='utf-8-sig') as f:
                    return next(csv.reader(f), [])
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            row = next(wb.active.iter_rows(max_row=1, values_only=True), [])
            wb.close()
            return [str(c) for c in row if c is not None]
        except Exception as e:
            self._attr_status.setText(f'Header read error: {e}')
            return []

    def _load_data_file(self):
        path   = self._data_file_edit.text().strip()
        id_col = self._data_id_combo.currentText()
        if not path or not os.path.isfile(path):
            self._attr_status.setText('Select a valid data file.')
            return
        if not id_col:
            self._attr_status.setText('Select an ID column.')
            return
        try:
            self._data_headers, self._data_all_rows, self._data_by_id = \
                self._parse_data_file(path, id_col)
            self._data_file   = path
            self._data_id_col = id_col
            self._attr_status.setText(f'{len(self._data_all_rows)} rows loaded.')
            self._refresh_attributes()
        except Exception as e:
            self._attr_status.setText(f'Load error: {e}')

    def _parse_data_file(self, path, id_col):
        headers, rows, by_id = [], [], {}
        if path.lower().endswith('.csv'):
            import csv
            with open(path, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                headers = list(reader.fieldnames or [])
                for i, row in enumerate(reader):
                    rows.append(dict(row))
                    by_id.setdefault(str(row.get(id_col, '')), []).append(i)
        else:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            all_r = list(wb.active.iter_rows(values_only=True))
            wb.close()
            if not all_r:
                return headers, rows, by_id
            headers = [str(c) for c in all_r[0]]
            id_idx  = headers.index(id_col) if id_col in headers else None
            for i, r in enumerate(all_r[1:]):
                row = {headers[j]: (r[j] if j < len(r) else '') for j in range(len(headers))}
                rows.append(row)
                if id_idx is not None:
                    by_id.setdefault(str(r[id_idx] if id_idx < len(r) else ''), []).append(i)
        return headers, rows, by_id

    def _refresh_attributes(self):
        self._save_timer.stop()
        self._attr_edits = {}

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setSpacing(4)
        outer.setContentsMargins(2, 2, 2, 2)

        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setSpacing(4)
        outer.addWidget(form_w)

        def _done():
            outer.addStretch()
            self._attr_scroll.setWidget(container)

        if not self._data_all_rows or not isinstance(self._layer, QgsVectorLayer) \
                or self._index < 0 or not self._fids:
            return _done()

        field = self._id_combo.currentField()
        if not field:
            return _done()

        feat_id = str(self._layer.getFeature(
            self._fids[self._index]).attribute(field) or '')
        matches = self._data_by_id.get(feat_id, [])

        if not matches:
            form.addRow(self._gray(f'No record for parcel ID: {feat_id}'))
            return _done()

        self._attr_match_idx = max(0, min(self._attr_match_idx, len(matches) - 1))
        total = len(matches)

        row = self._data_all_rows[matches[self._attr_match_idx]]
        for col in self._data_headers:
            edit = QLineEdit(str(row.get(col, '') or ''))
            edit.textChanged.connect(self._on_attr_edited)
            form.addRow(col + ':', edit)
            self._attr_edits[col] = edit

        # ── fresh nav buttons created locally — no stale C++ references ──
        if total > 1:
            prev_btn = QPushButton('◀')
            prev_btn.setFixedWidth(32)
            prev_btn.setEnabled(self._attr_match_idx > 0)
            prev_btn.clicked.connect(self._go_prev_match)
            match_lbl = QLabel(f'Record {self._attr_match_idx + 1} of {total}')
            match_lbl.setAlignment(Qt.AlignCenter)
            match_lbl.setStyleSheet('font-size: 10px; color: gray;')
            next_btn = QPushButton('▶')
            next_btn.setFixedWidth(32)
            next_btn.setEnabled(self._attr_match_idx < total - 1)
            next_btn.clicked.connect(self._go_next_match)
            nav_row = QHBoxLayout()
            nav_row.addWidget(prev_btn)
            nav_row.addWidget(match_lbl)
            nav_row.addWidget(next_btn)
            outer.addLayout(nav_row)

        _done()

    def _on_attr_edited(self):
        self._attr_status.setText('Unsaved changes…')
        self._save_timer.start(3000)

    def _save_attributes(self):
        if not self._data_file or not self._data_all_rows or self._index < 0 or not self._fids:
            return
        field = self._id_combo.currentField()
        if not field:
            return
        feat_id = str(self._layer.getFeature(
            self._fids[self._index]).attribute(field) or '')
        matches = self._data_by_id.get(feat_id, [])
        if not matches:
            self._attr_status.setText(f'No record for parcel ID: {feat_id}')
            return
        row_idx = matches[min(self._attr_match_idx, len(matches) - 1)]

        for col, edit in self._attr_edits.items():
            self._data_all_rows[row_idx][col] = edit.text()

        try:
            if self._data_file.lower().endswith('.csv'):
                import csv
                with open(self._data_file, 'w', newline='', encoding='utf-8-sig') as f:
                    w = csv.DictWriter(f, fieldnames=self._data_headers)
                    w.writeheader()
                    w.writerows(self._data_all_rows)
            else:
                import openpyxl
                wb  = openpyxl.load_workbook(self._data_file)
                ws  = wb.active
                id_col_idx = self._data_headers.index(self._data_id_col)
                for xrow in ws.iter_rows(min_row=2):
                    if str(xrow[id_col_idx].value or '') == feat_id:
                        for j, col in enumerate(self._data_headers):
                            xrow[j].value = self._data_all_rows[row_idx].get(col, '')
                        break
                wb.save(self._data_file)
            self._attr_status.setText('Saved.')
        except Exception as e:
            self._attr_status.setText(f'Save error: {e}')
