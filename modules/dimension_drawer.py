from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import pyqtSignal, Qt
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
)
from PyQt5.QtCore import QVariant
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QIcon, QFont, QColor
import math

class DimensionDrawer(QgsMapTool):
    cursor_cords = pyqtSignal(float, float)  # define signal
    leftClicked = pyqtSignal(float, float)  # custom signal

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self.canvas.setMapTool(self)
        self.dim_points = []
        
        # create it once when initializing your tool
        self.snap_marker = QgsVertexMarker(self.canvas)
        self.snap_marker.setColor(QColor(255, 0, 0))  # red
        self.snap_marker.setIconSize(10)
        self.snap_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.snap_marker.setPenWidth(2)
        self.snap_marker.setVisible(False)  # start hidden
        
    def activate(self):
        super().activate()
        self.canvas.setFocus()
        
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.snap_marker.setVisible(False)
            self.dim_points.clear()
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + "\nCommand cancelled.\n"
            )
            self.canvas.unsetMapTool(self)

    def canvasMoveEvent(self, event):
        # self.activate()
        point = self.toMapCoordinates(event.pos())
        x = point.x()
        y = point.y()
        
        # Use the canvas to snap
        point = QgsPointXY(x, y)
        snap_result = self.canvas.snappingUtils().snapToMap(point)
        if snap_result.isValid():
            snapped_point = snap_result.point()
            point = snapped_point
            self.snap_marker.setCenter(snapped_point)
            self.snap_marker.setVisible(True)
        else:
            self.snap_marker.setCenter(point)
            self.snap_marker.setVisible(True)
            
        if len(self.dim_points) == 0:
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect start point: {round(x,3)}, {round(y,3)}\n'
            )
        elif len(self.dim_points) == 1:
            dim_dist = math.sqrt((self.dim_points[0][0]-x)**2 + (self.dim_points[0][1]-y)**2)
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect end point: {round(dim_dist,3)}\n'
            )
        
        
    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())
            x = point.x()
            y = point.y()

            # --- Step 0: Create/get layer ---
            layer_name = "dimension_layer"
            layers = QgsProject.instance().mapLayersByName(layer_name)
            
            if not layers:
                dim_layer = QgsVectorLayer("LineString?crs=EPSG:32636", layer_name, "memory")
                provider = dim_layer.dataProvider()
                provider.addAttributes([QgsField("distance", QVariant.Double)])
                dim_layer.updateFields()
                QgsProject.instance().addMapLayer(dim_layer)
            else:
                dim_layer = layers[0]
                # Ensure the layer has the 'distance' field
                if 'distance' not in [f.name() for f in dim_layer.fields()]:
                    dim_layer.startEditing()
                    dim_layer.dataProvider().addAttributes([QgsField("distance", QVariant.Double)])
                    dim_layer.updateFields()
                    dim_layer.commitChanges()

            dim_layer.startEditing()
            
            
            self.snap_marker.setVisible(False)
            snap_result = self.canvas.snappingUtils().snapToMap(point)
            if snap_result.isValid():
                snapped_point = snap_result.point()
                point = snapped_point
                x = snapped_point.x()
                y = snapped_point.y()
                print(x,y)
                
            self.dim_points.append([x, y])
            if len(self.dim_points) == 1:
                self.terminal_dock.commandOutputText += f'\nStart point: {round(x,3)}, {round(y,3)}'
                self.terminal_dock.commandDisplay.setText(
                    self.terminal_dock.commandOutputText + f'\nSelect end point: ... \n'
                )
            elif len(self.dim_points) == 2:
                dim_dist = math.sqrt((self.dim_points[0][0]-x)**2 + (self.dim_points[0][1]-y)**2)
                self.terminal_dock.commandOutputText += f'\nEnd point: {round(x,3)}, {round(y,3)} \nDistance: {round(dim_dist,3)}'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                # self.canvas.unsetMapTool(self)
                
                # --- Add feature ---
                feature = QgsFeature(dim_layer.fields())  # Important: initialize feature with layer fields
                feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(self.dim_points[0][0], self.dim_points[0][1]), QgsPointXY(self.dim_points[1][0], self.dim_points[1][1])]))
                # feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
                feature.setAttribute("distance", round(dim_dist, 3))  # Use field name instead of index
                
                
                # dim_layer.startEditing()
                dim_layer.addFeature(feature)  # simplified; no need to go through provider directly
                symbol = QgsLineSymbol.createSimple({
                    'color': 'transparent',
                    'width': '0',
                    'line_style': 'dashed'
                })
                dim_layer.renderer().setSymbol(symbol)
                dim_layer.triggerRepaint()
                
                # Create label settings
                label_settings = QgsPalLayerSettings()
                label_settings.fieldName = 'distance'  # field to show
                label_settings.placement = QgsPalLayerSettings.Line  # place labels along lines
                
                text_format = QgsTextFormat()
                text_format.setFont(QFont("Arial", 10))  # font + size
                text_format.setColor(QColor("#41b4e0"))      # text color
                text_format.setSize(10)                  # size in points

                label_settings.setFormat(text_format)
                
                labeling = QgsVectorLayerSimpleLabeling(label_settings)
                dim_layer.setLabelsEnabled(True)
                dim_layer.setLabeling(labeling)
                dim_layer.triggerRepaint()
                
                
                dim_layer.updateExtents()
                dim_layer.commitChanges()
                
                # Reset the tool for the next dimension to be drawn
                self.dim_points.clear()

            
            
