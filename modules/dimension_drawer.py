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
    QgsWkbTypes
)
from PyQt5.QtCore import QVariant
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QIcon, QFont, QColor
from .snapSettingConfig import snapSettingConfig

class DimensionDrawer(QgsMapTool):

    def __init__(self, canvas, terminal_dock, operation_type):
        super().__init__(canvas)
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self.operation_type = operation_type
        self.dim_points = []
        self.dim_layer = self.getDimensionLayer()
        snapSettingConfig()
        
        # create it once when initializing your tool
        self.snap_marker = QgsVertexMarker(self.canvas)
        self.snap_marker.setColor(QColor(255, 0, 0))  # red
        self.snap_marker.setIconSize(10)
        self.snap_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.snap_marker.setPenWidth(2)
        self.snap_marker.setVisible(False)  # start hidden
        
        
        # Create rubber band
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)

        # Style the rubberband
        self.rubber_band.setColor(QColor(255, 0, 0))  # Red
        self.rubber_band.setWidth(1)
        self.rubber_band.setLineStyle(Qt.DashLine)
        
        
        
        
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
        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.snap_marker.setVisible(False)
        self.dim_points.clear()
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\n........\n"
        )
        # Call parent
        super().deactivate()
        
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            self.deactivate()
            
            
    def getDimensionLayer(self):
        layer_name = "dimension_layer"
        layers = QgsProject.instance().mapLayersByName(layer_name)

        if layers:
            return layers[0]

        self.dim_layer = QgsVectorLayer("LineString?crs=EPSG:32636", layer_name, "memory")
        provider = self.dim_layer.dataProvider()
        # provider.addAttributes([QgsField("distance", "double")])
        provider.addAttributes([QgsField("distance", QVariant.Double)])
        self.dim_layer.updateFields()
        QgsProject.instance().addMapLayer(self.dim_layer)
        
        # Set the symbol settings
        self.symbol = QgsLineSymbol.createSimple({
                    'color': 'transparent',
                    'width': '0',
                    'line_style': 'dashed'
                })
        self.dim_layer.renderer().setSymbol(self.symbol)
        
        # Set label settings
        self.label_settings = QgsPalLayerSettings()
        self.label_settings.fieldName = 'distance'  # field to show
        self.label_settings.placement = QgsPalLayerSettings.Line  # place labels along lines
        
        self.text_format = QgsTextFormat()
        self.text_format.setFont(QFont("Arial", 10))  # font + size
        self.text_format.setColor(QColor("#41b4e0"))      # text color
        self.text_format.setSize(10)                  # size in points

        self.label_settings.setFormat(self.text_format)
        
        self.labeling = QgsVectorLayerSimpleLabeling(self.label_settings)
        self.dim_layer.setLabelsEnabled(True)
        self.dim_layer.setLabeling(self.labeling)
        self.dim_layer.triggerRepaint()
        
        self.dim_layer.startEditing()

        return self.dim_layer













    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        
        # Use the canvas to snap
        snap_result = self.canvas.snappingUtils().snapToMap(point)
        if snap_result.isValid():
            point = snap_result.point()
            self.snap_marker.setCenter(point)
            self.snap_marker.setVisible(True)
            
            # print("Layer:", snap_result.layer().name() if snap_result.layer() else None)
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
            self.rubber_band.setWidth(2)
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            self.snap_marker.setVisible(False)
            
        if len(self.dim_points) == 0:
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect start point: {round(point.x(),3)}, {round(point.y(),3)}\n'
            )
            if snap_result.type() == QgsPointLocator.Vertex:
                self.rubber_band.setWidth(1)
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                
            if snap_result.type() == QgsPointLocator.Edge:
                before_vertex, after_vertex = snap_result.edgePoints()
                self.rubber_band.setWidth(2)
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                self.rubber_band.addPoint(before_vertex)
                self.rubber_band.addPoint(after_vertex)
                
        elif len(self.dim_points) == 1:
            # Clear it
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            # Add points
            self.rubber_band.addPoint(self.dim_points[0])
            self.rubber_band.addPoint(point)
            dim_dist = self.dim_points[0].distance(point)
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect end point: {round(dim_dist,3)}\n'
            )
        
        
        
        
        
        
        
        
        
        
        
        
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
            snap_result = self.canvas.snappingUtils().snapToMap(point)
            if snap_result.isValid():
                point = snap_result.point()
                
                # Here we add dimensions for instances where the cursor is on the edge only
                if len(self.dim_points) == 0 and snap_result.type() == QgsPointLocator.Edge and self.operation_type == 'selected':
                    # Get the layer and feature ID
                    layer = snap_result.layer()
                    fid = snap_result.featureId()
                    
                    # Fetch the feature from the layer
                    feature = layer.getFeature(fid)
                    feature_geom = feature.geometry()
                    # print(feature_geom)
                    if feature_geom and feature_geom.isGeosValid():
                        # boundary = feature_geom.coerceToType(QgsWkbTypes.LineGeometry)  # False = don’t force 2D if it’s 3D
                        boundary = feature_geom.coerceToType(QgsWkbTypes.LineString, True)[0]  # True = allow 2D only

                        if boundary.isMultipart():
                            print('Will ahndle multipart later ... ')
                            # lines = boundary.asMultiPolyline()
                            return
                        else:
                            outer_ring = boundary.asPolyline()
                            
                        for i in range(1, len(outer_ring)):
                            before_vertex = outer_ring[i - 1]
                            after_vertex = outer_ring[i]

                            self.dim_points.append(before_vertex)
                            self.dim_points.append(after_vertex)

                            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                            self.createDimensionFeature()
                            self.dim_points.clear()

                    return
                
                elif len(self.dim_points) == 0 and snap_result.type() == QgsPointLocator.Edge:
                    before_vertex, after_vertex = snap_result.edgePoints()
                    
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
                
            elif len(self.dim_points) == 2:
                # Clear the rubberband preview
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                
                dim_dist = self.dim_points[0].distance(self.dim_points[1])
                self.terminal_dock.commandOutputText += f'\nEnd point: {round(point.x(),3)}, {round(point.y(),3)} \nDistance: {round(dim_dist,3)}'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                
                self.createDimensionFeature()
                self.dim_points.clear()
                
                
                
                
                
                
                
                
                
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
        
        dim_dist = self.dim_points[0].distance(self.dim_points[1])
        # --- Add feature ---
        self.feature = QgsFeature(self.dim_layer.fields())  # Important: initialize feature with layer fields
        self.feature.setGeometry(QgsGeometry.fromPolylineXY(self.dim_points))
        # feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        self.feature.setAttribute("distance", round(dim_dist, 3))  # Use field name instead of index
        self.dim_layer.addFeature(self.feature)  # simplified; no need to go through provider directly
        self.dim_layer.triggerRepaint()
        
        