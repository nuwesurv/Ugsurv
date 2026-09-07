import math
import os

from qgis.PyQt.QtCore import Qt, QRectF, QSize, QTimer, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (
    QColorDialog, QComboBox, QDockWidget, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QCheckBox, QLineEdit, QPushButton,
    QScrollArea, QSpinBox, QDoubleSpinBox, QTextEdit, QWidget, QVBoxLayout,
)
from qgis.core import (
    QgsApplication, QgsCircularString, QgsCompoundCurve, QgsGeometry, QgsPoint, QgsPointXY, QgsWkbTypes,
)

_PLUGIN_ICONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "map_icons", "point_icons",
)

try:
    from ..tools.layer_utils import circle_attrs
    from ..core.renderer_utils import (
        apply_circle_color_renderer,
        apply_hatch_renderer,
        apply_polyline_color_renderer,
        apply_point_color_renderer,
        apply_dimension_style,
    )
except ImportError:
    def circle_attrs(*a, **k): return {}
    def apply_circle_color_renderer(*a, **k): pass
    def apply_hatch_renderer(*a, **k): pass
    def apply_polyline_color_renderer(*a, **k): pass
    def apply_point_color_renderer(*a, **k): pass
    def apply_dimension_style(*a, **k): pass


class PropertiesDock(QDockWidget):
    """Right-side dock showing editable properties of the selected feature."""

    geometry_changed = pyqtSignal(object, int)

    def __init__(self, parent=None):
        super().__init__("Properties", parent)
        self.setObjectName("UgsurvPropertiesDock")
        self.setMinimumWidth(210)

        self._layer      = None
        self._fid        = None
        self._updating   = False
        self._selection  = []   # [(layer, fid), ...] for multi-select; empty when single

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
        scroll.setFocusPolicy(Qt.NoFocus)

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
        self._layer     = layer
        self._fid       = fid
        self._selection = []
        self._refresh()

    def update_features(self, items):
        """Show merged properties for multiple selected features."""
        self._layer     = None
        self._fid       = None
        self._selection = list(items)
        self._refresh()

    def clear_selection(self):
        self._layer     = None
        self._fid       = None
        self._selection = []
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
        cc = QgsCompoundCurve()
        cc.addCurve(cs)
        return QgsGeometry(cc)

    # ------------------------------------------------------------------
    # Main refresh
    # ------------------------------------------------------------------

    def _refresh(self):
        if self._selection:
            self._refresh_multi()
            return
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

        if lyr_name == "lines" and not geom.isEmpty():
            self._build_polyline_rows(feat, geom)
        elif lyr_name == "circles" and not geom.isEmpty():
            self._build_circle_rows(feat, geom)
        elif lyr_name == "points" and not geom.isEmpty():
            self._build_point_rows(feat, geom)
        elif lyr_name == "_hatches" and not geom.isEmpty():
            self._build_hatch_rows(feat)
        elif lyr_name == "dimensions" and not geom.isEmpty():
            self._build_dimension_rows(feat)
        elif lyr_name == "text":
            self._build_text_rows(feat)
        elif not geom.isEmpty():
            self._form.addRow("Type:", self._ro(QgsWkbTypes.displayString(geom.wkbType())))

    # ------------------------------------------------------------------
    # Per-type row builders
    # ------------------------------------------------------------------

    def _build_polyline_rows(self, feat, geom):  # noqa: C901
        from qgis.core import QgsPointXY as _Pt
        pts       = [_Pt(v.x(), v.y()) for v in geom.vertices()]
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

        cl_idx = self._layer.fields().indexOf("cad_layer")
        current_cl = self._attr(feat, cl_idx) or ""
        cl_edit = self._edit(current_cl, "cad layer name")

        def on_cl_edited(_idx=cl_idx):
            text = cl_edit.text().strip()
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)

        cl_edit.editingFinished.connect(on_cl_edited)
        self._form.addRow("Cad Layer:", cl_edit)
        self._form.addRow(self._sep())
        self._form.addRow("Color:", self._make_color_button())

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
                apply_polyline_color_renderer(self._layer)
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
                apply_polyline_color_renderer(self._layer)
                self._layer.triggerRepaint()

        lw_edit.editingFinished.connect(on_lw_edited)
        self._form.addRow("Thickness:", lw_edit)

    def _build_circle_rows(self, feat, geom):  # noqa: C901
        radius_idx = self._layer.fields().indexOf("radius")
        radius     = feat.attribute(radius_idx) if radius_idx >= 0 else None
        center     = self._circle_center(geom)

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
                apply_circle_color_renderer(self._layer)
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
                apply_circle_color_renderer(self._layer)
                self._layer.triggerRepaint()

        lw_edit.editingFinished.connect(on_lw_edited)
        self._form.addRow("Thickness:", lw_edit)

    def _build_point_rows(self, feat, geom):  # noqa: C901
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

        cl_idx = self._layer.fields().indexOf("cad_layer")
        current_cl = self._attr(feat, cl_idx) or ""
        cl_edit = self._edit(current_cl, "cad layer name")

        def on_cl_edited(_idx=cl_idx):
            text = cl_edit.text().strip()
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)

        cl_edit.editingFinished.connect(on_cl_edited)
        self._form.addRow("Cad Layer:", cl_edit)
        self._form.addRow(self._sep())

        desc_idx = self._layer.fields().indexOf("Description")
        current_desc = self._attr(feat, desc_idx)
        desc_edit = self._edit(current_desc or "", "e.g. BM, TP, WH…")

        def on_desc_edited(_didx=desc_idx):
            text = desc_edit.text().strip()
            stored = text if text else None
            if _didx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _didx, stored)
                if stored is not None:
                    _svg_idx = self._layer.fields().lookupField("symbol")
                    if _svg_idx >= 0:
                        if stored.lower() == "basic":
                            self._layer.changeAttributeValue(self._fid, _svg_idx, "basic")
                            apply_point_color_renderer(self._layer)
                        elif os.path.isdir(_PLUGIN_ICONS_DIR):
                            for _fname in os.listdir(_PLUGIN_ICONS_DIR):
                                if _fname.lower().endswith(".svg"):
                                    if os.path.splitext(_fname)[0].lower() == stored.lower():
                                        _svg_path = os.path.join(_PLUGIN_ICONS_DIR, _fname)
                                        self._layer.changeAttributeValue(self._fid, _svg_idx, _svg_path)
                                        apply_point_color_renderer(self._layer)
                                        break
                self._layer.triggerRepaint()
                self._deferred_refresh()

        desc_edit.editingFinished.connect(on_desc_edited)
        self._form.addRow("Description:", desc_edit)
        self._form.addRow(self._sep())

        size_idx = self._layer.fields().indexOf("symbol_size")
        current_size = self._attr(feat, size_idx)

        size_spin = QDoubleSpinBox()
        size_spin.setRange(0.1, 500.0)
        size_spin.setSingleStep(0.5)
        size_spin.setDecimals(2)
        size_spin.setSuffix(" px")
        size_spin.setValue(float(current_size) if current_size is not None else 2.0)

        def on_size_changed(v, _idx=size_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(v, 3))
                self._layer.triggerRepaint()

        size_spin.valueChanged.connect(on_size_changed)
        self._form.addRow("Size:", size_spin)

        svg_idx     = self._layer.fields().lookupField("symbol")
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

    def _build_text_rows(self, feat):
        lt_idx = self._layer.fields().indexOf("label_text")
        cl_idx = self._layer.fields().indexOf("cad_layer")

        current_text = self._attr(feat, lt_idx) or ""
        te = QTextEdit()
        te.setPlainText(current_text)
        te.setMinimumHeight(72)
        te.setMaximumHeight(160)

        _save_timer = QTimer(te)
        _save_timer.setSingleShot(True)
        _save_timer.setInterval(300)

        def _do_save(_idx=lt_idx):
            val = te.toPlainText()
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, val)
                self._layer.triggerRepaint()

        _save_timer.timeout.connect(_do_save)
        te.textChanged.connect(_save_timer.start)
        self._form.addRow("Label:", te)
        self._form.addRow(self._sep())

        current_cl = self._attr(feat, cl_idx) or ""
        cl_edit = self._edit(current_cl, "cad layer name")

        def on_cl_edited(_idx=cl_idx):
            val = cl_edit.text().strip()
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, val)

        cl_edit.editingFinished.connect(on_cl_edited)
        self._form.addRow("Cad Layer:", cl_edit)

    def _build_hatch_rows(self, feat):  # noqa: C901
        pat_idx  = self._layer.fields().indexOf("fill_pattern")
        size_idx = self._layer.fields().indexOf("element_size")
        ang_idx  = self._layer.fields().indexOf("angle")
        opa_idx  = self._layer.fields().indexOf("opacity")

        pat_combo = QComboBox()
        for name in ["lines", "diagonal", "crosshatch", "dots", "pavers", "wetland"]:
            pat_combo.addItem(name)
        current_pat = self._attr(feat, pat_idx) or "lines"
        pat_combo.setCurrentText(current_pat)

        def on_pat_changed(text, _idx=pat_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)
                if text == "wetland":
                    col_idx = self._layer.fields().indexOf("color")
                    if col_idx >= 0:
                        self._layer.changeAttributeValue(self._fid, col_idx, "#46aeef")
                apply_hatch_renderer(self._layer)
                self._layer.triggerRepaint()
                self._deferred_refresh()

        pat_combo.currentTextChanged.connect(on_pat_changed)
        self._form.addRow("Pattern:", pat_combo)

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

    def _build_dimension_rows(self, feat):  # noqa: C901
        dist_idx = self._layer.fields().indexOf("distance")
        dp_idx   = self._layer.fields().indexOf("decimal_places")
        size_idx = self._layer.fields().indexOf("text_size")
        font_idx = self._layer.fields().indexOf("font_type")

        dp = self._attr(feat, dp_idx)
        dp = int(dp) if dp is not None else 3
        dist = self._attr(feat, dist_idx)
        self._form.addRow("Distance:", self._ro(f"{round(dist, dp)} m" if dist is not None else "—"))
        self._form.addRow(self._sep())

        dp_spin = QSpinBox()
        dp_spin.setRange(0, 6)
        dp_spin.setValue(dp)

        def on_dp_changed(v, _idx=dp_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, v)
                apply_dimension_style(self._layer)

        dp_spin.valueChanged.connect(on_dp_changed)
        self._form.addRow("Decimals:", dp_spin)

        self._form.addRow("Text Color:", self._make_color_button())

        current_size = self._attr(feat, size_idx)
        size_spin = QDoubleSpinBox()
        size_spin.setRange(0.1, 100.0)
        size_spin.setSingleStep(0.1)
        size_spin.setDecimals(3)
        size_spin.setSuffix(" m")
        size_spin.setValue(float(current_size) if current_size is not None else 1.5)

        def on_size_changed(v, _idx=size_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, round(v, 3))
                apply_dimension_style(self._layer)

        size_spin.valueChanged.connect(on_size_changed)
        self._form.addRow("Font Size:", size_spin)

        font_combo = QComboBox()
        for name in ["Century Gothic", "Arial", "Calibri", "Times New Roman", "Courier New"]:
            font_combo.addItem(name)
        current_font = self._attr(feat, font_idx)
        font_combo.setCurrentText(current_font if current_font else "Century Gothic")

        def on_font_changed(text, _idx=font_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, text)
                apply_dimension_style(self._layer)

        font_combo.currentTextChanged.connect(on_font_changed)
        self._form.addRow("Font:", font_combo)

    # ------------------------------------------------------------------
    # SVG icon picker
    # ------------------------------------------------------------------

    def _make_icon_picker(self, current_svg, svg_idx):
        if not os.path.isdir(_PLUGIN_ICONS_DIR):
            return None

        svgs = sorted(
            f for f in os.listdir(_PLUGIN_ICONS_DIR) if f.lower().endswith(".svg")
        )

        ICON_PX = 44
        COLS    = 4

        container = QWidget()
        grid      = QGridLayout(container)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(4)

        # ── "Basic" tile — rendered from basic.svg for visual consistency ──
        _basic_svg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "map_icons", "basic.svg",
        )
        basic_pix = QPixmap(ICON_PX, ICON_PX)
        basic_pix.fill(Qt.transparent)
        _bsvg = QSvgRenderer(_basic_svg_path)
        if _bsvg.isValid():
            bp = QPainter(basic_pix)
            bp.setRenderHint(QPainter.Antialiasing)
            _bsvg.render(bp, QRectF(0, 0, ICON_PX, ICON_PX))
            bp.end()
        else:
            bp = QPainter(basic_pix)
            bp.setRenderHint(QPainter.Antialiasing)
            bp.setPen(QPen(QColor(47, 47, 47), 1.5))
            bp.setBrush(QBrush(QColor(255, 255, 255)))
            r = ICON_PX // 3
            cx, cy = ICON_PX // 2, ICON_PX // 2
            bp.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
            bp.end()

        basic_btn = QPushButton()
        basic_btn.setIcon(QIcon(basic_pix))
        basic_btn.setIconSize(QSize(ICON_PX - 4, ICON_PX - 4))
        basic_btn.setFixedSize(ICON_PX + 6, ICON_PX + 6)
        basic_btn.setToolTip("basic")
        basic_btn.setFlat(True)
        basic_selected = not bool(current_svg)
        basic_btn.setStyleSheet(
            "border: 2px solid #0078d4; background: #e3f2fd; border-radius: 4px;"
            if basic_selected else
            "border: 1px solid #bbb; border-radius: 4px;"
        )

        def on_pick_basic(checked=False, _idx=svg_idx):
            if _idx >= 0 and self._fid is not None:
                if not self._layer.isEditable():
                    self._layer.startEditing()
                self._layer.changeAttributeValue(self._fid, _idx, "basic")
                apply_point_color_renderer(self._layer)
                self._layer.triggerRepaint()
            self._deferred_refresh()

        basic_btn.clicked.connect(on_pick_basic)
        grid.addWidget(basic_btn, 0, 0)

        # ── SVG icon tiles ──────────────────────────────────────────────────
        for i, fname in enumerate(svgs):
            path = os.path.join(_PLUGIN_ICONS_DIR, fname)
            pos  = i + 1  # offset by 1 for the Basic tile

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
            grid.addWidget(btn, pos // COLS, pos % COLS)

        n_items = len(svgs) + 1
        n_rows  = (n_items + COLS - 1) // COLS
        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(min(n_rows, 3) * (ICON_PX + 10) + 8)
        scroll.setFrameShape(QFrame.StyledPanel)
        return scroll

    # ------------------------------------------------------------------
    # Multi-feature properties
    # ------------------------------------------------------------------

    def _refresh_multi(self):
        self._clear_form()
        items = self._selection
        n = len(items)

        layer_names = sorted(set(lyr.name() for lyr, _ in items))
        self._form.addRow("Selection:", self._ro(f"{n} features"))
        self._form.addRow("Layer(s):",  self._ro(", ".join(layer_names)))
        self._form.addRow(self._sep())

        if len(layer_names) != 1:
            return  # mixed types — header only

        # Cache all features up-front so multi_val doesn't call getFeature
        # once per field per feature (N×F → N calls).
        _feat_cache = {(lyr.id(), fid): lyr.getFeature(fid) for lyr, fid in items}

        def multi_val(fname):
            vals = []
            for lyr, fid in items:
                idx = lyr.fields().indexOf(fname)
                if idx < 0:
                    return None, False
                feat = _feat_cache.get((lyr.id(), fid))
                if feat is None:
                    return None, False
                val  = self._attr(feat, idx)
                vals.append(str(val) if val is not None else "")
            if not vals:
                return None, False
            return (vals[0], True) if len(set(vals)) == 1 else (None, False)

        def apply_all(fname, value):
            for lyr, fid in items:
                idx = lyr.fields().indexOf(fname)
                if idx < 0:
                    continue
                if not lyr.isEditable():
                    lyr.startEditing()
                lyr.changeAttributeValue(fid, idx, value)

        def repaint_all():
            seen = set()
            for lyr, _ in items:
                lid = lyr.id()
                if lid not in seen:
                    seen.add(lid)
                    lyr.triggerRepaint()

        lyr_name = layer_names[0]
        if lyr_name == "points":
            self._build_multi_point_rows(items, multi_val, apply_all, repaint_all)
        elif lyr_name == "lines":
            self._build_multi_polyline_rows(items, multi_val, apply_all, repaint_all)
        elif lyr_name == "circles":
            self._build_multi_circle_rows(items, multi_val, apply_all, repaint_all)
        elif lyr_name == "_hatches":
            self._build_multi_hatch_rows(items, multi_val, apply_all, repaint_all)
        elif lyr_name == "text":
            self._build_multi_text_rows(items, multi_val, apply_all, repaint_all)

    # ------------------------------------------------------------------
    # Multi-feature type-specific builders
    # (mirror the single-feature _build_*_rows, minus geometry fields)
    # ------------------------------------------------------------------

    def _build_multi_point_rows(self, items, multi_val, apply_all, repaint_all):  # noqa: C901
        # Resolved once with lookupField (case-insensitive) — same as single-select
        svg_idx = items[0][0].fields().lookupField("symbol")

        val, is_common = multi_val("cad_layer")
        cl_edit = self._edit(val if is_common else "", "cad layer name")
        if not is_common:
            cl_edit.setPlaceholderText("(mixed)")

        def on_cl():
            text = cl_edit.text().strip()
            if text:
                apply_all("cad_layer", text)

        cl_edit.editingFinished.connect(on_cl)
        self._form.addRow("Cad Layer:", cl_edit)
        self._form.addRow(self._sep())

        val, is_common = multi_val("Description")
        desc_edit = self._edit(val if is_common else "", "e.g. BM, TP, WH…")
        if not is_common:
            desc_edit.setPlaceholderText("(mixed)")

        def on_desc(_svg_idx=svg_idx):
            text = desc_edit.text().strip()
            stored = text if text else None
            apply_all("Description", stored)
            if stored is not None and _svg_idx >= 0:
                svg_path = None
                if stored.lower() == "basic":
                    svg_path = "basic"
                elif os.path.isdir(_PLUGIN_ICONS_DIR):
                    for _fname in os.listdir(_PLUGIN_ICONS_DIR):
                        if (_fname.lower().endswith(".svg")
                                and os.path.splitext(_fname)[0].lower() == stored.lower()):
                            svg_path = os.path.join(_PLUGIN_ICONS_DIR, _fname)
                            break
                if svg_path is not None:
                    for lyr, fid in items:
                        if not lyr.isEditable():
                            lyr.startEditing()
                        lyr.changeAttributeValue(fid, _svg_idx, svg_path)
            seen = set()
            for lyr, _ in items:
                if lyr.id() not in seen:
                    seen.add(lyr.id())
                    apply_point_color_renderer(lyr)
            repaint_all()
            self._deferred_refresh()

        desc_edit.editingFinished.connect(on_desc)
        self._form.addRow("Description:", desc_edit)
        self._form.addRow(self._sep())

        val, is_common = multi_val("symbol_size")
        size_spin = QDoubleSpinBox()
        size_spin.setRange(0.0, 500.0)
        size_spin.setSingleStep(0.5)
        size_spin.setDecimals(2)
        size_spin.setSuffix(" px")
        size_spin.setSpecialValueText("(mixed)")
        if is_common and val:
            try:
                size_spin.setValue(float(val))
            except (ValueError, TypeError):
                size_spin.setValue(0.0)
        else:
            size_spin.setValue(0.0)

        def on_size(v):
            if v > 0:
                apply_all("symbol_size", round(v, 3))
                repaint_all()

        size_spin.valueChanged.connect(on_size)
        self._form.addRow("Size:", size_spin)

        svg_val, svg_common = multi_val("symbol")
        current_svg = svg_val if svg_common else ""

        self._form.addRow(self._sep())

        if svg_common and current_svg:
            svg_lbl = self._ro(os.path.basename(current_svg))
        elif svg_common:
            svg_lbl = self._ro("None")
        else:
            svg_lbl = self._ro("(mixed)")
        self._form.addRow("SVG:", svg_lbl)

        def _apply_svg(path_or_none, _idx=svg_idx):
            if _idx < 0:
                return
            layers_done = {}
            for lyr, fid in items:
                lid = lyr.id()
                if lid not in layers_done:
                    if not lyr.isEditable():
                        lyr.startEditing()
                    lyr.beginEditCommand("Set SVG")
                    layers_done[lid] = lyr
                lyr.changeAttributeValue(fid, _idx, path_or_none)
            for lyr in layers_done.values():
                lyr.endEditCommand()
                apply_point_color_renderer(lyr)
                lyr.triggerRepaint()

        def browse_svg():
            path, _ = QFileDialog.getOpenFileName(None, "Select SVG pin", "", "SVG files (*.svg)")
            if path:
                _apply_svg(path)
                self._deferred_refresh()

        def clear_svg():
            _apply_svg(None)
            self._deferred_refresh()

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(browse_svg)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(clear_svg)
        clear_btn.setEnabled(bool(svg_common and current_svg))
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addWidget(browse_btn)
        btn_layout.addWidget(clear_btn)
        self._form.addRow(btn_row)

        picker = self._make_multi_icon_picker(items, current_svg, svg_idx)
        if picker is not None:
            self._form.addRow("Icons:", picker)

    def _build_multi_polyline_rows(self, items, multi_val, apply_all, repaint_all):  # noqa: C901
        val, is_common = multi_val("cad_layer")
        cl_edit = self._edit(val if is_common else "", "cad layer name")
        if not is_common:
            cl_edit.setPlaceholderText("(mixed)")

        def on_cl():
            text = cl_edit.text().strip()
            if text:
                apply_all("cad_layer", text)

        cl_edit.editingFinished.connect(on_cl)
        self._form.addRow("Cad Layer:", cl_edit)
        self._form.addRow(self._sep())
        self._form.addRow("Color:", self._make_multi_color_button(items))

        val, is_common = multi_val("line_type")
        lt_combo = QComboBox()
        lt_combo.addItem("")
        for name in ["solid", "dash", "dot", "dash dot", "dash dot dot"]:
            lt_combo.addItem(name)
        if is_common and val:
            lt_combo.setCurrentText(val)

        def _rebuild_line_renderer():
            seen = set()
            for lyr, _ in items:
                lid = lyr.id()
                if lid not in seen:
                    seen.add(lid)
                    apply_polyline_color_renderer(lyr)
            repaint_all()

        def on_lt(text):
            if not text:
                return
            apply_all("line_type", text)
            _rebuild_line_renderer()

        lt_combo.currentTextChanged.connect(on_lt)
        self._form.addRow("Line Type:", lt_combo)

        val, is_common = multi_val("line_thickness")
        lw_edit = self._edit(
            f"{float(val):.2f}" if is_common and val else "", "mm"
        )
        if not is_common:
            lw_edit.setPlaceholderText("(mixed)")

        def on_lw():
            try:
                w = float(lw_edit.text())
            except ValueError:
                return
            apply_all("line_thickness", round(w, 3))
            _rebuild_line_renderer()

        lw_edit.editingFinished.connect(on_lw)
        self._form.addRow("Thickness:", lw_edit)

    def _build_multi_circle_rows(self, items, multi_val, apply_all, repaint_all):  # noqa: C901
        self._form.addRow("Color:", self._make_multi_color_button(items))

        val, is_common = multi_val("line_type")
        lt_combo = QComboBox()
        lt_combo.addItem("")
        for name in ["solid", "dash", "dot", "dash dot", "dash dot dot"]:
            lt_combo.addItem(name)
        if is_common and val:
            lt_combo.setCurrentText(val)

        def _rebuild_circle_renderer():
            seen = set()
            for lyr, _ in items:
                lid = lyr.id()
                if lid not in seen:
                    seen.add(lid)
                    apply_circle_color_renderer(lyr)
            repaint_all()

        def on_lt(text):
            if not text:
                return
            apply_all("line_type", text)
            _rebuild_circle_renderer()

        lt_combo.currentTextChanged.connect(on_lt)
        self._form.addRow("Line Type:", lt_combo)

        val, is_common = multi_val("line_thickness")
        lw_edit = self._edit(
            f"{float(val):.2f}" if is_common and val else "", "mm"
        )
        if not is_common:
            lw_edit.setPlaceholderText("(mixed)")

        def on_lw():
            try:
                w = float(lw_edit.text())
            except ValueError:
                return
            apply_all("line_thickness", round(w, 3))
            _rebuild_circle_renderer()

        lw_edit.editingFinished.connect(on_lw)
        self._form.addRow("Thickness:", lw_edit)

    def _build_multi_hatch_rows(self, items, multi_val, apply_all, repaint_all):  # noqa: C901
        val, is_common = multi_val("fill_pattern")
        pat_combo = QComboBox()
        pat_combo.addItem("")
        for name in ["lines", "diagonal", "crosshatch", "dots", "pavers", "wetland"]:
            pat_combo.addItem(name)
        if is_common and val:
            pat_combo.setCurrentText(val)

        def on_pat(text):
            if not text:
                return
            apply_all("fill_pattern", text)
            if text == "wetland":
                apply_all("color", "#46aeef")
            seen = set()
            for lyr, _ in items:
                if lyr.id() not in seen:
                    seen.add(lyr.id())
                    apply_hatch_renderer(lyr)
            repaint_all()
            self._deferred_refresh()

        pat_combo.currentTextChanged.connect(on_pat)
        self._form.addRow("Pattern:", pat_combo)

        val, is_common = multi_val("element_size")
        sz_edit = self._edit(
            f"{float(val):.2f}" if is_common and val else "", "map units"
        )
        if not is_common:
            sz_edit.setPlaceholderText("(mixed)")

        def on_sz():
            try:
                v = float(sz_edit.text())
            except ValueError:
                return
            if v > 0:
                apply_all("element_size", round(v, 4))
                repaint_all()

        sz_edit.editingFinished.connect(on_sz)
        self._form.addRow("Size:", sz_edit)

        val, is_common = multi_val("angle")
        ang_edit = self._edit(
            f"{float(val):.1f}" if is_common and val else "", "degrees"
        )
        if not is_common:
            ang_edit.setPlaceholderText("(mixed)")

        def on_ang():
            try:
                v = float(ang_edit.text()) % 360
            except ValueError:
                return
            apply_all("angle", round(v, 1))
            repaint_all()

        ang_edit.editingFinished.connect(on_ang)
        self._form.addRow("Angle:", ang_edit)

        val, is_common = multi_val("opacity")
        opa_edit = self._edit(
            f"{float(val):.2f}" if is_common and val else "", "0 – 1"
        )
        if not is_common:
            opa_edit.setPlaceholderText("(mixed)")

        def on_opa():
            try:
                v = max(0.0, min(1.0, float(opa_edit.text())))
            except ValueError:
                return
            apply_all("opacity", round(v, 2))
            repaint_all()

        opa_edit.editingFinished.connect(on_opa)
        self._form.addRow("Opacity:", opa_edit)

        self._form.addRow(self._sep())
        self._form.addRow("Color:", self._make_multi_color_button(items))

    def _build_multi_text_rows(self, items, multi_val, apply_all, repaint_all):
        val, is_common = multi_val("label_text")
        te = QTextEdit()
        te.setPlainText(val if is_common else "")
        if not is_common:
            te.setPlaceholderText("(mixed)")
        te.setMinimumHeight(72)
        te.setMaximumHeight(160)

        _save_timer = QTimer(te)
        _save_timer.setSingleShot(True)
        _save_timer.setInterval(300)

        def _do_save():
            apply_all("label_text", te.toPlainText())
            repaint_all()

        _save_timer.timeout.connect(_do_save)
        te.textChanged.connect(_save_timer.start)
        self._form.addRow("Label:", te)
        self._form.addRow(self._sep())

        val, is_common = multi_val("cad_layer")
        cl_edit = self._edit(val if is_common else "", "cad layer name")
        if not is_common:
            cl_edit.setPlaceholderText("(mixed)")

        def on_cl():
            v = cl_edit.text().strip()
            if v:
                apply_all("cad_layer", v)

        cl_edit.editingFinished.connect(on_cl)
        self._form.addRow("Cad Layer:", cl_edit)

    def _make_multi_color_button(self, items):
        all_colors = []
        for lyr, fid in items:
            cidx = lyr.fields().indexOf("color")
            if cidx < 0:
                continue
            val = self._attr(lyr.getFeature(fid), cidx)
            if val:
                c = QColor(str(val))
                if c.isValid():
                    all_colors.append(c.name())

        is_common  = bool(all_colors) and len(set(all_colors)) == 1
        init_color = QColor(all_colors[0]) if is_common else QColor(0, 0, 255)

        btn = QPushButton()
        if is_common:
            btn.setStyleSheet(
                f"background-color: rgb({init_color.red()},{init_color.green()},{init_color.blue()});"
                "border: 1px solid #666; border-radius: 2px; min-height: 20px;"
            )
        else:
            btn.setText("(mixed)")
            btn.setStyleSheet("border: 1px solid #666; border-radius: 2px; min-height: 20px;")

        def on_clicked():
            cur_colors = []
            for lyr, fid in items:
                cidx = lyr.fields().indexOf("color")
                if cidx < 0:
                    continue
                val = self._attr(lyr.getFeature(fid), cidx)
                if val:
                    c = QColor(str(val))
                    if c.isValid():
                        cur_colors.append(c.name())
            start = QColor(cur_colors[0]) if cur_colors and len(set(cur_colors)) == 1 else init_color
            chosen = QColorDialog.getColor(start, None, "Color")
            if not chosen.isValid():
                return
            layers_done = {}
            for lyr, fid in items:
                cidx = lyr.fields().indexOf("color")
                if cidx < 0:
                    continue
                if not lyr.isEditable():
                    lyr.startEditing()
                lyr.changeAttributeValue(fid, cidx, chosen.name())
                layers_done[lyr.id()] = lyr
            for lyr in layers_done.values():
                lyr_name = lyr.name()
                if lyr_name == "circles":
                    apply_circle_color_renderer(lyr)
                elif lyr_name == "lines":
                    apply_polyline_color_renderer(lyr)
                elif lyr_name == "points":
                    apply_point_color_renderer(lyr)
                elif lyr_name == "_hatches":
                    apply_hatch_renderer(lyr)
                elif lyr_name == "dimensions":
                    apply_dimension_style(lyr)
                lyr.triggerRepaint()
            btn.setStyleSheet(
                f"background-color: rgb({chosen.red()},{chosen.green()},{chosen.blue()});"
                "border: 1px solid #666; border-radius: 2px; min-height: 20px;"
            )
            btn.setText("")

        btn.clicked.connect(on_clicked)
        return btn

    def _make_multi_icon_picker(self, items, current_svg, svg_idx):
        if not os.path.isdir(_PLUGIN_ICONS_DIR):
            return None

        svgs = sorted(
            f for f in os.listdir(_PLUGIN_ICONS_DIR) if f.lower().endswith(".svg")
        )

        ICON_PX = 44
        COLS    = 4

        container = QWidget()
        grid      = QGridLayout(container)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(4)

        def _pick_and_refresh(path_or_basic, _idx=svg_idx):
            if _idx < 0:
                return
            layers_done = {}
            for lyr, fid in items:
                lid = lyr.id()
                if lid not in layers_done:
                    if not lyr.isEditable():
                        lyr.startEditing()
                    lyr.beginEditCommand("Set SVG")
                    layers_done[lid] = lyr
                lyr.changeAttributeValue(fid, _idx, path_or_basic)
            for lyr in layers_done.values():
                lyr.endEditCommand()
                apply_point_color_renderer(lyr)
                lyr.triggerRepaint()
            self._deferred_refresh()

        # "Basic" tile — rendered from basic.svg for visual consistency
        _basic_svg_path_m = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "map_icons", "basic.svg",
        )
        basic_pix = QPixmap(ICON_PX, ICON_PX)
        basic_pix.fill(Qt.transparent)
        _bsvg_m = QSvgRenderer(_basic_svg_path_m)
        if _bsvg_m.isValid():
            bp = QPainter(basic_pix)
            bp.setRenderHint(QPainter.Antialiasing)
            _bsvg_m.render(bp, QRectF(0, 0, ICON_PX, ICON_PX))
            bp.end()
        else:
            bp = QPainter(basic_pix)
            bp.setRenderHint(QPainter.Antialiasing)
            bp.setPen(QPen(QColor(47, 47, 47), 1.5))
            bp.setBrush(QBrush(QColor(255, 255, 255)))
            r = ICON_PX // 3
            cx2, cy2 = ICON_PX // 2, ICON_PX // 2
            bp.drawEllipse(cx2 - r, cy2 - r, 2 * r, 2 * r)
            bp.end()

        basic_btn = QPushButton()
        basic_btn.setIcon(QIcon(basic_pix))
        basic_btn.setIconSize(QSize(ICON_PX - 4, ICON_PX - 4))
        basic_btn.setFixedSize(ICON_PX + 6, ICON_PX + 6)
        basic_btn.setToolTip("basic")
        basic_btn.setFlat(True)
        basic_selected = bool(current_svg and current_svg == "basic")
        basic_btn.setStyleSheet(
            "border: 2px solid #0078d4; background: #e3f2fd; border-radius: 4px;"
            if basic_selected else
            "border: 1px solid #bbb; border-radius: 4px;"
        )
        basic_btn.clicked.connect(lambda checked=False: _pick_and_refresh("basic"))
        grid.addWidget(basic_btn, 0, 0)

        for i, fname in enumerate(svgs):
            path = os.path.join(_PLUGIN_ICONS_DIR, fname)
            pos  = i + 1

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
            btn.clicked.connect(lambda checked=False, p=path: _pick_and_refresh(p))
            grid.addWidget(btn, pos // COLS, pos % COLS)

        n_items = len(svgs) + 1
        n_rows  = (n_items + COLS - 1) // COLS
        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(min(n_rows, 3) * (ICON_PX + 10) + 8)
        scroll.setFrameShape(QFrame.StyledPanel)
        return scroll

    def _write_circle_attrs(self, cx, cy, radius):
        attrs = circle_attrs(cx, cy, radius)
        for fname, val in attrs.items():
            idx = self._layer.fields().indexOf(fname)
            if idx >= 0:
                self._layer.changeAttributeValue(self._fid, idx, val)

    # ------------------------------------------------------------------
    # Color picker
    # ------------------------------------------------------------------

    def _get_layer_color(self):
        try:
            return self._layer.renderer().symbol().color()
        except Exception:
            return QColor(0, 0, 255)

    def _get_current_color(self):
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
                if lyr_name == "circles":
                    apply_circle_color_renderer(self._layer)
                elif lyr_name == "lines":
                    apply_polyline_color_renderer(self._layer)
                elif lyr_name == "points":
                    apply_point_color_renderer(self._layer)
                elif lyr_name == "_hatches":
                    apply_hatch_renderer(self._layer)
                elif lyr_name == "dimensions":
                    apply_dimension_style(self._layer)
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

        pts              = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
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
