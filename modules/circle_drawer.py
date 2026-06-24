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
from PyQt5.QtCore import QVariant
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QIcon, QFont, QColor
from .snapSettingConfig import snapSettingConfig
import math
from . import get_appropriate_crs_str


LAYER_NAME = "circles"
LAYER_COLOR_OUTLINE = "#305ED2"
LAYER_COLOR_FILL = "255,0,0,0"
RUBBER_BAND_COLOR = QColor(255, 0, 0)
SNAP_MARKER_COLOR = QColor(255, 0, 0)
TEXT_COLOR = Qt.red
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
            band.setFillColor(QColor(255, 0, 0, fill_alpha))
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
            "outline_width": "0.2",
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

    def deactivate(self):
        self.canvas.unsetMapTool(self)
        self.terminal_dock.command.setFocus()

        # Persist edits
        self.circle_layer.updateExtents()
        self.circle_layer.commitChanges()

        # Clean up UI
        self._clear_preview()
        self.canvas.scene().removeItem(self.radius_text)

        # Reset state
        self.center_point = None
        self.is_drawing = False

        self._display("\n........\n")
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
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
            self._display("\nSelect radius point …\n")

        else:
            # ── Second click: finalise circle ───────────────────────────────
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