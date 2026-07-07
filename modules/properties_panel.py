import math

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QColorDialog, QDockWidget, QFormLayout, QFrame,
    QLabel, QCheckBox, QLineEdit, QPushButton,
    QScrollArea, QWidget, QVBoxLayout,
)
from qgis.core import (
    QgsCircularString, QgsGeometry, QgsPoint, QgsPointXY, QgsWkbTypes,
)
from .layer_utils import circle_attrs


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
        self._form = QFormLayout(self._content)
        self._form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._form.setSpacing(5)
        self._form.setContentsMargins(0, 4, 0, 4)

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
            self._build_polyline_rows(geom)
        elif lyr_name == "_circles" and not geom.isEmpty():
            self._build_circle_rows(feat, geom)
        elif lyr_name == "_points" and not geom.isEmpty():
            self._build_point_rows(geom)
        elif not geom.isEmpty():
            self._form.addRow("Type:", self._ro(QgsWkbTypes.displayString(geom.wkbType())))

    # ------------------------------------------------------------------
    # Per-type row builders
    # ------------------------------------------------------------------

    def _build_polyline_rows(self, geom):
        pts       = geom.asPolyline()
        is_closed = self._is_closed(pts)
        area_sqm  = QgsGeometry.fromPolygonXY([list(pts)]).area() if is_closed else 0.0
        area_ac   = area_sqm * 0.000247105

        self._form.addRow("Length:",   self._ro(f"{geom.length():.3f} m"))
        self._form.addRow("Vertices:", self._ro(str(len(pts))))

        # Fresh checkbox each refresh (safe to delete via _clear_form later)
        closed_check = QCheckBox()
        closed_check.setChecked(is_closed)
        closed_check.stateChanged.connect(self._on_closed_toggled)
        self._form.addRow("Closed:", closed_check)

        # Area always shown — 0 when open
        self._form.addRow("Area (m²):", self._ro(f"{area_sqm:.3f}"))
        self._form.addRow("Area (ac):", self._ro(f"{area_ac:.6f}"))

        # Color picker — changes the layer's symbol colour
        self._form.addRow("Color:", self._make_color_button())

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

    def _build_point_rows(self, geom):
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
        self._form.addRow("Color:", self._make_color_button())

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
                try:
                    self._layer.renderer().symbol().setColor(chosen)
                    self._layer.triggerRepaint()
                except Exception:
                    pass
                color_idx = self._layer.fields().indexOf("color")
                if color_idx >= 0 and self._fid is not None:
                    if not self._layer.isEditable():
                        self._layer.startEditing()
                    self._layer.changeAttributeValue(self._fid, color_idx, chosen.name())
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
