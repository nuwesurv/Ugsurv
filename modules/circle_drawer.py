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
    QgsSingleSymbolRenderer,
    QgsCircularString,
    QgsPoint,
    QgsCoordinateReferenceSystem,
)
from qgis.PyQt.QtWidgets import QGraphicsTextItem
from PyQt5.QtCore import QVariant, QPoint
from PyQt5.QtWidgets import QLabel
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QFont, QColor
from .snap_config import snapSettingConfig
from .dynamic_input import DynamicInput
from .layer_utils import add_to_plugin_group, open_layer_from_gpkg, create_layer_in_gpkg
import math
from . import crs_utils


LAYER_NAME = "_circles"
LAYER_COLOR_OUTLINE = "#E05C00"
LAYER_COLOR_FILL = "224,92,0,0"
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


class CircleDrawer(QgsMapTool):
    """
    A QGIS map tool that allows the user to draw circles by:
      1. Clicking a center point
      2. Clicking a radius point  —or—  typing a radius value
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
        self.appropriate_crs = crs_utils.get_canvas_epsg(self.canvas)
        self._apply_project_crs()

        # Layer
        self.circle_layer = self._get_or_create_circle_layer()

        # Snap settings
        snapSettingConfig()

        # UI elements
        self.snap_marker = self._create_snap_marker()
        self.preview_circle_band = self._create_rubber_band(QgsWkbTypes.LineGeometry, Qt.DashLine)
        self.radius_line_band = self._create_rubber_band(QgsWkbTypes.LineGeometry, Qt.DashLine)
        self.radius_text = self._create_text_item()
        self._maptool = None   # set by UgsurvMaptool.set_tool()

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        # Floating dynamic input (single "Radius" field)
        self._dinput = DynamicInput(canvas, terminal_dock, [{"key": "radius", "label": "Radius"}])
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
        layer = open_layer_from_gpkg(LAYER_NAME)
        if layer:
            self._apply_circle_style(layer)
            add_to_plugin_group(layer)
            layer.startEditing()
            return layer
        return self._create_circle_layer()

    def _apply_circle_style(self, layer):
        symbol = QgsLineSymbol.createSimple({
            "color": LAYER_COLOR_OUTLINE,
            "width": "0.4",
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setLabelsEnabled(False)

    def _create_circle_layer(self):
        """Create a new CircularString line layer (file-backed in GPKG, memory fallback)."""
        mem = QgsVectorLayer(
            f"CircularString?crs=EPSG:{self.appropriate_crs}",
            LAYER_NAME,
            "memory"
        )
        mem.dataProvider().addAttributes([QgsField("radius", QVariant.Double)])
        mem.updateFields()

        layer = create_layer_in_gpkg(mem)
        self._apply_circle_style(layer)
        add_to_plugin_group(layer)
        layer.startEditing()
        return layer

    # -------------------------------------------------------------------------
    # Circle geometry
    # -------------------------------------------------------------------------

    def _build_circle_geometry(self, center: QgsPointXY, radius: float) -> QgsGeometry:
        """Build a QgsGeometry circle as a closed QgsCircularString (polyline)."""
        cx, cy = center.x(), center.y()
        arc_points = [
            QgsPoint(cx + radius, cy),           # East  (start)
            QgsPoint(cx,          cy - radius),  # South
            QgsPoint(cx - radius, cy),           # West
            QgsPoint(cx,          cy + radius),  # North
            QgsPoint(cx + radius, cy),           # East  (close)
        ]
        cs = QgsCircularString()
        cs.setPoints(arc_points)
        return QgsGeometry(cs)

    def _commit_circle(self, center: QgsPointXY, radius_point: QgsPointXY):
        """Add a circle feature to the circle layer and return the radius."""
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

        self.preview_circle_band.reset(QgsWkbTypes.LineGeometry)
        cx, cy = center.x(), center.y()
        cs_pts = [
            QgsPoint(cx + radius, cy),
            QgsPoint(cx,          cy - radius),
            QgsPoint(cx - radius, cy),
            QgsPoint(cx,          cy + radius),
            QgsPoint(cx + radius, cy),
        ]
        cs = QgsCircularString()
        cs.setPoints(cs_pts)
        self.preview_circle_band.addGeometry(QgsGeometry(cs), None)
        self.preview_circle_band.show()

        self.radius_line_band.reset(QgsWkbTypes.LineGeometry)
        self.radius_line_band.addPoint(center)
        self.radius_line_band.addPoint(cursor)

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
        if angle < -90:
            angle += 180
        elif angle > 90:
            angle -= 180
        canvas_pt = self.canvas.getCoordinateTransform().transform(mid)
        self.radius_text.setPos(canvas_pt.x(), canvas_pt.y())
        self.radius_text.setPlainText(f"{radius:.3f}")
        self.radius_text.setRotation(angle)

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

    def _find_circle_center_snap(self, screen_pos) -> QgsPointXY | None:
        """Return the center of the nearest circle if cursor is within 12 px of it."""
        threshold_sq = 12 ** 2
        ct = self.canvas.getCoordinateTransform()
        for feat in self.circle_layer.getFeatures():
            geom = feat.geometry()
            if geom.isNull():
                continue
            bbox = geom.boundingBox()
            if bbox.isEmpty():
                continue
            # Bounding-box centre == circle centre for any circular geometry type
            center_map = QgsPointXY(bbox.center())
            sc = ct.transform(center_map)
            dx = screen_pos.x() - sc.x()
            dy = screen_pos.y() - sc.y()
            if dx * dx + dy * dy <= threshold_sq:
                return center_map
        return None

    def _clear_preview(self):
        self.preview_circle_band.reset(QgsWkbTypes.LineGeometry)
        self.radius_line_band.reset(QgsWkbTypes.LineGeometry)
        self.radius_text.setPlainText("")
        self.snap_marker.setVisible(False)

    # -------------------------------------------------------------------------
    # Input handling
    # -------------------------------------------------------------------------

    def _on_radius_terminal(self, text: str):
        """Called when user types a value in the terminal and presses Enter."""
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._apply_radius(text.strip())

    def _on_radius_committed(self, values: dict):
        """Called when user presses Enter / Space in the floating input widget."""
        # DynamicInput already called hide() before firing this callback
        self.terminal_dock.clear_input_handler()
        self._apply_radius(values["radius"])

    def _apply_radius(self, text: str):
        """Validate radius text and commit the circle, or re-show input on error."""
        try:
            radius = float(text)
            if radius <= 0:
                raise ValueError
        except ValueError:
            self._log(f"\nInvalid radius '{text}' — enter a positive number")
            self._re_request_input()
            return

        radius_pt = QgsPointXY(self.center_point.x() + radius, self.center_point.y())
        actual = self._commit_circle(self.center_point, radius_pt)
        self._log(f"\nRadius: {actual:.3f}")

        self._clear_preview()
        self.center_point = None
        self.is_drawing = False
        self._display("\nSelect center point …\n")

    def _re_request_input(self):
        """Re-show both the terminal prompt and the floating widget after a bad value."""
        if not self.center_point:
            return
        self.terminal_dock.request_input("radius: ", self._on_radius_terminal)
        self._dinput.on_commit = self._on_radius_committed
        cp = self.canvas.getCoordinateTransform().transform(self.center_point)
        self._dinput.show(cp.x(), cp.y())

    # -------------------------------------------------------------------------
    # Terminal helpers
    # -------------------------------------------------------------------------

    def _display(self, message: str):
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + message
        )

    def _log(self, message: str):
        self.terminal_dock.commandOutputText += message
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    # -------------------------------------------------------------------------
    # QgsMapTool overrides
    # -------------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()
        self._display("\nSelect center point …\n")

    def deactivate(self):
        self.terminal_dock.clear_input_handler()
        self._dinput.destroy()

        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            self.terminal_dock.command.setFocus()

        self.circle_layer.updateExtents()

        self._clear_preview()
        self._hint.hide()
        self.canvas.scene().removeItem(self.radius_text)

        self.center_point = None
        self.is_drawing = False

        self._display("\n........\n")
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.deactivate()

    def mouseDoubleClickEvent(self, event):
        self.terminal_dock.command.setFocus()

    # -------------------------------------------------------------------------
    # Mouse events
    # -------------------------------------------------------------------------

    def canvasMoveEvent(self, event):
        raw_point = self.toMapCoordinates(event.pos())
        snap_result = self.canvas.snappingUtils().snapToMap(raw_point)
        cursor = snap_result.point() if snap_result.isValid() else raw_point

        center_snap = self._find_circle_center_snap(event.pos())
        if center_snap:
            cursor = center_snap
            self.snap_marker.setCenter(center_snap)
            self.snap_marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self.snap_marker.setVisible(True)
        else:
            self._update_snap_marker(cursor, snap_result)

        if not self.is_drawing:
            self._display(f"\nSelect center point: {cursor.x():.3f}, {cursor.y():.3f}\n")
            self._show_hint(event.pos(), "Click center point")
        else:
            self._draw_circle_preview(self.center_point, cursor)
            radius = self.center_point.distance(cursor)
            self._display(f"\nRadius: {radius:.3f}\n")
            # Move the floating input box near the cursor; update its placeholder live
            cp = self.canvas.getCoordinateTransform().transform(cursor)
            self._dinput.update(cp.x(), cp.y(), {"radius": f"{radius:.3f}"})
            self._show_hint(event.pos(), "Click radius point  or  type radius + Enter")

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            return

        if event.button() != Qt.LeftButton:
            return

        raw_point = self.toMapCoordinates(event.pos())
        snap_result = self.canvas.snappingUtils().snapToMap(raw_point)
        clicked_point = snap_result.point() if snap_result.isValid() else raw_point

        center_snap = self._find_circle_center_snap(event.pos())
        if center_snap:
            clicked_point = center_snap
        self.snap_marker.setVisible(False)

        self.circle_layer = self._get_or_create_circle_layer()

        if not self.is_drawing:
            # First click: record center and show floating input
            self.center_point = clicked_point
            self.is_drawing = True
            self._log(f"\nCenter: {clicked_point.x():.3f}, {clicked_point.y():.3f}")
            self._display("\nEnter radius or click radius point…\n")
            self.terminal_dock.request_input("radius: ", self._on_radius_terminal)
            self._dinput.on_commit = self._on_radius_committed
            cp = self.canvas.getCoordinateTransform().transform(clicked_point)
            self._dinput.show(cp.x(), cp.y())

        else:
            # Second click: commit circle using the clicked radius point
            self._dinput.hide()
            self.terminal_dock.clear_input_handler()
            radius = self._commit_circle(self.center_point, clicked_point)
            self._log(
                f"\nRadius point: {clicked_point.x():.3f}, {clicked_point.y():.3f}"
                f"  |  Radius: {radius:.3f}"
            )
            self._clear_preview()
            self.center_point = None
            self.is_drawing = False
            self._display("\nSelect center point …\n")

        self.terminal_dock.command.setFocus()
