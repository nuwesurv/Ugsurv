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
from qgis.gui import QgsMapTool, QgsMapToolIdentifyFeature, QgsRubberBand
from qgis.PyQt.QtGui import QIcon, QFont, QColor
import math


class FixGeometry(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self.cursor_points = []
        self.selected_geoms = []
        self.adj_feature_properties = {}
        # self.s_layer = self.getSampleLayer()
        
        # Create rubber bands
        # Style the rubberband
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(QColor(0, 0, 255))  # Blue
        self.rubber_band.setWidth(2)
        self.rubber_band.setLineStyle(Qt.DashLine)
        self.rubber_band.setFillColor(QColor(0, 0, 255, 10))
        
        
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
        
        # Remove rubberbands
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        
        # Clean variables
        self.cursor_points.clear()
        self.selected_geoms.clear()
        self.adj_feature_properties = {}
        
        # Hide snap marker and clear state
        self.cursor_points.clear()
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\n...\n"
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
                self.terminal_dock.commandOutputText + f'\nClick adjust feature:\n'
            )
        
        
    def canvasPressEvent(self, event):
        try:
            if event.button() == Qt.RightButton:
                self.fixGeometry()
                
                # Reset the rubberbands
                self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                
                # Clean variables
                self.cursor_points.clear()
                self.selected_geoms.clear()
                self.adj_feature_properties = {}
                
                self.terminal_dock.commandOutputText += f'\n------Next >>>'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                return


            if event.button() == Qt.LeftButton:
                point = self.toMapCoordinates(event.pos())
                # Call identify from parent class
                results = self.identify(
                    event.x(),
                    event.y(),
                    [layer for layer in QgsProject.instance().mapLayers().values()],
                    QgsMapToolIdentifyFeature.TopDownAll
                )

                if results:
                    self.cursor_points.append(point)
                    feature = results[0].mFeature
                    self.selected_geoms.append(feature.geometry())
                    if len(self.selected_geoms) == 1:
                        self.adj_feature_properties['layer'] = results[0].mLayer.name()
                        self.adj_feature_properties['fid'] = results[0].mFeature.id()
                else:
                    if len(self.selected_geoms) == 1:
                        self.adj_feature_properties['layer'] = results[0].mLayer.name()
                        self.adj_feature_properties['fid'] = results[0].mFeature.id()
                        self.terminal_dock.commandOutputText += f'\nNo feature detected?'
                        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                    
                self.terminal_dock.commandOutputText += f'\nFeature{len(self.cursor_points)}: {results[0].mLayer.name()}'
                self.terminal_dock.commandDisplay.setText(
                    self.terminal_dock.commandOutputText + '\n'
                )
        except Exception as e:
            self.terminal_dock.commandOutputText += f'\nExperienced error: {e}'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                
                
                
    def fixGeometry(self):
        if self.selected_geoms.__len__() != 1:
            self.terminal_dock.commandOutputText += f'\nSelect only one feature!'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
            return
        
        # Setup the adj_feature
        # adj_feature = self.selected_geoms[0]
        layer_name = self.adj_feature_properties['layer']
        fid = self.adj_feature_properties['fid']
        layers = QgsProject.instance().mapLayersByName(layer_name)

        if layers:
            layer =  layers[0]
        else:
            self.terminal_dock.commandOutputText += f'\nSelect layer not found!'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
            return
        adj_feature = layer.getFeature(fid).geometry()
        
        
        # Clean up the Geometries.
        adj_feature1 = adj_feature.makeValid()
        # adj_feature1 = adj_feature1.simplify(0.001)
            
        layer.startEditing()
        layer.beginEditCommand("Fix parcel Geometry")
        
        layer.changeGeometry(self.adj_feature_properties['fid'], adj_feature1)
        layer.endEditCommand()
        layer.triggerRepaint()
        
    