from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QFormLayout,
    QLabel, QCheckBox, QFrame, QScrollArea,
)
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes


class PropertiesDock(QDockWidget):
    """Right-side dock showing properties of the selected feature."""

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
    # Public API called by VertexSelector signals
    # ------------------------------------------------------------------

    def update_feature(self, layer, fid):
        self._layer = layer
        self._fid   = fid
        self._refresh()

    def clear_selection(self):
        self._layer = None
        self._fid   = None
        self._clear_form()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clear_form(self):
        while self._form.rowCount() > 0:
            self._form.removeRow(0)

    @staticmethod
    def _ro(text):
        lbl = QLabel(str(text))
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return lbl

    @staticmethod
    def _sep():
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setFrameShadow(QFrame.Sunken)
        return f

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

        # ── Feature identity ──────────────────────────────────────────
        self._form.addRow("Layer:", self._ro(lyr_name))
        self._form.addRow("FID:",   self._ro(str(self._fid)))

        self._form.addRow(self._sep())

        # ── Geometry properties ───────────────────────────────────────
        if lyr_name == "_polylines" and not geom.isEmpty():
            pts       = geom.asPolyline()
            is_closed = self._is_closed(pts)

            self._form.addRow("Length:",   self._ro(f"{geom.length():.3f} m"))
            self._form.addRow("Vertices:", self._ro(str(len(pts))))

            # Create a fresh checkbox each refresh so _clear_form() can safely
            # delete the old one without breaking a shared reference.
            closed_check = QCheckBox()
            closed_check.setChecked(is_closed)           # set before connecting
            closed_check.stateChanged.connect(self._on_closed_toggled)
            self._form.addRow("Closed:", closed_check)

            if is_closed:
                area_sqm   = QgsGeometry.fromPolygonXY([list(pts)]).area()
                area_acres = area_sqm * 0.000247105
                self._form.addRow("Area (m²):", self._ro(f"{area_sqm:.3f}"))
                self._form.addRow("Area (ac):", self._ro(f"{area_acres:.6f}"))

        elif lyr_name == "_circles" and not geom.isEmpty():
            radius_idx = self._layer.fields().indexOf("radius")
            if radius_idx >= 0:
                radius = feat.attribute(radius_idx)
                self._form.addRow("Radius:", self._ro(f"{radius:.3f} m" if radius else "—"))

        elif not geom.isEmpty():
            self._form.addRow("Type:", self._ro(QgsWkbTypes.displayString(geom.wkbType())))

        # ── All stored attributes ─────────────────────────────────────
        fields = self._layer.fields()
        if fields.count() > 0:
            self._form.addRow(self._sep())

            sec = QLabel("Attributes")
            sec.setStyleSheet("font-weight: bold; font-size: 8pt; color: #888;")
            self._form.addRow(sec)

            for i in range(fields.count()):
                name = fields.at(i).name()
                val  = feat.attribute(i)
                self._form.addRow(f"{name}:", self._ro("" if val is None else str(val)))

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
                # Need at least 3 unique vertices — revert silently
                self._updating = True
                cb = self.sender()
                if cb:
                    cb.setChecked(True)
                self._updating = False
                return

        self._layer.triggerRepaint()
        self._refresh()

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

    # ------------------------------------------------------------------
    # Called externally to refresh after geometry edits
    # ------------------------------------------------------------------

    def refresh_if_current(self, layer, fid):
        if self._layer is layer and self._fid == fid:
            self._refresh()
