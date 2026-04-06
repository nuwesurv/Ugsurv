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
from PyQt5.QtCore import QVariant
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QIcon, QFont, QColor
from .snapSettingConfig import snapSettingConfig
import math
from . import get_appropriate_crs_str


class UgsurvMaptool(QgsMapTool):

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas
        extent = self.canvas.extent()

        # Set the coordinate sytem to 36N
        self.appropriate_crs = get_appropriate_crs_str.get_canvas_epsg(self.canvas)
        crs = QgsCoordinateReferenceSystem(f"EPSG:{self.appropriate_crs}")
        QgsProject.instance().setCrs(crs)
        
        
        self.terminal_dock = terminal_dock
        self.cursor_points = []
        self.dim_layer = self.getDimensionLayer()
        snapSettingConfig()
        
        # create it once when initializing your tool
        self.snap_marker = QgsVertexMarker(self.canvas)
        self.snap_marker.setColor(QColor(255, 0, 0))  # red
        self.snap_marker.setIconSize(10)
        self.snap_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.snap_marker.setPenWidth(2)
        self.snap_marker.setVisible(False)  # start hidden
        

        # Create rubber bands
        # Style the rubberband
        self.rubber_band1 = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band1.setColor(QColor(255, 0, 0))  # Green
        self.rubber_band1.setWidth(2)
        self.rubber_band1.setLineStyle(Qt.DashLine)
        self.rubber_band1.setFillColor(QColor(255, 0, 0, 10))
        
        
    def showRubberBandPolygon(self, geometry, rubber_band):
        rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        rubber_band.addGeometry(geometry, None)
        rubber_band.show()
        
        
    def activate(self):
        super().activate()
        self.canvas.setFocus()
        
    def deactivate(self):
        self.canvas.unsetMapTool(self)
        self.terminal_dock.command.setFocus()
        # Commit any remaining edits
        self.dim_layer.updateExtents()
        self.dim_layer.commitChanges()
        
        # Hide snap marker and clear state
        self.rubber_band1.reset(QgsWkbTypes.PolygonGeometry)
        self.snap_marker.setVisible(False)
        self.cursor_points.clear()
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\n........\n"
        )
        # Call parent
        super().deactivate()
        
    def keyPressEvent(self, event):
        # if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
        #     self.deactivate()
            
        if event.key() in (Qt.Key_D, Qt.Key_S):
            self.terminal_dock.command.setText('D')
            
            
            
            
            
    def getDimensionLayer(self):
        """
        Create or return a curve-enabled memory layer for storing circular strings.
        """
        layer_name = "_drafts"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if layers:
            return layers[0]

        # Notice 'curve=yes' for circular strings support
        self.dim_layer = QgsVectorLayer(
            f"CurvePolygon?crs=EPSG:{self.appropriate_crs}&curve=yes", layer_name, "memory"
        )
        provider = self.dim_layer.dataProvider()
        provider.addAttributes([QgsField("distance", QVariant.Double)])
        self.dim_layer.updateFields()
        QgsProject.instance().addMapLayer(self.dim_layer)

        # Symbol
        self.symbol = QgsFillSymbol.createSimple({
            'outline_color': "#393939",
            'outline_width': '0.2',
            'outline_style': 'solid',
            'color': '255,0,0,0'  # transparent fill
        })
        renderer = QgsSingleSymbolRenderer(self.symbol)
        self.dim_layer.setRenderer(renderer)

        # Labeling (same as before)
        self.label_settings = QgsPalLayerSettings()
        self.label_settings.fieldName = 'distance'
        self.label_settings.placement = QgsPalLayerSettings.Line
        self.text_format = QgsTextFormat()
        self.text_format.setFont(QFont("Arial", 8))
        self.text_format.setColor(QColor("#393939"))
        self.text_format.setSize(10)
        self.label_settings.setFormat(self.text_format)
        self.labeling = QgsVectorLayerSimpleLabeling(self.label_settings)
        self.dim_layer.setLabelsEnabled(True)
        self.dim_layer.setLabeling(self.labeling)
        self.dim_layer.triggerRepaint()
        self.dim_layer.startEditing()
        return self.dim_layer
    
    
    def createCircleFeature(self):
        if len(self.cursor_points) != 2:
            print(f"Expected 2 points, got {len(self.cursor_points)}")
            return


        p1 = self.cursor_points[0]
        p2 = self.cursor_points[1]
        center = p1
        radius = center.distance(p2)

        # Create the curve polygon
        curve_polygon = QgsCurvePolygon()

        cs = QgsCircularString()
        cs.setPoints([
            QgsPoint(center.x() + radius, center.y() + 0),
            QgsPoint(center.x() + 0, center.y() - radius),
            QgsPoint(center.x() - radius, center.y() + 0),
            QgsPoint(center.x() + 0, center.y() + radius),
            QgsPoint(center.x() + radius, center.y() + 0),
        ])
        curve_polygon.setExteriorRing(cs)  # Add the arc

        # Create feature
        feat = QgsFeature(self.dim_layer.fields())
        feat.setGeometry(QgsGeometry(curve_polygon))
        feat.setAttribute("distance", round(radius, 3))
        self.dim_layer.addFeature(feat)
        self.dim_layer.triggerRepaint()
    
    
    
    



    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        
        # Use the canvas to snap
        snap_result = self.canvas.snappingUtils().snapToMap(point)
        if snap_result.isValid():
            point = snap_result.point()
            self.snap_marker.setCenter(point)
            self.snap_marker.setVisible(True)
            
            if snap_result.type() == QgsPointLocator.Vertex:
                self.snap_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
            elif snap_result.type() == QgsPointLocator.Edge:
                self.snap_marker.setIconType(QgsVertexMarker.ICON_DOUBLE_TRIANGLE)
            elif snap_result.type() == QgsPointLocator.Area:
                self.snap_marker.setIconType(QgsVertexMarker.ICON_RHOMBUS)
            elif snap_result.type() == QgsPointLocator.MiddleOfSegment:
                self.snap_marker.setIconType(QgsVertexMarker.ICON_TRIANGLE)
            else:
                self.snap_marker.setIconType(QgsVertexMarker.ICON_X)
        else:
            self.snap_marker.setVisible(False)
            
        if len(self.cursor_points) == 0:
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect start point: {round(point.x(),3)}, {round(point.y(),3)}\n'
            )
                
        elif len(self.cursor_points) == 1:
            # Clear it
            self.rubber_band1.reset(QgsWkbTypes.PolygonGeometry)
            # Add points
            circle_radius = self.cursor_points[0].distance(point)
            center_point = self.cursor_points[0]
            circle_geom = QgsGeometry.fromPointXY(center_point).buffer(circle_radius, 40)
            # print(circle_geom)
            self.showRubberBandPolygon(circle_geom, self.rubber_band1)
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect end point: {round(circle_radius,3)}\n'
            )
        
        
        
        
        
        
    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            
            
        if event.button() == Qt.LeftButton:
            layers = QgsProject.instance().mapLayersByName('_drafts')
            if layers:
                self.dim_layer.startEditing()
            else:
                self.dim_layer = self.getDimensionLayer()
            
            point = self.toMapCoordinates(event.pos())
            self.snap_marker.setVisible(False)
            # Recognize the snapped point.
            snap_result = self.canvas.snappingUtils().snapToMap(point)
            if snap_result.isValid():
                point = snap_result.point()
                    
                
            self.cursor_points.append(point)
            if len(self.cursor_points) == 1:
                self.terminal_dock.commandOutputText += f'\nStart point: {round(point.x(),3)}, {round(point.y(),3)}'
                self.terminal_dock.commandDisplay.setText(
                    self.terminal_dock.commandOutputText + f'\nSelect end point: ...\n'
                )
                
                
            elif len(self.cursor_points) == 2:
                # Clear the rubberband preview
                self.rubber_band1.reset(QgsWkbTypes.PolygonGeometry)
                
                self.createCircleFeature()
                circle_radius = self.cursor_points[0].distance(self.cursor_points[1])
                self.terminal_dock.commandOutputText += f'\nRadius: {round(point.x(),3)}, {round(point.y(),3)} \nDistance: {round(circle_radius,3)}'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                self.cursor_points.clear()
                
                