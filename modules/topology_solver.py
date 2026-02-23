from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsPointLocator,
    QgsWkbTypes
)
from qgis.gui import QgsMapTool, QgsMapToolIdentifyFeature
from qgis.PyQt.QtGui import QIcon, QFont, QColor

class TopologySolver(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self.cursor_points = []
        self.selected_geoms = []
        self.adj_feature_properties = {}
        self.s_layer = self.getSampleLayer()
        
        
    def activate(self):
        super().activate()
        self.canvas.setFocus()
        
    def deactivate(self):
        self.canvas.unsetMapTool(self)
        self.terminal_dock.command.setFocus()
        
        # Clean variables
        self.cursor_points.clear()
        self.selected_geoms.clear()
        self.adj_feature_properties = {}
        
        # Hide snap marker and clear state
        self.cursor_points.clear()
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\n........\n"
        )
        # Call parent
        super().deactivate()
        
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            self.deactivate()
            
            

    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
            
        if len(self.cursor_points) == 0:
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect adjust feature:\n'
            )
                
        elif len(self.cursor_points) >= 1:
            # Clear it
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect reference feature no:{len(self.cursor_points)+1}\n'
            )
        
        
        
        
        
        
        
        
        
        
        
        
    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.solveTopology()
            self.deactivate()
            return

        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())
            self.cursor_points.append(point)
            # Call identify from parent class
            results = self.identify(
                event.x(),
                event.y(),
                [layer for layer in QgsProject.instance().mapLayers().values()],
                QgsMapToolIdentifyFeature.TopDownAll
            )

            if results:
                feature = results[0].mFeature
                self.selected_geoms.append(feature.geometry())
                if len(self.selected_geoms) == 1:
                    self.adj_feature_properties['layer'] = results[0].mLayer.name()
                    self.adj_feature_properties['featureId'] = results[0].mFeature.id()
                
                
            else:
                self.terminal_dock.commandOutputText += f'\nNo feature detected?'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                
            
            self.terminal_dock.commandOutputText += f'\nFeature{len(self.cursor_points)}: {results[0].mLayer.name()}'
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect reference feature no:{len(self.cursor_points)+1}\n'
            )
                
                
    def solveTopology(self):
        if self.selected_geoms.__len__() <= 1:
            self.terminal_dock.commandOutputText += f'\nSelect atleast two features!'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
            return
        
        adj_feature = self.selected_geoms[0]
        other_features = self.selected_geoms[1:]
        # Merge all other features
        merged_features = QgsGeometry.unaryUnion(other_features)
          
        # Step 1# ========================================
        # Solve overlaps
        adj_feature1 = adj_feature.difference(merged_features)
        if adj_feature1.isEmpty():
            # Nothing was subtracted
            adj_feature1 = adj_feature 
        
        # Step 2#  =======================================
        # Solve gaps
        union = adj_feature1.combine(merged_features)
        if not union.isMultipart():  # single polygon
            polygons = union.asPolygon()
            inner_rings = polygons[1:]  # list of list[QgsPointXY]

            # Convert each hole to QgsGeometry
            hole_geoms = []
            for ring in inner_rings:
                hole_geom = QgsGeometry.fromPolygonXY([ring])
                hole_geoms.append(hole_geom)

        # Merge the holes into one Multipart gap.
        merged_gaps = QgsGeometry.unaryUnion(hole_geoms)
        adj_feature2 = adj_feature1.combine(merged_gaps)
        adj_feature2 = adj_feature2.makeValid()
        # Optional: simplify to remove tiny unnecessary vertices
        adj_feature2 = adj_feature2.simplify(0.001)
        
        
        # Solve topological relation.
        layer_name = self.adj_feature_properties['layer']
        layers = QgsProject.instance().mapLayersByName(layer_name)

        if layers:
            layer =  layers[0]
        else:
            self.terminal_dock.commandOutputText += f'\nSelect layer not found!'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
            return
            
        layer.startEditing()
        layer.beginEditCommand("Fix parcel topology")
        
        layer.changeGeometry(self.adj_feature_properties['featureId'], adj_feature2)
        layer.endEditCommand()
        # layer.commitChanges()
        layer.triggerRepaint()
        
        
        new_feature = QgsFeature(self.s_layer.fields())
        new_feature.setGeometry(adj_feature2)

        self.s_layer.startEditing()
        self.s_layer.addFeature(new_feature)
        self.s_layer.triggerRepaint()
        
        # Clean variables
        self.cursor_points.clear()
        self.selected_geoms.clear()
        self.adj_feature_properties = {}
        
        
    
    
    
    
    
    
    
    
    
    def getSampleLayer(self):
        layer_name = "sample_layer"
        layers = QgsProject.instance().mapLayersByName(layer_name)

        if layers:
            return layers[0]

        self.s_layer = QgsVectorLayer("Polygon?crs=EPSG:32636", layer_name, "memory")
        QgsProject.instance().addMapLayer(self.s_layer)
        
        self.s_layer.triggerRepaint()
        self.s_layer.startEditing()

        return self.s_layer