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
    QgsPalLayerSettings, 
    QgsTextFormat, 
    QgsVectorLayerSimpleLabeling,
    QgsPointLocator,
    QgsWkbTypes,
    QgsFillSymbol,
    QgsSingleSymbolRenderer,
    QgsCircularString, 
    QgsCurvePolygon, 
    QgsGeometry, 
    QgsFeature,
    QgsPoint,
    QgsCoordinateReferenceSystem
)
from qgis.PyQt.QtWidgets import QGraphicsTextItem
from PyQt5.QtCore import QVariant, QEvent
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QGraphicsProxyWidget
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QIcon, QFont, QColor
from .snapSettingConfig import snapSettingConfig
import math
from . import get_appropriate_crs_str


LAYER_NAME = "circles"
LAYER_COLOR_OUTLINE = "#E05C00"
LAYER_COLOR_FILL = "224,92,0,0"
RUBBER_BAND_COLOR = QColor(224, 92, 0)
SNAP_MARKER_COLOR = QColor(224, 92, 0)
TEXT_COLOR = QColor(180, 70, 0)
LABEL_FONT = QFont("Arial", 8)
LABEL_COLOR = QColor("#393939")
LABEL_SIZE = 10


class CircleDrawer(QgsMapTool):
    """
    A QGIS map tool that allows the user to draw circles by:
      1. Clicking a center point
      2. Clicking a radius point
    The resulting circle is stored as a CurvePolygon (circular string) in a memory layer.
    """

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas
        self.terminal_dock = terminal_dock

        # State
        self.center_point = None   # QgsPointXY — set on first click
        self.is_drawing = False    # True after center is picked

        # CRS
        self.appropriate_crs = get_appropriate_crs_str.get_canvas_epsg(self.canvas)
        self._apply_project_crs()

        # Layer
        self.circle_layer = self._get_or_create_circle_layer()

        # Snap settings
        snapSettingConfig()

        # UI elements
        self.snap_marker = self._create_snap_marker()
        self.preview_circle_band = self._create_rubber_band(QgsWkbTypes.PolygonGeometry, Qt.DashLine, fill_alpha=10)
        self.radius_line_band = self._create_rubber_band(QgsWkbTypes.LineGeometry, Qt.DashLine)
        self.radius_text = self._create_text_item()
        self._syncing = False
        self.dynamic_input_proxy = self._create_dynamic_input()

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

    def _create_rubber_band(self, geometry_type, line_style, fill_alpha=0):
        band = QgsRubberBand(self.canvas, geometry_type)
        band.setColor(RUBBER_BAND_COLOR)
        band.setWidth(2 if geometry_type == QgsWkbTypes.PolygonGeometry else 1)
        band.setLineStyle(line_style)
        if geometry_type == QgsWkbTypes.PolygonGeometry:
            band.setFillColor(QColor(224, 92, 0, fill_alpha))
        return band

    def _create_text_item(self):
        text_item = QGraphicsTextItem("")
        text_item.setDefaultTextColor(TEXT_COLOR)
        self.canvas.scene().addItem(text_item)
        return text_item

    # -------------------------------------------------------------------------
    # Layer management
    # -------------------------------------------------------------------------

    def _get_or_create_circle_layer(self):
        """Return the existing circles layer or create a fresh one."""
        existing = QgsProject.instance().mapLayersByName(LAYER_NAME)
        if existing:
            layer = existing[0]
            if not layer.isEditable():
                layer.startEditing()
            return layer
        return self._create_circle_layer()

    def _create_circle_layer(self):
        """Create a new CurvePolygon memory layer for storing circles."""
        layer = QgsVectorLayer(
            f"CurvePolygon?crs=EPSG:{self.appropriate_crs}&curve=yes",
            LAYER_NAME,
            "memory"
        )
        provider = layer.dataProvider()
        provider.addAttributes([QgsField("radius", QVariant.Double)])
        layer.updateFields()

        # Style
        symbol = QgsFillSymbol.createSimple({
            "outline_color": LAYER_COLOR_OUTLINE,
            "outline_width": "0.4",
            "outline_style": "solid",
            "color": LAYER_COLOR_FILL,
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        # Labels
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = "radius"
        label_settings.placement = QgsPalLayerSettings.Line
        text_format = QgsTextFormat()
        text_format.setFont(LABEL_FONT)
        text_format.setColor(LABEL_COLOR)
        text_format.setSize(LABEL_SIZE)
        label_settings.setFormat(text_format)
        layer.setLabelsEnabled(True)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))

        QgsProject.instance().addMapLayer(layer)
        layer.startEditing()
        return layer

    # -------------------------------------------------------------------------
    # Circle geometry
    # -------------------------------------------------------------------------

    def _build_circle_geometry(self, center: QgsPointXY, radius: float) -> QgsGeometry:
        """
        Build a QgsGeometry representing a full circle as a QgsCurvePolygon
        made of a QgsCircularString. Five points define the full arc
        (start == end to close it).
        """
        cx, cy = center.x(), center.y()

        # Cardinal points around the circle: E → S → W → N → E
        arc_points = [
            QgsPoint(cx + radius, cy),           # East  (start)
            QgsPoint(cx,          cy - radius),  # South
            QgsPoint(cx - radius, cy),           # West
            QgsPoint(cx,          cy + radius),  # North
            QgsPoint(cx + radius, cy),           # East  (close)
        ]

        circular_string = QgsCircularString()
        circular_string.setPoints(arc_points)

        curve_polygon = QgsCurvePolygon()
        curve_polygon.setExteriorRing(circular_string)

        return QgsGeometry(curve_polygon)

    def _commit_circle(self, center: QgsPointXY, radius_point: QgsPointXY):
        """Add a circle feature to the circle layer."""
        radius = center.distance(radius_point)
        geometry = self._build_circle_geometry(center, radius)

        feature = QgsFeature(self.circle_layer.fields())
        feature.setGeometry(geometry)
        feature.setAttribute("radius", round(radius, 3))

        self.circle_layer.addFeature(feature)
        self.circle_layer.updateExtents()
        self.circle_layer.triggerRepaint()

        return radius

    # -------------------------------------------------------------------------
    # Preview helpers
    # -------------------------------------------------------------------------

    def _update_snap_marker(self, point: QgsPointXY, snap_result):
        """Show or hide the snap marker based on snap result."""
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

    def _draw_circle_preview(self, center: QgsPointXY, cursor: QgsPointXY):
        """Render rubber-band circle preview and radius line."""
        radius = center.distance(cursor)

        # Circle preview
        self.preview_circle_band.reset(QgsWkbTypes.PolygonGeometry)
        circle_geom = QgsGeometry.fromPointXY(center).buffer(radius, 64)
        self.preview_circle_band.addGeometry(circle_geom, None)
        self.preview_circle_band.show()

        # Radius line
        self.radius_line_band.reset(QgsWkbTypes.LineGeometry)
        self.radius_line_band.addPoint(center)
        self.radius_line_band.addPoint(cursor)

        # Floating radius label
        self._update_radius_label(center, cursor, radius)

    def _update_radius_label(self, center: QgsPointXY, cursor: QgsPointXY, radius: float):
        """Position and rotate the floating distance label."""
        mid = QgsPointXY(
            (center.x() + cursor.x()) / 2,
            (center.y() + cursor.y()) / 2,
        )
        dx = cursor.x() - center.x()
        dy = cursor.y() - center.y()
        angle = -math.degrees(math.atan2(dy, dx))
        # Keep text readable (avoid upside-down)
        if angle < -90:
            angle += 180
        elif angle > 90:
            angle -= 180

        canvas_pt = self.canvas.getCoordinateTransform().transform(mid)
        self.radius_text.setPos(canvas_pt.x(), canvas_pt.y())
        self.radius_text.setPlainText(f"{radius:.3f}")
        self.radius_text.setRotation(angle)

    def _clear_preview(self):
        """Reset all temporary drawing feedback."""
        self.preview_circle_band.reset(QgsWkbTypes.PolygonGeometry)
        self.radius_line_band.reset(QgsWkbTypes.LineGeometry)
        self.radius_text.setPlainText("")
        self.snap_marker.setVisible(False)

    # -------------------------------------------------------------------------
    # Dynamic input box (AutoCAD-style floating widget)
    # -------------------------------------------------------------------------

    def _create_dynamic_input(self):
        container = QWidget()
        container.setStyleSheet(
            "QWidget  { background: #1a1a2e; border: 1px solid #4a9eff; }"
            "QLabel   { color: #7fa8d4; font-size: 9px; border: none;"
            "           padding: 2px 6px 1px 6px; }"
            "QLineEdit { background: transparent; color: #e8e8e8; border: none;"
            "            border-top: 1px solid #2a2a4e; padding: 3px 6px;"
            "            font-size: 11px; min-width: 110px; }"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._dynamic_label = QLabel("Radius")
        self._dynamic_line  = QLineEdit()
        self._dynamic_line.returnPressed.connect(self._on_dynamic_input_enter)
        self._dynamic_line.installEventFilter(self)

        layout.addWidget(self._dynamic_label)
        layout.addWidget(self._dynamic_line)
        container.adjustSize()

        proxy = QGraphicsProxyWidget()
        proxy.setWidget(container)
        proxy.setZValue(100)
        proxy.setVisible(False)
        self.canvas.scene().addItem(proxy)
        return proxy

    def eventFilter(self, obj, event):
        if obj is self._dynamic_line and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.deactivate()
                return True
            if event.key() == Qt.Key_Space:
                self._on_dynamic_input_enter()
                return True
        return super().eventFilter(obj, event)

    def _show_dynamic_input(self, canvas_x, canvas_y):
        self._dynamic_line.clear()
        self.dynamic_input_proxy.setPos(canvas_x + 15, canvas_y + 15)
        self.dynamic_input_proxy.setVisible(True)
        self.terminal_dock.command.textChanged.connect(self._sync_terminal_to_dynamic)
        self._dynamic_line.textChanged.connect(self._sync_dynamic_to_terminal)
        self._dynamic_line.setFocus()

    def _hide_dynamic_input(self):
        self.dynamic_input_proxy.setVisible(False)
        try:
            self.terminal_dock.command.textChanged.disconnect(self._sync_terminal_to_dynamic)
            self._dynamic_line.textChanged.disconnect(self._sync_dynamic_to_terminal)
        except Exception:
            pass
        self._dynamic_line.clear()

    def _sync_terminal_to_dynamic(self, text):
        if not self._syncing:
            self._syncing = True
            self._dynamic_line.setText(text)
            self._syncing = False

    def _sync_dynamic_to_terminal(self, text):
        if not self._syncing:
            self._syncing = True
            self.terminal_dock.command.setText(text)
            self._syncing = False

    def _on_dynamic_input_enter(self):
        text = self._dynamic_line.text() or self._dynamic_line.placeholderText()
        self._hide_dynamic_input()
        self._on_radius_typed(text)

    # -------------------------------------------------------------------------
    # Terminal helpers
    # -------------------------------------------------------------------------

    def _display(self, message: str):
        """Append a line to the terminal display (does not persist to commandOutputText)."""
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + message
        )

    def _log(self, message: str):
        """Persist a line to commandOutputText and refresh the display."""
        self.terminal_dock.commandOutputText += message
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    # -------------------------------------------------------------------------
    # QgsMapTool overrides
    # -------------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._display("\nSelect center point …\n")

    def _on_radius_typed(self, text: str):
        """Handle a radius value typed in the terminal or dynamic input box."""
        self._hide_dynamic_input()
        try:
            radius = float(text)
            if radius <= 0:
                raise ValueError
        except ValueError:
            self._log(f"\nInvalid radius '{text}' — enter a positive number")
            self.terminal_dock.request_input("radius: ", self._on_radius_typed)
            cp = self.canvas.getCoordinateTransform().transform(self.center_point)
            self._show_dynamic_input(cp.x(), cp.y())
            return

        radius_pt = QgsPointXY(self.center_point.x() + radius, self.center_point.y())
        actual = self._commit_circle(self.center_point, radius_pt)
        self._log(f"\nRadius: {actual:.3f}")

        self._clear_preview()
        self.center_point = None
        self.is_drawing = False
        self._display("\nSelect center point …\n")

    def deactivate(self):
        self.terminal_dock.clear_input_handler()
        self._hide_dynamic_input()
        self.canvas.unsetMapTool(self)
        self.terminal_dock.command.setFocus()

        # Persist edits
        self.circle_layer.updateExtents()
        self.circle_layer.commitChanges()

        # Clean up UI
        self._clear_preview()
        self.canvas.scene().removeItem(self.radius_text)
        self.canvas.scene().removeItem(self.dynamic_input_proxy)

        # Reset state
        self.center_point = None
        self.is_drawing = False

        self._display("\n........\n")
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.deactivate()

    # -------------------------------------------------------------------------
    # Mouse events
    # -------------------------------------------------------------------------

    def canvasMoveEvent(self, event):
        raw_point = self.toMapCoordinates(event.pos())
        snap_result = self.canvas.snappingUtils().snapToMap(raw_point)
        cursor = snap_result.point() if snap_result.isValid() else raw_point

        self._update_snap_marker(cursor, snap_result)

        if not self.is_drawing:
            # Phase 1: waiting for center click — just echo cursor position
            self._display(f"\nSelect center point: {cursor.x():.3f}, {cursor.y():.3f}\n")
        else:
            # Phase 2: center chosen — show live circle preview
            self._draw_circle_preview(self.center_point, cursor)
            radius = self.center_point.distance(cursor)
            self._display(f"\nRadius: {radius:.3f}\n")
            # Keep dynamic input box near cursor with live radius as placeholder
            if self.dynamic_input_proxy.isVisible():
                cp = self.canvas.getCoordinateTransform().transform(cursor)
                self.dynamic_input_proxy.setPos(cp.x() + 15, cp.y() + 15)
                self._dynamic_line.setPlaceholderText(f"{radius:.3f}")

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            return

        if event.button() != Qt.LeftButton:
            return

        # Resolve snapped click position
        raw_point = self.toMapCoordinates(event.pos())
        snap_result = self.canvas.snappingUtils().snapToMap(raw_point)
        clicked_point = snap_result.point() if snap_result.isValid() else raw_point
        self.snap_marker.setVisible(False)

        # Ensure the layer is ready
        self.circle_layer = self._get_or_create_circle_layer()

        if not self.is_drawing:
            # ── First click: record center ──────────────────────────────────
            self.center_point = clicked_point
            self.is_drawing = True
            self._log(f"\nCenter: {clicked_point.x():.3f}, {clicked_point.y():.3f}")
            self._display("\nEnter radius or click radius point…\n")
            self.terminal_dock.request_input("radius: ", self._on_radius_typed)
            cp = self.canvas.getCoordinateTransform().transform(clicked_point)
            self._show_dynamic_input(cp.x(), cp.y())

        else:
            # ── Second click: finalise circle (cancel any pending typed input) ──
            self._hide_dynamic_input()
            self.terminal_dock.clear_input_handler()
            radius = self._commit_circle(self.center_point, clicked_point)
            self._log(
                f"\nRadius point: {clicked_point.x():.3f}, {clicked_point.y():.3f}"
                f"  |  Radius: {radius:.3f}"
            )

            # Reset for the next circle
            self._clear_preview()
            self.center_point = None
            self.is_drawing = False
            self._display("\nSelect center point …\n")