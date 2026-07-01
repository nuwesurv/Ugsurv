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
    QgsPointLocator,
)
from qgis.PyQt.QtWidgets import QGraphicsTextItem
from PyQt5.QtCore import QVariant, QEvent
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QGraphicsProxyWidget
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtGui import QFont, QColor
from .snapSettingConfig import snapSettingConfig
import math
from . import get_appropriate_crs_str


LAYER_NAME = "polylines"
LAYER_COLOR = "#E05C00"
RUBBER_BAND_COLOR = QColor(224, 92, 0)
SNAP_MARKER_COLOR = QColor(224, 92, 0)
TEXT_COLOR = QColor(180, 70, 0)
LABEL_FONT = QFont("Arial", 8)
LABEL_COLOR = QColor("#393939")
LABEL_SIZE = 10


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
        self.appropriate_crs = get_appropriate_crs_str.get_canvas_epsg(self.canvas)
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
        self._syncing = False
        self.dynamic_input_proxy = self._create_dynamic_input()
        self._maptool = None   # set by UgsurvMaptool.set_tool()

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
            return layer
        return self._create_polyline_layer()

    def _create_polyline_layer(self):
        layer = QgsVectorLayer(
            f"LineString?crs=EPSG:{self.appropriate_crs}",
            LAYER_NAME,
            "memory"
        )
        provider = layer.dataProvider()
        provider.addAttributes([QgsField("length", QVariant.Double)])
        layer.updateFields()

        symbol = QgsLineSymbol.createSimple({
            "color": LAYER_COLOR,
            "width": "0.4",
            "line_style": "solid",
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        QgsProject.instance().addMapLayer(layer)
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

        feature = QgsFeature(self.polyline_layer.fields())
        feature.setGeometry(geometry)
        feature.setAttribute("length", round(length, 3))

        self.polyline_layer.addFeature(feature)
        self.polyline_layer.updateExtents()
        self.polyline_layer.triggerRepaint()

        self._log(f"\nPolyline saved — {len(self.points)} vertices, length: {length:.3f}")

    # -------------------------------------------------------------------------
    # Polar helpers
    # -------------------------------------------------------------------------

    def _bearing(self, p1: QgsPointXY, p2: QgsPointXY) -> float:
        """Azimuth from p1 to p2 in degrees (North = 0°, clockwise)."""
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        return math.degrees(math.atan2(dx, dy)) % 360

    def _polar_to_point(self, origin: QgsPointXY, distance: float, bearing_deg: float) -> QgsPointXY:
        """Return the point reached from origin by traveling distance at bearing_deg."""
        rad = math.radians(bearing_deg)
        return QgsPointXY(
            origin.x() + distance * math.sin(rad),
            origin.y() + distance * math.cos(rad),
        )

    # -------------------------------------------------------------------------
    # Preview helpers
    # -------------------------------------------------------------------------

    def _update_snap_marker(self, point, snap_result):
        if snap_result.isValid():
            self.snap_marker.setCenter(snap_result.point())
            self.snap_marker.setVisible(True)
            icon_map = {
                QgsPointLocator.Vertex:          QgsVertexMarker.ICON_CIRCLE,
                QgsPointLocator.Edge:            QgsVertexMarker.ICON_DOUBLE_TRIANGLE,
                QgsPointLocator.Area:            QgsVertexMarker.ICON_RHOMBUS,
                QgsPointLocator.MiddleOfSegment: QgsVertexMarker.ICON_TRIANGLE,
            }
            self.snap_marker.setIconType(icon_map.get(snap_result.type(), QgsVertexMarker.ICON_X))
        else:
            self.snap_marker.setVisible(False)

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

    def _clear_preview(self):
        self.committed_band.reset(QgsWkbTypes.LineGeometry)
        self.preview_band.reset(QgsWkbTypes.LineGeometry)
        self.segment_text.setPlainText("")
        self.snap_marker.setVisible(False)

    # -------------------------------------------------------------------------
    # Dynamic input box  (two-field AutoCAD-style: Distance | Bearing)
    # -------------------------------------------------------------------------

    def _create_dynamic_input(self):
        container = QWidget()
        container.setStyleSheet(
            "QWidget  { background: #1a1a2e; border: 1px solid #4a9eff; }"
            "QLabel   { color: #7fa8d4; font-size: 9px; border: none;"
            "           padding: 2px 6px 1px 6px; }"
            "QLineEdit { background: transparent; color: #e8e8e8; border: none;"
            "            border-top: 1px solid #2a2a4e; padding: 3px 6px;"
            "            font-size: 11px; min-width: 120px; }"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._dist_label  = QLabel("Distance")
        self._dist_line   = QLineEdit()
        self._dist_line.returnPressed.connect(self._on_dynamic_input_enter)
        self._dist_line.installEventFilter(self)

        self._angle_label = QLabel("Bearing (°)")
        self._angle_line  = QLineEdit()
        self._angle_line.returnPressed.connect(self._on_dynamic_input_enter)
        self._angle_line.installEventFilter(self)

        layout.addWidget(self._dist_label)
        layout.addWidget(self._dist_line)
        layout.addWidget(self._angle_label)
        layout.addWidget(self._angle_line)
        container.adjustSize()

        proxy = QGraphicsProxyWidget()
        proxy.setWidget(container)
        proxy.setZValue(100)
        proxy.setVisible(False)
        self.canvas.scene().addItem(proxy)
        return proxy

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.deactivate()
                return True
            if event.key() == Qt.Key_Tab:
                # Tab / Shift+Tab cycles between fields
                if event.modifiers() & Qt.ShiftModifier:
                    if obj is self._angle_line:
                        self._dist_line.setFocus()
                    elif obj is self._dist_line:
                        self._angle_line.setFocus()
                else:
                    if obj is self._dist_line:
                        self._angle_line.setFocus()
                    elif obj is self._angle_line:
                        self._dist_line.setFocus()
                return True
            if event.key() in (Qt.Key_Down, Qt.Key_Right):
                if obj is self._dist_line:
                    self._angle_line.setFocus()
                    return True
            if event.key() in (Qt.Key_Up, Qt.Key_Left):
                if obj is self._angle_line:
                    self._dist_line.setFocus()
                    return True
            if event.key() == Qt.Key_Space:
                self._on_dynamic_input_enter()
                return True
        return super().eventFilter(obj, event)

    def _show_dynamic_input(self, canvas_x, canvas_y):
        self._dist_line.clear()
        self._angle_line.clear()
        self.dynamic_input_proxy.setPos(canvas_x + 15, canvas_y + 15)
        self.dynamic_input_proxy.setVisible(True)
        self.terminal_dock.command.textChanged.connect(self._sync_terminal_to_dynamic)
        self._dist_line.textChanged.connect(self._sync_dynamic_to_terminal)
        self._angle_line.textChanged.connect(self._sync_dynamic_to_terminal)
        self._dist_line.setFocus()

    def _hide_dynamic_input(self):
        self.dynamic_input_proxy.setVisible(False)
        try:
            self.terminal_dock.command.textChanged.disconnect(self._sync_terminal_to_dynamic)
            self._dist_line.textChanged.disconnect(self._sync_dynamic_to_terminal)
            self._angle_line.textChanged.disconnect(self._sync_dynamic_to_terminal)
        except Exception:
            pass
        self._dist_line.clear()
        self._angle_line.clear()

    def _sync_terminal_to_dynamic(self, text):
        if self._syncing:
            return
        self._syncing = True
        if ',' in text:
            idx = text.index(',')
            self._dist_line.setText(text[:idx])
            self._angle_line.setText(text[idx + 1:])
        else:
            self._dist_line.setText(text)
            self._angle_line.clear()
        self._syncing = False

    def _sync_dynamic_to_terminal(self, _text=None):
        if self._syncing:
            return
        self._syncing = True
        dist = self._dist_line.text()
        angle = self._angle_line.text()
        self.terminal_dock.command.setText(f"{dist},{angle}" if angle else dist)
        self._syncing = False

    def _on_dynamic_input_enter(self):
        dist_text  = self._dist_line.text()  or self._dist_line.placeholderText()
        angle_text = self._angle_line.text() or self._angle_line.placeholderText()
        self._hide_dynamic_input()
        if dist_text.strip().lower() == 'c':
            self._close()
            return
        self._process_polar_input(dist_text, angle_text)

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
    # Input handling
    # -------------------------------------------------------------------------

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

    def _on_terminal_input(self, text):
        """Terminal accepts 'distance', 'distance,bearing', or 'c' to close."""
        text = text.strip()
        if not text:
            self._finish()
            return
        if text.lower() == 'c':
            self._close()
            return
        parts = text.replace(',', ' ').split()
        if len(parts) == 1:
            dist_text  = parts[0]
            angle_text = self._angle_line.placeholderText() or "0"
            self._process_polar_input(dist_text, angle_text)
        elif len(parts) == 2:
            self._process_polar_input(parts[0], parts[1])
        else:
            self._log(f"\nInvalid input '{text}' — use: distance  or  distance,bearing")
            self._re_request_input()

    def _add_point(self, point: QgsPointXY):
        self.points.append(point)
        self.is_drawing = True
        self._log(f"\nPoint {len(self.points)}: {point.x():.3f}, {point.y():.3f}")
        self._redraw_committed_segments()

        cp = self.canvas.getCoordinateTransform().transform(point)
        self._show_dynamic_input(cp.x(), cp.y())
        self.terminal_dock.request_input("dist,bearing: ", self._on_terminal_input)

    def _re_request_input(self):
        if self.points:
            cp = self.canvas.getCoordinateTransform().transform(self.points[-1])
            self._show_dynamic_input(cp.x(), cp.y())
        self.terminal_dock.request_input("dist,bearing: ", self._on_terminal_input)

    def _close(self):
        """Close the polyline by connecting the last point back to the first."""
        if len(self.points) < 2:
            self._log("\nNeed at least 2 points to close")
            self._re_request_input()
            return
        self._hide_dynamic_input()
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
        self._hide_dynamic_input()
        self.terminal_dock.clear_input_handler()
        self._commit_polyline()
        self._clear_preview()
        self.points = []
        self.is_drawing = False
        self._display("\nClick first point…\n")

    # -------------------------------------------------------------------------
    # QgsMapTool overrides
    # -------------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._display("\nClick first point…\n")

    def deactivate(self):
        self.terminal_dock.clear_input_handler()
        self._hide_dynamic_input()
        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            self.terminal_dock.command.setFocus()

        self.polyline_layer.updateExtents()
        self.polyline_layer.commitChanges()

        self._clear_preview()
        self.canvas.scene().removeItem(self.segment_text)
        self.canvas.scene().removeItem(self.dynamic_input_proxy)

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
        snap_result = self.canvas.snappingUtils().snapToMap(raw_point)
        cursor = snap_result.point() if snap_result.isValid() else raw_point

        self._update_snap_marker(cursor, snap_result)

        if not self.is_drawing:
            self._display(f"\nFirst point: {cursor.x():.3f}, {cursor.y():.3f}\n")
        else:
            dist, bearing = self._draw_preview_segment(cursor)
            self._display(f"\nDist: {dist:.3f}   Bearing: {bearing:.2f}°\n")
            if self.dynamic_input_proxy.isVisible():
                cp = self.canvas.getCoordinateTransform().transform(cursor)
                self.dynamic_input_proxy.setPos(cp.x() + 15, cp.y() + 15)
                self._dist_line.setPlaceholderText(f"{dist:.3f}")
                self._angle_line.setPlaceholderText(f"{bearing:.2f}")

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._finish()
            return

        if event.button() != Qt.LeftButton:
            return

        raw_point = self.toMapCoordinates(event.pos())
        snap_result = self.canvas.snappingUtils().snapToMap(raw_point)
        clicked_point = snap_result.point() if snap_result.isValid() else raw_point
        self.snap_marker.setVisible(False)

        self.polyline_layer = self._get_or_create_polyline_layer()
        self._add_point(clicked_point)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._finish()
