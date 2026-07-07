from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsWkbTypes,
    QgsLineSymbol,
    QgsSingleSymbolRenderer,
    QgsCoordinateReferenceSystem,
)
from qgis.PyQt.QtWidgets import QGraphicsTextItem
from PyQt5.QtCore import QVariant, QPoint
from PyQt5.QtWidgets import QLabel
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtGui import QFont, QColor
from .snap_config import snapSettingConfig
from .dynamic_input import DynamicInput
from .layer_utils import add_to_plugin_group, open_layer_from_gpkg, create_layer_in_gpkg
from . import snap_utils
import math
from . import crs_utils


LAYER_NAME = "_polylines"
LAYER_COLOR = "#E05C00"
RUBBER_BAND_COLOR = QColor(224, 92, 0)
SNAP_MARKER_COLOR = QColor(66, 135, 245)
TEXT_COLOR = QColor(180, 70, 0)
LABEL_FONT = QFont("Arial", 8)
LABEL_COLOR = QColor("#393939")
LABEL_SIZE = 10

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


class PolylineDrawer(QgsMapTool):
    """
    A QGIS map tool for drawing polylines using polar input (distance + bearing),
    following AutoCAD's dynamic input model.

    - Click on map to add the first point, then each subsequent point.
    - After the first point the dynamic input box appears near the cursor:
        Distance field  →  Tab  →  Bearing field  →  Enter to commit
    - Leaving a field empty uses the live cursor value shown in its placeholder.
    - Terminal also accepts:  'distance'  or  'distance,bearing'
    - Right-click, double-click, or Enter with empty input finishes the polyline.
    - Backspace removes the last vertex. Escape cancels without saving.
    """

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas
        self.terminal_dock = terminal_dock

        # State
        self.points = []         # list of QgsPointXY
        self.is_drawing = False  # True after the first point is placed

        # CRS
        self.appropriate_crs = crs_utils.get_canvas_epsg(self.canvas)
        self._apply_project_crs()

        # Layer
        self.polyline_layer = self._get_or_create_polyline_layer()

        # Snap settings
        snapSettingConfig()

        # UI elements
        self.snap_marker    = self._create_snap_marker()
        self.committed_band = self._create_rubber_band(Qt.SolidLine)
        self.preview_band   = self._create_rubber_band(Qt.DashLine)
        self.segment_text   = self._create_text_item()
        self._maptool = None   # set by UgsurvMaptool.set_tool()

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        # Floating dynamic input (Distance + Bearing, Tab cycles between them)
        self._dinput = DynamicInput(canvas, terminal_dock, [
            {"key": "dist",    "label": "Distance"},
            {"key": "bearing", "label": "Bearing (°)"},
        ])
        self._dinput.on_cancel = self.deactivate

    # -------------------------------------------------------------------------
    # Setup helpers
    # -------------------------------------------------------------------------

    def _apply_project_crs(self):
        crs = QgsCoordinateReferenceSystem(f"EPSG:{self.appropriate_crs}")
        QgsProject.instance().setCrs(crs)

    def _create_snap_marker(self):
        marker = QgsVertexMarker(self.canvas)
        marker.setColor(SNAP_MARKER_COLOR)
        marker.setIconSize(10)
        marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        marker.setPenWidth(2)
        marker.setVisible(False)
        return marker

    def _create_rubber_band(self, line_style):
        band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        band.setColor(RUBBER_BAND_COLOR)
        band.setWidth(2)
        band.setLineStyle(line_style)
        return band

    def _create_text_item(self):
        item = QGraphicsTextItem("")
        item.setDefaultTextColor(TEXT_COLOR)
        self.canvas.scene().addItem(item)
        return item

    # -------------------------------------------------------------------------
    # Layer management
    # -------------------------------------------------------------------------

    def _get_or_create_polyline_layer(self):
        existing = QgsProject.instance().mapLayersByName(LAYER_NAME)
        if existing:
            layer = existing[0]
            if not layer.isEditable():
                layer.startEditing()
            self._ensure_fields(layer)
            return layer
        layer = open_layer_from_gpkg(LAYER_NAME)
        if layer:
            self._ensure_fields(layer)
            self._apply_polyline_style(layer)
            add_to_plugin_group(layer)
            layer.startEditing()
            return layer
        return self._create_polyline_layer()

    def _ensure_fields(self, layer):
        """Add any fields that are missing from an existing layer."""
        existing = {f.name() for f in layer.fields()}
        to_add = []
        if "closed"     not in existing: to_add.append(QgsField("closed",     QVariant.Bool))
        if "area_sqm"   not in existing: to_add.append(QgsField("area_sqm",   QVariant.Double))
        if "area_acres" not in existing: to_add.append(QgsField("area_acres", QVariant.Double))
        if to_add:
            layer.dataProvider().addAttributes(to_add)
            layer.updateFields()

    def _apply_polyline_style(self, layer):
        symbol = QgsLineSymbol.createSimple({
            "color": LAYER_COLOR,
            "width": "0.4",
            "line_style": "solid",
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

    def _create_polyline_layer(self):
        mem = QgsVectorLayer(
            f"LineString?crs=EPSG:{self.appropriate_crs}",
            LAYER_NAME,
            "memory"
        )
        mem.dataProvider().addAttributes([
            QgsField("length",     QVariant.Double),
            QgsField("closed",     QVariant.Bool),
            QgsField("area_sqm",   QVariant.Double),
            QgsField("area_acres", QVariant.Double),
        ])
        mem.updateFields()

        layer = create_layer_in_gpkg(mem)
        self._apply_polyline_style(layer)
        add_to_plugin_group(layer)
        layer.startEditing()
        return layer

    # -------------------------------------------------------------------------
    # Geometry
    # -------------------------------------------------------------------------

    def _commit_polyline(self):
        if len(self.points) < 2:
            self._log("\nNeed at least 2 points — polyline not saved")
            return
        geometry = QgsGeometry.fromPolylineXY(self.points)
        length = geometry.length()

        p0, pn = self.points[0], self.points[-1]
        is_closed = (len(self.points) >= 4 and
                     abs(p0.x() - pn.x()) < 1e-9 and
                     abs(p0.y() - pn.y()) < 1e-9)
        if is_closed:
            poly_geom  = QgsGeometry.fromPolygonXY([self.points])
            area_sqm   = poly_geom.area()
            area_acres = area_sqm * 0.000247105
        else:
            area_sqm = area_acres = 0.0

        feature = QgsFeature(self.polyline_layer.fields())
        feature.setGeometry(geometry)
        feature.setAttribute("length",     round(length, 3))
        feature.setAttribute("closed",     is_closed)
        feature.setAttribute("area_sqm",   round(area_sqm, 3))
        feature.setAttribute("area_acres", round(area_acres, 6))

        self.polyline_layer.addFeature(feature)
        self.polyline_layer.updateExtents()
        self.polyline_layer.triggerRepaint()

        if is_closed:
            self._log(
                f"\nPolyline saved — {len(self.points)} vertices, length: {length:.3f}"
                f", area: {area_sqm:.3f} sqm ({area_acres:.4f} acres)"
            )
        else:
            self._log(f"\nPolyline saved — {len(self.points)} vertices, length: {length:.3f}")

    # -------------------------------------------------------------------------
    # Polar helpers
    # -------------------------------------------------------------------------

    def _bearing(self, p1: QgsPointXY, p2: QgsPointXY) -> float:
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        return math.degrees(math.atan2(dx, dy)) % 360

    def _polar_to_point(self, origin: QgsPointXY, distance: float, bearing_deg: float) -> QgsPointXY:
        rad = math.radians(bearing_deg)
        return QgsPointXY(
            origin.x() + distance * math.sin(rad),
            origin.y() + distance * math.cos(rad),
        )

    # -------------------------------------------------------------------------
    # Preview helpers
    # -------------------------------------------------------------------------

    def _redraw_committed_segments(self):
        self.committed_band.reset(QgsWkbTypes.LineGeometry)
        for pt in self.points:
            self.committed_band.addPoint(pt)

    def _draw_preview_segment(self, cursor: QgsPointXY):
        last = self.points[-1]
        self.preview_band.reset(QgsWkbTypes.LineGeometry)
        self.preview_band.addPoint(last)
        self.preview_band.addPoint(cursor)

        dist = last.distance(cursor)
        bearing = self._bearing(last, cursor)
        self._update_segment_label(last, cursor, dist)
        return dist, bearing

    def _update_segment_label(self, p1, p2, dist):
        mid = QgsPointXY((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)
        dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
        angle = -math.degrees(math.atan2(dy, dx))
        if angle < -90:
            angle += 180
        elif angle > 90:
            angle -= 180
        cp = self.canvas.getCoordinateTransform().transform(mid)
        self.segment_text.setPos(cp.x(), cp.y())
        self.segment_text.setPlainText(f"{dist:.3f}")
        self.segment_text.setRotation(angle)

    def _show_hint(self, screen_pos, text):
        if not text:
            self._hint.hide()
            return
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

    def _clear_preview(self):
        self.committed_band.reset(QgsWkbTypes.LineGeometry)
        self.preview_band.reset(QgsWkbTypes.LineGeometry)
        self.segment_text.setPlainText("")
        self.snap_marker.setVisible(False)

    # -------------------------------------------------------------------------
    # Input handling
    # -------------------------------------------------------------------------

    def _on_polar_committed(self, values: dict):
        """Called when user presses Enter / Space in the floating input widget."""
        # DynamicInput already hid itself; clear terminal handler
        self.terminal_dock.clear_input_handler()
        dist_text  = values["dist"]
        angle_text = values["bearing"]
        if not dist_text.strip():
            self._finish()
            return
        if dist_text.strip().lower() == 'c':
            self._close()
            return
        self._process_polar_input(dist_text, angle_text)

    def _on_terminal_input(self, text: str):
        """Called when user types in the terminal and presses Enter."""
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        text = text.strip()
        if not text:
            self._finish()
            return
        if text.lower() == 'c':
            self._close()
            return
        parts = text.replace(',', ' ').split()
        if len(parts) == 1:
            bearing_text = self._dinput._lines["bearing"].placeholderText() or "0"
            self._process_polar_input(parts[0], bearing_text)
        elif len(parts) >= 2:
            self._process_polar_input(parts[0], parts[1])
        else:
            self._log(f"\nInvalid input '{text}' — use: distance  or  distance,bearing")
            self._re_request_input()

    def _process_polar_input(self, dist_text, angle_text):
        """Calculate the next vertex from distance + bearing and add it."""
        try:
            distance = float(dist_text)
            bearing  = float(angle_text)
            if distance <= 0:
                raise ValueError
        except ValueError:
            self._log(f"\nInvalid input — enter distance [Tab] bearing (°)")
            self._re_request_input()
            return

        point = self._polar_to_point(self.points[-1], distance, bearing)
        self._add_point(point)
        self._log(f"  →  dist: {distance:.3f}  bearing: {bearing:.2f}°")

    def _add_point(self, point: QgsPointXY):
        self.points.append(point)
        self.is_drawing = True
        self._log(f"\nPoint {len(self.points)}: {point.x():.3f}, {point.y():.3f}")
        self._redraw_committed_segments()

        self.terminal_dock.request_input("dist,bearing: ", self._on_terminal_input)
        self._dinput.on_commit = self._on_polar_committed
        cp = self.canvas.getCoordinateTransform().transform(point)
        self._dinput.show(cp.x(), cp.y())

    def _re_request_input(self):
        if self.points:
            self.terminal_dock.request_input("dist,bearing: ", self._on_terminal_input)
            self._dinput.on_commit = self._on_polar_committed
            cp = self.canvas.getCoordinateTransform().transform(self.points[-1])
            self._dinput.show(cp.x(), cp.y())

    def _close(self):
        """Close the polyline by connecting the last point back to the first."""
        if len(self.points) < 2:
            self._log("\nNeed at least 2 points to close")
            self._re_request_input()
            return
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self.points.append(QgsPointXY(self.points[0].x(), self.points[0].y()))
        self._log(f"\nClosed → {self.points[0].x():.3f}, {self.points[0].y():.3f}")
        self._commit_polyline()
        self._clear_preview()
        self.points = []
        self.is_drawing = False
        self._display("\nClick first point…\n")

    def _finish(self):
        """Commit the current polyline and reset for the next one."""
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._commit_polyline()
        self._clear_preview()
        self.points = []
        self.is_drawing = False
        self._display("\nClick first point…\n")

    # -------------------------------------------------------------------------
    # Terminal helpers
    # -------------------------------------------------------------------------

    def _display(self, message):
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + message
        )

    def _log(self, message):
        self.terminal_dock.commandOutputText += message
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    # -------------------------------------------------------------------------
    # QgsMapTool overrides
    # -------------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()
        self._display("\nClick first point…\n")

    def deactivate(self):
        self.terminal_dock.clear_input_handler()
        self._dinput.destroy()

        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            self.terminal_dock.command.setFocus()

        self.polyline_layer.updateExtents()

        self._clear_preview()
        self._hint.hide()
        self.canvas.scene().removeItem(self.segment_text)

        self.points = []
        self.is_drawing = False

        self._display("\n........\n")
        super().deactivate()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._finish()
        elif key == Qt.Key_BackSpace and self.points:
            removed = self.points.pop()
            self._log(f"\nUndo — removed: {removed.x():.3f}, {removed.y():.3f}")
            if self.points:
                self._redraw_committed_segments()
            else:
                self._clear_preview()
                self.is_drawing = False

    def canvasMoveEvent(self, event):
        raw_point = self.toMapCoordinates(event.pos())
        cursor, icon = snap_utils.snap_point(self.canvas, raw_point)
        if icon is not None:
            self.snap_marker.setCenter(cursor)
            self.snap_marker.setIconType(icon)
            self.snap_marker.setVisible(True)
        else:
            self.snap_marker.setVisible(False)

        if not self.is_drawing:
            self._display(f"\nFirst point: {cursor.x():.3f}, {cursor.y():.3f}\n")
            self._show_hint(event.pos(), "Click first point")
        else:
            dist, bearing = self._draw_preview_segment(cursor)
            self._display(f"\nDist: {dist:.3f}   Bearing: {bearing:.2f}°\n")
            # Move the floating input near cursor; update both placeholders live
            cp = self.canvas.getCoordinateTransform().transform(cursor)
            self._dinput.update(cp.x(), cp.y(), {
                "dist":    f"{dist:.3f}",
                "bearing": f"{bearing:.2f}",
            })
            self._show_hint(event.pos(), "Click next point  or  type dist,bearing + Enter")

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._finish()
            return

        if event.button() != Qt.LeftButton:
            return

        raw_point = self.toMapCoordinates(event.pos())
        clicked_point, _ = snap_utils.snap_point(self.canvas, raw_point)
        self.snap_marker.setVisible(False)

        self.polyline_layer = self._get_or_create_polyline_layer()
        self._add_point(clicked_point)
        self.terminal_dock.command.setFocus()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._finish()
        self.terminal_dock.command.setFocus()
