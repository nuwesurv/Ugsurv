import math
import os

from PyQt5.QtCore import Qt, QRectF, QSize, QTimer, QVariant, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QColorDialog, QComboBox, QDockWidget, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QCheckBox, QLineEdit, QPushButton,
    QScrollArea, QWidget, QVBoxLayout,
)
from qgis.core import (
    QgsApplication, QgsCircularString, QgsGeometry, QgsPoint, QgsPointXY, QgsWkbTypes,
)

_PLUGIN_ICONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "map_icons", "point_icons",
)
from .layer_utils import (
    circle_attrs,
    apply_circle_color_renderer,
    apply_hatch_renderer,
    apply_polyline_color_renderer,
    apply_point_color_renderer,
)


class PropertiesDock(QDockWidget):
    """Right-side dock showing editable properties of the selected feature."""

    # Emitted after any geometry edit so the vertex selector can refresh its rubber band.
    geometry_changed = pyqtSignal(object, int)

    def __init__(self, parent=None):
        super().__init__("Properties", parent)
        self.setObjectName("UgsurvPropertiesDock")
        self.setMinimumWidth(210)

        self._layer    = None
        self._fid      = None
        self._updating = False

        outer = QWidget()
        outer_vbox = QVBoxLayout(outer)
        outer_vbox.setContentsMargins(10, 8, 10, 8)
        outer_vbox.setSpacing(4)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        outer_vbox.addWidget(line)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        self._content = QWidget()
        _outer_vbox = QVBoxLayout(self._content)
        _outer_vbox.setContentsMargins(0, 0, 0, 0)
        _outer_vbox.setSpacing(0)

        _form_holder = QWidget()
        self._form = QFormLayout(_form_holder)
        self._form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._form.setSpacing(5)
        self._form.setContentsMargins(0, 4, 0, 4)

        _outer_vbox.addWidget(_form_holder)
        _outer_vbox.addStretch(1)

        scroll.setWidget(self._content)
        outer_vbox.addWidget(scroll)

        self.setWidget(outer)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_feature(self, layer, fid):
        self._layer = layer
        self._fid   = fid
        self._refresh()

    def clear_selection(self):
        self._layer = None
        self._fid   = None
        self._clear_form()

    def refresh_if_current(self, layer, fid):
        if self._layer is layer and self._fid == fid:
            self._refresh()

    # ------------------------------------------------------------------
    # Form helpers
    # ------------------------------------------------------------------

    def _clear_form(self):
        while self._form.rowCount() > 0:
            self._form.removeRow(0)

    def _deferred_refresh(self):
        """Schedule refresh after the current signal dispatch finishes.

        Calling _refresh() directly from inside a widget's signal would delete
        that widget while Qt is still dispatching its signal — unsafe. A zero-
        delay timer defers the call until after the event loop returns.
        """
        QTimer.singleShot(0, self._refresh)

    @staticmethod
    def _ro(text):
        lbl = QLabel(str(text))
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return lbl

    @staticmethod
    def _edit(value="", placeholder=""):
        le = QLineEdit(str(value))
        le.setPlaceholderText(placeholder)
        return le

    @staticmethod
    def _sep():
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setFrameShadow(QFrame.Sunken)
        return f

    @staticmethod
    def _attr(feat, idx):
        """Feature attribute as Python value; None for NULL, QVariant, or missing index."""
        if idx < 0:
            return None
        val = feat.attribute(idx)
        if val is None:
            return None
        if isinstance(val, QVariant):
            return None if val.isNull() else val.value() if hasattr(val, "value") else None
        return val

    # ------------------------------------------------------------------
    # Circle geometry helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _circle_center(geom):
        c = geom.boundingBox().center()
        return QgsPointXY(c.x(), c.y())

    @staticmethod
    def _circle_radius_from_geom(geom):
        return geom.boundingBox().width() / 2.0

    @staticmethod
    def _build_circle_geom(cx, cy, radius):
        cs = QgsCircularString()
        cs.setPoints([
            QgsPoint(cx + radius, cy),
            QgsPoint(cx,          cy + radius),
            QgsPoint(cx - radius, cy),
            QgsPoint(cx,          cy - radius),
            QgsPoint(cx + radius, cy),
        ])
        return QgsGeometry(cs)

    # ------------------------------------------------------------------
    # Main refresh
    # ------------------------------------------------------------------

    def _refresh(self):
        if self._layer is None or self._fid is None:
            self._clear_form()
            return

        feat = self._layer.getFeature(self._fid)
        if not feat.isValid():
            self._clear_form()
            return

        geom     = feat.geometry()
        lyr_name = self._layer.name()

        self._clear_form()

        self._form.addRow("Layer:", self._ro(lyr_name))
        self._form.addRow("FID:",   self._ro(str(self._fid)))
        self._form.addRow(self._sep())

        if lyr_name == "_polylines" and not geom.isEmpty():
            self._build_polyline_rows(feat, geom)
        elif lyr_name == "_circles" and not geom.isEmpty():
            self._build_circle_rows(feat, geom)
        elif lyr_name == "_points" and not geom.isEmpty():
            self._build_point_rows(feat, geom)
        elif lyr_name == "_hatches" and not geom.isEmpty():
            self._build_hatch_rows(feat)
        elif not geom.isEmpty():
            self._form.addRow("Type:", self._ro(QgsWkbTypes.displayString(geom.wkbType())))

    # ------------------------------------------------------------------
    # Per-type row builders
    # ------------------------------------------------------------------

    def _build_polyline_rows(self, feat, geom):
        pts       = geom.asPolyline()
        is_closed = self._is_closed(pts)
        area_sqm  = QgsGeometry.fromPolygonXY([list(pts)]).area() if is_closed else 0.0
        area_ac   = area_sqm * 0.000247105

        self._form.addRow("Length:",   self._ro(f"{geom.length():.3f} m"))
        self._form.addRow("Vertices:", self._ro(str(len(pts))))

        closed_check = QCheckBox()
        closed_check.setChecked(is_closed)
        closed_check.stateChanged.connect(self._on_closed_toggled)
        self._form.addRow("Closed:", closed_check)

        self._form.addRow("Area (m²):", self._ro(f"{area_sqm:.3f}"))
        self._form.addRow("Area (ac):", self._ro(f"{area_ac:.6f}"))
        self._form.addRow(self._sep())
        self._form.addRow("Color:", self._make_color_button())

        # ── Line style ────────────────────────────────────────────────
        lt_idx = self._layer.fields().indexOf("line_type")
        lw_idx = self._layer.fields().indexOf("line_thickness")

        lt_combo = QComboBox()
        for name in ["solid", "dash", "dot", "dash dot", "dash dot dot"]:
            lt_combo.addItem(name)
        current_lt = self._attr(feat, lt_idx)
        lt_combo.setCurrentText(current_lt if current_lt else "solid")

        def on_lt_changed(text, _idx=lt_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)
                self._layer.triggerRepaint()

        lt_combo.currentTextChanged.connect(on_lt_changed)
        self._form.addRow("Line Type:", lt_combo)

        current_lw = self._attr(feat, lw_idx)
        lw_edit = self._edit(f"{float(current_lw):.2f}" if current_lw is not None else "0.40", "mm")

        def on_lw_edited(_idx=lw_idx):
            try:
                w = float(lw_edit.text())
            except ValueError:
                return
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(w, 3))
                self._layer.triggerRepaint()

        lw_edit.editingFinished.connect(on_lw_edited)
        self._form.addRow("Thickness:", lw_edit)

    def _build_circle_rows(self, feat, geom):
        radius_idx = self._layer.fields().indexOf("radius")
        radius     = feat.attribute(radius_idx) if radius_idx >= 0 else None
        center     = self._circle_center(geom)

        # ── Center ───────────────────────────────────────────────────
        cx_edit = self._edit(f"{center.x():.3f}", "x")
        cy_edit = self._edit(f"{center.y():.3f}", "y")

        def apply_center():
            try:
                cx = float(cx_edit.text())
                cy = float(cy_edit.text())
            except ValueError:
                return
            r = self._circle_radius_from_geom(self._layer.getFeature(self._fid).geometry())
            if not self._layer.isEditable():
                self._layer.startEditing()
            self._layer.changeGeometry(self._fid, self._build_circle_geom(cx, cy, r))
            self._write_circle_attrs(cx, cy, r)
            self._layer.triggerRepaint()
            self.geometry_changed.emit(self._layer, self._fid)
            self._deferred_refresh()

        cx_edit.editingFinished.connect(apply_center)
        cy_edit.editingFinished.connect(apply_center)
        self._form.addRow("Center X:", cx_edit)
        self._form.addRow("Center Y:", cy_edit)

        self._form.addRow(self._sep())

        # ── Radius / Diameter — coupled editable fields ───────────────
        r_edit = self._edit(f"{radius:.3f}"     if radius else "", "m")
        d_edit = self._edit(f"{radius * 2:.3f}" if radius else "", "m")

        def apply_radius(r):
            if r <= 0:
                return
            c = self._circle_center(self._layer.getFeature(self._fid).geometry())
            if not self._layer.isEditable():
                self._layer.startEditing()
            self._layer.changeGeometry(self._fid, self._build_circle_geom(c.x(), c.y(), r))
            self._write_circle_attrs(c.x(), c.y(), r)
            self._layer.triggerRepaint()
            self.geometry_changed.emit(self._layer, self._fid)
            self._deferred_refresh()

        def on_r_edited():
            try:
                r = float(r_edit.text())
            except ValueError:
                return
            d_edit.blockSignals(True)
            d_edit.setText(f"{r * 2:.3f}")
            d_edit.blockSignals(False)
            apply_radius(r)

        def on_d_edited():
            try:
                r = float(d_edit.text()) / 2.0
            except ValueError:
                return
            r_edit.blockSignals(True)
            r_edit.setText(f"{r:.3f}")
            r_edit.blockSignals(False)
            apply_radius(r)

        r_edit.editingFinished.connect(on_r_edited)
        d_edit.editingFinished.connect(on_d_edited)
        self._form.addRow("Radius:",   r_edit)
        self._form.addRow("Diameter:", d_edit)

        self._form.addRow(self._sep())

        # ── Derived read-only (always shown) ──────────────────────────
        if radius:
            circ     = 2 * math.pi * radius
            area_sqm = math.pi * radius ** 2
            area_ac  = area_sqm * 0.000247105
            self._form.addRow("Circumference:", self._ro(f"{circ:.3f} m"))
            self._form.addRow("Area (m²):",     self._ro(f"{area_sqm:.3f}"))
            self._form.addRow("Area (ac):",     self._ro(f"{area_ac:.6f}"))
        else:
            self._form.addRow("Circumference:", self._ro("—"))
            self._form.addRow("Area (m²):",     self._ro("—"))
            self._form.addRow("Area (ac):",     self._ro("—"))

        self._form.addRow("Color:", self._make_color_button())

        # ── Line style ────────────────────────────────────────────────
        lt_idx = self._layer.fields().indexOf("line_type")
        lw_idx = self._layer.fields().indexOf("line_thickness")

        lt_combo = QComboBox()
        for name in ["solid", "dash", "dot", "dash dot", "dash dot dot"]:
            lt_combo.addItem(name)
        current_lt = self._attr(feat, lt_idx)
        lt_combo.setCurrentText(current_lt if current_lt else "solid")

        def on_lt_changed(text, _idx=lt_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)
                self._layer.triggerRepaint()

        lt_combo.currentTextChanged.connect(on_lt_changed)
        self._form.addRow("Line Type:", lt_combo)

        current_lw = self._attr(feat, lw_idx)
        lw_edit = self._edit(f"{float(current_lw):.2f}" if current_lw is not None else "0.40", "mm")

        def on_lw_edited(_idx=lw_idx):
            try:
                w = float(lw_edit.text())
            except ValueError:
                return
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(w, 3))
                self._layer.triggerRepaint()

        lw_edit.editingFinished.connect(on_lw_edited)
        self._form.addRow("Thickness:", lw_edit)

    def _build_point_rows(self, feat, geom):
        pt = geom.asPoint()

        x_edit = self._edit(f"{pt.x():.3f}", "x")
        y_edit = self._edit(f"{pt.y():.3f}", "y")

        def apply_coords():
            try:
                x = float(x_edit.text())
                y = float(y_edit.text())
            except ValueError:
                return
            if not self._layer.isEditable():
                self._layer.startEditing()
            self._layer.changeGeometry(self._fid, QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            for fname, val in (("x", round(x, 4)), ("y", round(y, 4))):
                idx = self._layer.fields().indexOf(fname)
                if idx >= 0:
                    self._layer.changeAttributeValue(self._fid, idx, val)
            self._layer.triggerRepaint()
            self.geometry_changed.emit(self._layer, self._fid)
            self._deferred_refresh()

        x_edit.editingFinished.connect(apply_coords)
        y_edit.editingFinished.connect(apply_coords)
        self._form.addRow("X:", x_edit)
        self._form.addRow("Y:", y_edit)
        self._form.addRow(self._sep())
        self._form.addRow("Color:", self._make_color_button())

        # ── Symbol shape ──────────────────────────────────────────────
        sym_idx = self._layer.fields().indexOf("symbol")
        sym_combo = QComboBox()
        for name in ["circle", "square", "triangle", "star", "cross", "x", "diamond"]:
            sym_combo.addItem(name)
        current_sym = self._attr(feat, sym_idx)
        sym_combo.setCurrentText(current_sym if current_sym else "circle")

        def on_sym_changed(text, _idx=sym_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)
                self._layer.triggerRepaint()

        sym_combo.currentTextChanged.connect(on_sym_changed)
        self._form.addRow("Symbol:", sym_combo)

        size_idx = self._layer.fields().indexOf("symbol_size")
        current_size = self._attr(feat, size_idx)
        size_edit = self._edit(f"{float(current_size):.2f}" if current_size is not None else "2.00", "px")

        def on_size_edited(_idx=size_idx):
            try:
                s = float(size_edit.text())
            except ValueError:
                return
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(s, 3))
                self._layer.triggerRepaint()

        size_edit.editingFinished.connect(on_size_edited)
        self._form.addRow("Size:", size_edit)

        # ── SVG pin / custom marker ───────────────────────────────────
        svg_idx     = self._layer.fields().indexOf("symbol_svg")
        current_svg = self._attr(feat, svg_idx) or ""

        self._form.addRow(self._sep())

        svg_name_lbl = self._ro(os.path.basename(current_svg) if current_svg else "None")
        self._form.addRow("SVG:", svg_name_lbl)

        def _svg_paths():
            try:
                paths = QgsApplication.svgPaths()
                return paths[0] if paths else ""
            except Exception:
                return ""

        def browse_svg():
            path, _ = QFileDialog.getOpenFileName(
                None, "Select SVG pin", _svg_paths(), "SVG files (*.svg)"
            )
            if not path:
                return
            if svg_idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, svg_idx, path)
                apply_point_color_renderer(self._layer)
                self._layer.triggerRepaint()
            self._deferred_refresh()

        def clear_svg():
            if svg_idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, svg_idx, None)
                apply_point_color_renderer(self._layer)
                self._layer.triggerRepaint()
            self._deferred_refresh()

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(browse_svg)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(clear_svg)
        clear_btn.setEnabled(bool(current_svg))

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addWidget(browse_btn)
        btn_layout.addWidget(clear_btn)
        self._form.addRow(btn_row)

        picker = self._make_icon_picker(current_svg, svg_idx)
        if picker is not None:
            self._form.addRow("Icons:", picker)

    def _build_hatch_rows(self, feat):
        pat_idx  = self._layer.fields().indexOf("fill_pattern")
        size_idx = self._layer.fields().indexOf("element_size")
        ang_idx  = self._layer.fields().indexOf("angle")
        opa_idx  = self._layer.fields().indexOf("opacity")

        # ── Fill pattern ──────────────────────────────────────────────
        pat_combo = QComboBox()
        for name in ["lines", "diagonal", "crosshatch", "dots", "pavers"]:
            pat_combo.addItem(name)
        current_pat = self._attr(feat, pat_idx) or "lines"
        pat_combo.setCurrentText(current_pat)

        def on_pat_changed(text, _idx=pat_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)
                apply_hatch_renderer(self._layer)
                self._layer.triggerRepaint()

        pat_combo.currentTextChanged.connect(on_pat_changed)
        self._form.addRow("Pattern:", pat_combo)

        # ── Element size ──────────────────────────────────────────────
        current_size = self._attr(feat, size_idx)
        size_edit = self._edit(
            f"{float(current_size):.2f}" if current_size is not None else "1.00",
            "map units",
        )

        def on_size_edited(_idx=size_idx):
            try:
                v = float(size_edit.text())
            except ValueError:
                return
            if v <= 0:
                return
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(v, 4))
                self._layer.triggerRepaint()

        size_edit.editingFinished.connect(on_size_edited)
        self._form.addRow("Size:", size_edit)

        # ── Angle ─────────────────────────────────────────────────────
        current_ang = self._attr(feat, ang_idx)
        ang_edit = self._edit(
            f"{float(current_ang):.1f}" if current_ang is not None else "45.0",
            "degrees",
        )

        def on_ang_edited(_idx=ang_idx):
            try:
                v = float(ang_edit.text()) % 360
            except ValueError:
                return
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(v, 1))
                self._layer.triggerRepaint()

        ang_edit.editingFinished.connect(on_ang_edited)
        self._form.addRow("Angle:", ang_edit)

        # ── Opacity ───────────────────────────────────────────────────
        current_opa = self._attr(feat, opa_idx)
        opa_edit = self._edit(
            f"{float(current_opa):.2f}" if current_opa is not None else "0.70",
            "0 – 1",
        )

        def on_opa_edited(_idx=opa_idx):
            try:
                v = max(0.0, min(1.0, float(opa_edit.text())))
            except ValueError:
                return
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(v, 2))
                self._layer.triggerRepaint()

        opa_edit.editingFinished.connect(on_opa_edited)
        self._form.addRow("Opacity:", opa_edit)

        self._form.addRow(self._sep())
        self._form.addRow("Color:", self._make_color_button())

    # ------------------------------------------------------------------
    # SVG icon picker
    # ------------------------------------------------------------------

    def _make_icon_picker(self, current_svg, svg_idx):
        """Scrollable grid of plugin-bundled SVG icons the user can click to select."""
        if not os.path.isdir(_PLUGIN_ICONS_DIR):
            return None

        svgs = sorted(
            f for f in os.listdir(_PLUGIN_ICONS_DIR) if f.lower().endswith(".svg")
        )
        if not svgs:
            return None

        ICON_PX = 44
        COLS    = 4

        container = QWidget()
        grid      = QGridLayout(container)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(4)

        for i, fname in enumerate(svgs):
            path = os.path.join(_PLUGIN_ICONS_DIR, fname)

            pix = QPixmap(ICON_PX, ICON_PX)
            pix.fill(Qt.transparent)
            renderer = QSvgRenderer(path)
            if renderer.isValid():
                painter = QPainter(pix)
                painter.setRenderHint(QPainter.Antialiasing)
                renderer.render(painter, QRectF(0, 0, ICON_PX, ICON_PX))
                painter.end()

            btn = QPushButton()
            btn.setIcon(QIcon(pix))
            btn.setIconSize(QSize(ICON_PX - 4, ICON_PX - 4))
            btn.setFixedSize(ICON_PX + 6, ICON_PX + 6)
            btn.setToolTip(os.path.splitext(fname)[0])
            btn.setFlat(True)

            selected = bool(current_svg and os.path.abspath(path) == os.path.abspath(current_svg))
            btn.setStyleSheet(
                "border: 2px solid #0078d4; background: #e3f2fd; border-radius: 4px;"
                if selected else
                "border: 1px solid #bbb; border-radius: 4px;"
            )

            def on_pick(checked=False, p=path, _idx=svg_idx):
                if _idx >= 0 and self._fid is not None:
                    if not self._layer.isEditable():
                        self._layer.startEditing()
                    self._layer.changeAttributeValue(self._fid, _idx, p)
                    apply_point_color_renderer(self._layer)
                    self._layer.triggerRepaint()
                self._deferred_refresh()

            btn.clicked.connect(on_pick)
            grid.addWidget(btn, i // COLS, i % COLS)

        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(min(len(svgs), 3) * (ICON_PX + 10) + 8)
        scroll.setFrameShape(QFrame.StyledPanel)
        return scroll

    def _write_circle_attrs(self, cx, cy, radius):
        attrs = circle_attrs(cx, cy, radius)
        for fname, val in attrs.items():
            idx = self._layer.fields().indexOf(fname)
            if idx >= 0:
                self._layer.changeAttributeValue(self._fid, idx, val)

    # ------------------------------------------------------------------
    # Color picker (polyline)
    # ------------------------------------------------------------------

    def _get_layer_color(self):
        try:
            return self._layer.renderer().symbol().color()
        except Exception:
            return QColor(0, 0, 255)

    def _get_current_color(self):
        """Read color from the feature's 'color' attribute if set, else from layer renderer."""
        if self._layer is not None and self._fid is not None:
            color_idx = self._layer.fields().indexOf("color")
            if color_idx >= 0:
                val = self._layer.getFeature(self._fid).attribute(color_idx)
                if val:
                    c = QColor(str(val))
                    if c.isValid():
                        return c
        return self._get_layer_color()

    def _make_color_button(self):
        color = self._get_current_color()
        btn   = QPushButton()
        btn.setStyleSheet(
            f"background-color: rgb({color.red()},{color.green()},{color.blue()});"
            "border: 1px solid #666; border-radius: 2px; min-height: 20px;"
        )

        def on_clicked():
            chosen = QColorDialog.getColor(self._get_current_color(), None, "Color")
            if chosen.isValid():
                color_idx = self._layer.fields().indexOf("color")
                if color_idx >= 0 and self._fid is not None:
                    if not self._layer.isEditable():
                        self._layer.startEditing()
                    self._layer.changeAttributeValue(self._fid, color_idx, chosen.name())
                lyr_name = self._layer.name()
                if lyr_name == "_circles":
                    apply_circle_color_renderer(self._layer)
                elif lyr_name == "_polylines":
                    apply_polyline_color_renderer(self._layer)
                elif lyr_name == "_points":
                    apply_point_color_renderer(self._layer)
                elif lyr_name == "_hatches":
                    apply_hatch_renderer(self._layer)
                self._layer.triggerRepaint()
                self._deferred_refresh()

        btn.clicked.connect(on_clicked)
        return btn

    # ------------------------------------------------------------------
    # Closed toggle
    # ------------------------------------------------------------------

    @staticmethod
    def _is_closed(pts):
        return (len(pts) >= 4
                and abs(pts[0].x() - pts[-1].x()) < 1e-9
                and abs(pts[0].y() - pts[-1].y()) < 1e-9)

    def _on_closed_toggled(self, state):
        if self._updating:
            return
        if self._layer is None or self._fid is None:
            return

        want_closed = (state == Qt.Checked)

        feat = self._layer.getFeature(self._fid)
        if not feat.isValid():
            return
        geom = feat.geometry()
        if geom.isEmpty():
            return

        pts              = list(geom.asPolyline())
        currently_closed = self._is_closed(pts)

        if want_closed == currently_closed:
            return

        if not self._layer.isEditable():
            self._layer.startEditing()

        if want_closed:
            pts.append(QgsPointXY(pts[0].x(), pts[0].y()))
            new_geom = QgsGeometry.fromPolylineXY(pts)
            self._layer.changeGeometry(self._fid, new_geom)
            self._write_attrs(True, new_geom)
        else:
            if len(pts) > 3:
                pts.pop()
                new_geom = QgsGeometry.fromPolylineXY(pts)
                self._layer.changeGeometry(self._fid, new_geom)
                self._write_attrs(False, new_geom)
            else:
                # Cannot open a polyline with only 3 points — revert silently
                self._updating = True
                cb = self.sender()
                if cb:
                    cb.setChecked(True)
                self._updating = False
                return

        self._layer.triggerRepaint()
        self.geometry_changed.emit(self._layer, self._fid)
        self._deferred_refresh()

    def _write_attrs(self, is_closed, geom):
        layer = self._layer
        fid   = self._fid

        closed_idx = layer.fields().indexOf("closed")
        if closed_idx >= 0:
            layer.changeAttributeValue(fid, closed_idx, is_closed)

        area_sqm_idx   = layer.fields().indexOf("area_sqm")
        area_acres_idx = layer.fields().indexOf("area_acres")

        if is_closed:
            pts        = geom.asPolyline()
            area_sqm   = QgsGeometry.fromPolygonXY([list(pts)]).area()
            area_acres = area_sqm * 0.000247105
        else:
            area_sqm = area_acres = 0.0

        if area_sqm_idx >= 0:
            layer.changeAttributeValue(fid, area_sqm_idx, round(area_sqm, 3))
        if area_acres_idx >= 0:
            layer.changeAttributeValue(fid, area_acres_idx, round(area_acres, 6))
