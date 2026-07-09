"""
HATCH tool — fill a closed polyline or circle with a configurable hatch pattern.

Workflow
────────
1. Hover over a polyline or circle → yellow fill preview appears.
2. Click to add a hatch fill with default settings.
3. Select the new hatch feature to edit its properties in the Properties panel.
4. RMB / Esc → exit.
"""

import math

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor
from PyQt5.QtCore import QVariant

from . import crs_utils
from .layer_utils import (
    add_to_plugin_group,
    apply_hatch_renderer,
    create_layer_in_gpkg,
    enable_feature_render_order,
    open_layer_from_gpkg,
)

_LAYER_NAME = "_hatches"

_DEFAULT_PATTERN = "lines"
_DEFAULT_SIZE    = 1.0
_DEFAULT_COLOR   = "#E05C00"
_DEFAULT_ANGLE   = 45.0
_DEFAULT_OPACITY = 0.7

_C_HOVER = QColor(255, 200, 0, 80)

_HINT_STYLE = (
    "QLabel {"
    "  background-color: rgba(20, 20, 20, 210);"
    "  color: #f0f0f0;"
    "  border: 1px solid rgba(255, 255, 255, 80);"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "  font-size: 9pt;"
    "}"
)

_HIT_PX = 10


class HatchTool(QgsMapTool):
    """Click a polyline or circle feature to cover it with a hatch fill polygon."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None
        self._hatch_layer  = None

        self._hover_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self._hover_band.setColor(QColor(255, 200, 0, 180))
        self._hover_band.setFillColor(_C_HOVER)
        self._hover_band.setWidth(2)
        self._hover_band.setVisible(False)

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

    # ------------------------------------------------------------------
    # Layer
    # ------------------------------------------------------------------

    def _ensure_hatch_fields(self, lyr):
        existing = {f.name() for f in lyr.fields()}
        to_add = []
        if "fill_pattern" not in existing: to_add.append(QgsField("fill_pattern", QVariant.String))
        if "element_size" not in existing: to_add.append(QgsField("element_size", QVariant.Double))
        if "color"        not in existing: to_add.append(QgsField("color",        QVariant.String))
        if "angle"        not in existing: to_add.append(QgsField("angle",        QVariant.Double))
        if "opacity"      not in existing: to_add.append(QgsField("opacity",      QVariant.Double))
        if "z_index"      not in existing: to_add.append(QgsField("z_index",      QVariant.Int))
        if to_add:
            lyr.dataProvider().addAttributes(to_add)
            lyr.updateFields()

    def _get_or_create_layer(self):
        existing = QgsProject.instance().mapLayersByName(_LAYER_NAME)
        if existing:
            lyr = existing[0]
            if not lyr.isEditable():
                lyr.startEditing()
            self._ensure_hatch_fields(lyr)
            apply_hatch_renderer(lyr)
            enable_feature_render_order(lyr)
            return lyr

        lyr = open_layer_from_gpkg(_LAYER_NAME)
        if lyr and lyr.isValid():
            self._ensure_hatch_fields(lyr)
            add_to_plugin_group(lyr)
            apply_hatch_renderer(lyr)
            enable_feature_render_order(lyr)
            lyr.startEditing()
            return lyr

        epsg = crs_utils.get_canvas_epsg(self.canvas)
        mem  = QgsVectorLayer(f"Polygon?crs=EPSG:{epsg}", _LAYER_NAME, "memory")
        mem.dataProvider().addAttributes([
            QgsField("fill_pattern", QVariant.String),
            QgsField("element_size", QVariant.Double),
            QgsField("color",        QVariant.String),
            QgsField("angle",        QVariant.Double),
            QgsField("opacity",      QVariant.Double),
            QgsField("z_index",      QVariant.Int),
        ])
        mem.updateFields()

        lyr = create_layer_in_gpkg(mem)
        add_to_plugin_group(lyr)
        apply_hatch_renderer(lyr)
        enable_feature_render_order(lyr)
        lyr.startEditing()
        return lyr

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _hit_tol(self):
        return _HIT_PX * self.canvas.mapUnitsPerPixel()

    def _log(self, msg):
        self.terminal_dock.commandOutputText += msg
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    def _show_hint(self, screen_pos):
        text = "Click a polyline or circle to add hatch fill  (Esc = exit)"
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self.canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self.canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    def _rm(self, item):
        try:
            self.canvas.scene().removeItem(item)
        except Exception:
            pass

    def _source_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.isSpatial()
            and lyr.name() in ("_polylines", "_circles")
        ]

    def _find_near(self, map_pt):
        tol  = self._hit_tol() * 3
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        cg = QgsGeometry.fromPointXY(map_pt)
        best_layer, best_feat, best_d = None, None, float("inf")
        for lyr in self._source_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                d = feat.geometry().distance(cg)
                if d < best_d:
                    best_d, best_layer, best_feat = d, lyr, feat
        return (best_layer, best_feat) if best_d <= tol else (None, None)

    @staticmethod
    def _to_polygon(layer_name, geom):
        """Return a QgsGeometry polygon from a circle or polyline geometry."""
        if layer_name == "_circles":
            poly = geom.convertToType(QgsWkbTypes.PolygonGeometry, False)
            if poly and not poly.isEmpty():
                return poly
            # Fallback: bounding-box circle approximation
            bb = geom.boundingBox()
            cx, cy = bb.center().x(), bb.center().y()
            r  = bb.width() / 2.0
            pts = [
                QgsPointXY(cx + r * math.cos(math.radians(a)),
                           cy + r * math.sin(math.radians(a)))
                for a in range(0, 361, 10)
            ]
            return QgsGeometry.fromPolygonXY([pts])

        # _polylines
        pts = geom.asPolyline()
        if len(pts) < 3:
            return None
        ring = list(pts)
        if abs(ring[0].x() - ring[-1].x()) > 1e-9 or abs(ring[0].y() - ring[-1].y()) > 1e-9:
            ring.append(ring[0])   # close the ring
        return QgsGeometry.fromPolygonXY([ring])

    # ------------------------------------------------------------------
    # Apply hatch
    # ------------------------------------------------------------------

    def _apply_hatch(self, src_layer, src_feat):
        poly_geom = self._to_polygon(src_layer.name(), src_feat.geometry())
        if poly_geom is None or poly_geom.isEmpty():
            self._log("\nCould not form a polygon from that feature (need ≥ 3 points)")
            return

        lyr  = self._hatch_layer
        feat = QgsFeature(lyr.fields())
        feat.setGeometry(poly_geom)

        def _set(name, val):
            idx = lyr.fields().indexOf(name)
            if idx >= 0:
                feat.setAttribute(idx, val)

        _set("fill_pattern", _DEFAULT_PATTERN)
        _set("element_size", _DEFAULT_SIZE)
        _set("color",        _DEFAULT_COLOR)
        _set("angle",        _DEFAULT_ANGLE)
        _set("opacity",      _DEFAULT_OPACITY)
        _set("z_index",      1)

        lyr.addFeature(feat)
        lyr.updateExtents()
        lyr.triggerRepaint()
        self._log(
            f"\nHatch added  (pattern={_DEFAULT_PATTERN}, size={_DEFAULT_SIZE}, "
            f"angle={_DEFAULT_ANGLE}°)  —  select it to edit in Properties"
        )

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self._hatch_layer = self._get_or_create_layer()
        self.canvas.setFocus()
        self._log(
            "\nHATCH  ──  click a polyline or circle to add a hatch fill"
            "\n  Edit pattern, size, color and angle in the Properties panel"
            "\n  RMB / Esc → exit\n"
        )

    def deactivate(self):
        self._rm(self._hover_band)
        self._hint.hide()
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        lyr, feat = self._find_near(map_pt)
        if feat is not None:
            poly_geom = self._to_polygon(lyr.name(), feat.geometry())
            if poly_geom and not poly_geom.isEmpty():
                self._hover_band.setToGeometry(poly_geom, None)
                self._hover_band.setVisible(True)
            else:
                self._hover_band.setVisible(False)
        else:
            self._hover_band.setVisible(False)
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            return
        if event.button() != Qt.LeftButton:
            return

        map_pt = self.toMapCoordinates(event.pos())
        lyr, feat = self._find_near(map_pt)
        if feat is None:
            self._log("\nNo polyline or circle found near click")
            return

        self._apply_hatch(lyr, feat)
        self.canvas.setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.deactivate()
