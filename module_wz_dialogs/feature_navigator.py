# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt, QRectF
from qgis.PyQt.QtGui import QFont, QColor, QFontMetricsF
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton,
    QLineEdit, QCheckBox, QFrame, QSizePolicy,
)

from qgis.gui import (
    QgsMapLayerComboBox, QgsExpressionBuilderDialog,
    QgsRubberBand, QgsMapCanvasItem,
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

        expr = self.expr_edit.text().strip()
        req = QgsFeatureRequest()
        if expr:
            req.setFilterExpression(expr)

        self._fids = [f.id() for f in layer.getFeatures(req)]

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
                bbox = geom.boundingBox()
                size = max(bbox.width(), bbox.height(), 1.0)
                bbox.grow(size * 0.25)
                self.canvas.setExtent(bbox)
                self.canvas.refresh()

        self._update_nav_buttons()
        self._update_vertex_distances()

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

    def _update_vertex_distances(self):
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
            except Exception:
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
            except Exception:
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
