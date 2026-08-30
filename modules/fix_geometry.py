from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsPointLocator,
    QgsWkbTypes,
    QgsCoordinateTransform
)
from qgis.gui import QgsMapTool, QgsMapToolIdentifyFeature, QgsRubberBand
from qgis.PyQt.QtGui import QIcon, QFont, QColor
import math


class FixGeometry(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, iface, terminal_dock):
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self.cursor_points = []
        self.selected_geoms = []
        self.adj_feature_properties = {}
        self._maptool = None   # set by UgsurvMaptool.set_tool()
        # self.s_layer = self.getSampleLayer()

        # Create rubber bands
        # Style the rubberband
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band.setColor(QColor(0, 0, 255))  # Blue
        self.rubber_band.setWidth(2)
        self.rubber_band.setLineStyle(Qt.PenStyle.DashLine)
        self.rubber_band.setFillColor(QColor(0, 0, 255, 10))
        
        
    def showRubberBandPolygon(self, geometry, rubber_band):
        rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        rubber_band.addGeometry(geometry, None)
        rubber_band.show()
        
        
        
    def activate(self):
        super().activate()
        self.canvas.setFocus()
        
    def deactivate(self):
        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            self.terminal_dock.command.setFocus()

        # Remove rubberbands
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        
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
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.deactivate()
            
            
            
            

    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        
        if len(self.cursor_points) == 0:
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nClick adjust feature:\n'
            )
        
        
    def canvasPressEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.RightButton:
                self.fixGeometry()
                self.deactivate()
                return

            if event.button() == Qt.MouseButton.LeftButton:
                point = self.toMapCoordinates(event.pos())

                active_layer = self.iface.activeLayer()
                if not active_layer:
                    self.terminal_dock.commandOutputText += f'\nNo active layer selected in the Layers panel!'
                    self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                    return

                results = self.identify(
                    event.x(),
                    event.y(),
                    [active_layer],
                    QgsMapToolIdentifyFeature.IdentifyMode.TopDownAll
                )

                if not results:
                    self.terminal_dock.commandOutputText += f'\nNo feature detected'
                    self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                    return

                # Reset previous selection so only one feature is ever held
                self.cursor_points.clear()
                self.selected_geoms.clear()

                self.cursor_points.append(point)
                feature = results[0].mFeature
                feat_layer = results[0].mLayer

                geom = QgsGeometry(feature.geometry())
                project_crs = QgsProject.instance().crs()
                feat_crs = feat_layer.crs()
                if feat_crs != project_crs:
                    transform = QgsCoordinateTransform(feat_crs, project_crs, QgsProject.instance())
                    geom.transform(transform)

                self.selected_geoms.append(geom)
                self.adj_feature_properties['layer'] = feat_layer.name()
                self.adj_feature_properties['fid'] = feature.id()
                self.showRubberBandPolygon(geom, self.rubber_band)

                self.terminal_dock.commandOutputText += f'\nSelected: {feat_layer.name()} — right-click to fix, Esc to cancel'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText + '\n')

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
        
    