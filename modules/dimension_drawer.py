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
    QgsCoordinateReferenceSystem,
    QgsRectangle,
)
from PyQt5.QtCore import QVariant, QPoint
from PyQt5.QtWidgets import QLabel
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QColor
from .dynamic_input import DynamicInput
from .layer_utils import add_to_plugin_group, open_layer_from_gpkg, create_layer_in_gpkg, apply_dimension_style, enable_feature_render_order
from . import snap_utils
from . import crs_utils
import math


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


class DimensionDrawer(QgsMapTool):

    def __init__(self, canvas, terminal_dock, operation_type):
        super().__init__(canvas)
        self.canvas = canvas
        extent = self.canvas.extent()

        # Set the coordinate sytem to 36N
        self.appropriate_crs = crs_utils.get_canvas_epsg(self.canvas)
        self.crs = QgsCoordinateReferenceSystem(f"EPSG:{self.appropriate_crs}")
        QgsProject.instance().setCrs(self.crs)
        
        
        
        self.terminal_dock = terminal_dock
        self.operation_type = operation_type
        self.dim_points = []
        self.dim_layer = self.getDimensionLayer()
        self._maptool = None   # set by UgsurvMaptool.set_tool()
        snap_utils.init_snap()

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        # floating dynamic input: distance + bearing after start point is placed
        self._dinput = DynamicInput(canvas, terminal_dock, [
            {"key": "dist",    "label": "Distance"},
            {"key": "bearing", "label": "Bearing (°)"},
        ])
        self._dinput.on_cancel = self.deactivate

        # create it once when initializing your tool
        self.snap_marker = QgsVertexMarker(self.canvas)
        self.snap_marker.setColor(QColor(66, 135, 245))
        self.snap_marker.setIconSize(10)
        self.snap_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.snap_marker.setPenWidth(2)
        self.snap_marker.setVisible(False)  # start hidden
        
        
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.rubber_band.setColor(QColor(255, 140, 0))
        self.rubber_band.setWidth(2)
        self.rubber_band.setLineStyle(Qt.DashLine)
        self.rubber_band.setVisible(False)
        
        
        
        
        
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
    # DynamicInput callbacks
    # -------------------------------------------------------------------------

    def _on_endpoint_committed(self, values: dict):
        """Called when user presses Enter / Space in the floating dist/bearing widget."""
        self.terminal_dock.clear_input_handler()
        self._apply_endpoint(values["dist"], values["bearing"])

    def _on_endpoint_terminal(self, text: str):
        """Called when user types in the terminal and presses Enter."""
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        parts = text.strip().replace(',', ' ').split()
        if len(parts) == 1:
            self._apply_endpoint(parts[0], "0")
        elif len(parts) >= 2:
            self._apply_endpoint(parts[0], parts[1])
        else:
            self.terminal_dock.commandOutputText += f"\nInvalid input — use: distance  or  distance,bearing"
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    def _apply_endpoint(self, dist_text: str, bearing_text: str):
        try:
            distance = float(dist_text)
            bearing  = float(bearing_text)
            if distance <= 0:
                raise ValueError
        except ValueError:
            self.terminal_dock.commandOutputText += f"\nInvalid input — enter distance [Tab] bearing (°)"
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
            return

        end_pt = self._polar_to_point(self.dim_points[0], distance, bearing)
        self.dim_points.append(end_pt)
        dim_dist = self.dim_points[0].distance(end_pt)
        self.terminal_dock.commandOutputText += (
            f"\nEnd point: {round(end_pt.x(), 3)}, {round(end_pt.y(), 3)}"
            f"  |  Distance: {round(dim_dist, 3)}"
        )
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.createDimensionFeature()
        self.dim_points.clear()

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

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()

    def deactivate(self):
        self._dinput.destroy()
        self.terminal_dock.clear_input_handler()
        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            self.terminal_dock.command.setFocus()
        self.dim_layer.updateExtents()

        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.rubber_band.setVisible(False)
        self.snap_marker.setVisible(False)
        self._hint.hide()
        self.dim_points.clear()
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\n........\n"
        )
        # Call parent
        super().deactivate()
        
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.deactivate()

    def mouseDoubleClickEvent(self, event):
        self.terminal_dock.command.setFocus()
            
            
    def _ensure_dim_fields(self, layer):
        existing = {f.name() for f in layer.fields()}
        to_add = []
        if "distance"       not in existing: to_add.append(QgsField("distance",       QVariant.Double))
        if "decimal_places" not in existing: to_add.append(QgsField("decimal_places", QVariant.Int))
        if "color"          not in existing: to_add.append(QgsField("color",          QVariant.String))
        if "text_size"      not in existing: to_add.append(QgsField("text_size",      QVariant.Double))
        if "font_type"      not in existing: to_add.append(QgsField("font_type",      QVariant.String))
        if "line_thickness" not in existing: to_add.append(QgsField("line_thickness", QVariant.Double))
        if "line_type"      not in existing: to_add.append(QgsField("line_type",      QVariant.String))
        if "z_index"        not in existing: to_add.append(QgsField("z_index",        QVariant.Int))
        if to_add:
            layer.dataProvider().addAttributes(to_add)
            layer.updateFields()

    def getDimensionLayer(self):
        layer_name = "_dimension_layer"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if layers:
            layer = layers[0]
            self._ensure_dim_fields(layer)
            enable_feature_render_order(layer)
            return layer

        layer = open_layer_from_gpkg(layer_name)
        if layer:
            self._ensure_dim_fields(layer)
            apply_dimension_style(layer)
            enable_feature_render_order(layer)
            add_to_plugin_group(layer)
            layer.startEditing()
            return layer

        mem = QgsVectorLayer(f"LineString?crs=EPSG:{self.appropriate_crs}", layer_name, "memory")
        mem.dataProvider().addAttributes([
            QgsField("distance",       QVariant.Double),
            QgsField("decimal_places", QVariant.Int),
            QgsField("color",          QVariant.String),
            QgsField("text_size",      QVariant.Double),
            QgsField("font_type",      QVariant.String),
            QgsField("line_thickness", QVariant.Double),
            QgsField("line_type",      QVariant.String),
            QgsField("z_index",        QVariant.Int),
        ])
        mem.updateFields()

        layer = create_layer_in_gpkg(mem)
        apply_dimension_style(layer)
        enable_feature_render_order(layer)
        add_to_plugin_group(layer)
        layer.startEditing()
        return layer













    def _find_feature_near(self, map_pt):
        """Return (layer, feature) of the nearest non-point feature within snap tolerance, or (None, None)."""
        tol = snap_utils._tol(self.canvas)
        rect = QgsRectangle(map_pt.x() - tol, map_pt.y() - tol, map_pt.x() + tol, map_pt.y() + tol)
        best_lyr, best_feat, best_dist = None, None, tol
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        for lyr in snap_utils._non_point_layers():
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isNull() or geom.isEmpty():
                    continue
                d = geom.distance(pt_geom)
                if d < best_dist:
                    best_dist = d
                    best_lyr = lyr
                    best_feat = feat
        return best_lyr, best_feat

    def _nearest_segment(self, map_pt, geom):
        """Return (a, b) QgsPointXY of the segment in geom closest to map_pt, or (None, None)."""
        verts = list(geom.vertices())
        best_a, best_b, best_d = None, None, float('inf')
        pt_geom = QgsGeometry.fromPointXY(map_pt)
        for i in range(len(verts) - 1):
            a = QgsPointXY(verts[i].x(), verts[i].y())
            b = QgsPointXY(verts[i + 1].x(), verts[i + 1].y())
            seg = QgsGeometry.fromPolylineXY([a, b])
            d = seg.distance(pt_geom)
            if d < best_d:
                best_d = d
                best_a, best_b = a, b
        return best_a, best_b

    def canvasMoveEvent(self, event):
        raw_pt = self.toMapCoordinates(event.pos())
        snapped, icon = snap_utils.snap_point(self.canvas, raw_pt)
        if icon is not None:
            point = snapped
            self.snap_marker.setCenter(snapped)
            self.snap_marker.setIconType(icon)
            self.snap_marker.setVisible(True)
        else:
            point = raw_pt
            self.snap_marker.setVisible(False)

        if len(self.dim_points) == 0:
            on_edge = (icon == snap_utils.SNAP_ICON['nearest'])
            if on_edge:
                _, feature = self._find_feature_near(snapped)
                if feature is not None:
                    if self.operation_type == 'selected':
                        preview_geom = feature.geometry()
                    else:
                        a, b = self._nearest_segment(snapped, feature.geometry())
                        preview_geom = QgsGeometry.fromPolylineXY([a, b]) if (a and b) else None
                    if preview_geom:
                        self.rubber_band.setToGeometry(preview_geom)
                        self.rubber_band.setVisible(True)
                    else:
                        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                        self.rubber_band.setVisible(False)
                else:
                    self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                    self.rubber_band.setVisible(False)
            else:
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                self.rubber_band.setVisible(False)
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect start point: {round(point.x(),3)}, {round(point.y(),3)}\n'
            )
            self._show_hint(event.pos(), "Click start point")

        elif len(self.dim_points) == 1:
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            self.rubber_band.setVisible(True)
            self.rubber_band.addPoint(self.dim_points[0], False)
            self.rubber_band.addPoint(point)
            dim_dist = self.dim_points[0].distance(point)
            bearing  = self._bearing(self.dim_points[0], point)
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect end point: dist={round(dim_dist,3)}  bearing={round(bearing,2)}°\n'
            )
            cp = self.canvas.getCoordinateTransform().transform(point)
            self._dinput.update(cp.x(), cp.y(), {
                "dist":    f"{dim_dist:.3f}",
                "bearing": f"{bearing:.2f}",
            })
            self._show_hint(event.pos(), "Click end point  or  type dist,bearing + Enter")
        
        
        
        
        
        
        
        
        
        
        
        
    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            
        if event.button() == Qt.LeftButton:
            layers = QgsProject.instance().mapLayersByName('dimension_layer')
            if layers:
                self.dim_layer.startEditing()
            else:
                self.dim_layer = self.getDimensionLayer()
            
            point = self.toMapCoordinates(event.pos())

            self.snap_marker.setVisible(False)
            snapped, icon = snap_utils.snap_point(self.canvas, point)
            point = snapped
            on_edge = (icon == snap_utils.SNAP_ICON['nearest'])

            if len(self.dim_points) == 0 and on_edge and self.operation_type == 'selected':
                _, feature = self._find_feature_near(snapped)
                if feature is not None:
                    feature_geom = feature.geometry()

                    if feature_geom and feature_geom.isGeosValid():
                        gt = QgsWkbTypes.geometryType(feature_geom.wkbType())

                        # Collect all rings/parts as flat lists of QgsPointXY
                        vertex_rings = []
                        if gt == QgsWkbTypes.PolygonGeometry:
                            if feature_geom.isMultipart():
                                for poly in feature_geom.asMultiPolygon():
                                    vertex_rings.extend(poly)
                            else:
                                vertex_rings = feature_geom.asPolygon()
                        elif gt == QgsWkbTypes.LineGeometry:
                            if feature_geom.isMultipart():
                                vertex_rings = feature_geom.asMultiPolyline()
                            else:
                                vertex_rings = [feature_geom.asPolyline()]

                        for ring in vertex_rings:
                            for i in range(1, len(ring)):
                                self.dim_points.append(ring[i - 1])
                                self.dim_points.append(ring[i])
                                self.createDimensionFeature()
                                self.dim_points.clear()

                        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                    return
                
            elif len(self.dim_points) == 0 and on_edge:
                _, feat = self._find_feature_near(snapped)
                if feat is not None:
                    before_vertex, after_vertex = self._nearest_segment(snapped, feat.geometry())
                    if before_vertex and after_vertex:
                        self.dim_points.append(before_vertex)
                        self.dim_points.append(after_vertex)
                        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                        self.createDimensionFeature()
                        self.dim_points.clear()
                        return
                    
                    
                
            self.dim_points.append(point)
            if len(self.dim_points) == 1:
                self.terminal_dock.commandOutputText += f'\nStart point: {round(point.x(),3)}, {round(point.y(),3)}'
                self.terminal_dock.commandDisplay.setText(
                    self.terminal_dock.commandOutputText + f'\nSelect end point: ...\n'
                )
                cp = self.canvas.getCoordinateTransform().transform(point)
                self._dinput.on_commit = self._on_endpoint_committed
                self.terminal_dock.request_input("dist,bearing: ", self._on_endpoint_terminal)
                self._dinput.show(cp.x(), cp.y())

            elif len(self.dim_points) == 2:
                # User clicked the end point — hide dinput and commit
                self._dinput.hide()
                self.terminal_dock.clear_input_handler()
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)

                dim_dist = self.dim_points[0].distance(self.dim_points[1])
                self.terminal_dock.commandOutputText += f'\nEnd point: {round(point.x(),3)}, {round(point.y(),3)} \nDistance: {round(dim_dist,3)}'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

                self.createDimensionFeature()
                self.dim_points.clear()

        self.terminal_dock.command.setFocus()
                
                
                
                
                
                
                
                
                
    def createDimensionFeature(self):
        if self.dim_points.__len__() != 2:
            print(f'The dimension points required are two(2) but instead got{self.dim_points.__len__()}')
            return
        
        p1 = self.dim_points[0]
        p2 = self.dim_points[1]

        geom1 = QgsGeometry.fromPolylineXY([p1, p2])
        geom2 = QgsGeometry.fromPolylineXY([p2, p1])

        # 🔎 Check for duplicate
        for feat in self.dim_layer.getFeatures():
            existing_geom = feat.geometry()

            if not existing_geom:
                continue

            # Compare both directions
            if existing_geom.equals(geom1) or existing_geom.equals(geom2):
                print("Dimension already exists. Skipping.")
                return  # 🚫 Stop — duplicate found
        
        p1, p2 = self.dim_points[0], self.dim_points[1]
        dim_dist = p1.distance(p2)

        self.feature = QgsFeature(self.dim_layer.fields())
        self.feature.setGeometry(QgsGeometry.fromPolylineXY(self.dim_points))
        self.feature.setAttribute("distance",       round(dim_dist, 3))
        self.feature.setAttribute("decimal_places", 3)
        self.feature.setAttribute("color",          "#000000")
        self.feature.setAttribute("text_size",      4)
        self.feature.setAttribute("font_type",      "Century Gothic")
        self.feature.setAttribute("line_thickness", 0.3)
        self.feature.setAttribute("line_type",      "solid")
        self.feature.setAttribute("z_index",        1)
        self.dim_layer.addFeature(self.feature)
        self.dim_layer.triggerRepaint()
        
        
