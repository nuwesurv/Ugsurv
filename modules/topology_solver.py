from qgis.gui import QgsMapTool, QgsRubberBand
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
    QgsPointLocator,
    QgsWkbTypes
)
from PyQt5.QtCore import QVariant
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QIcon, QFont, QColor
from .snapSettingConfig import snapSettingConfig

class TopologySolver(QgsMapTool):
    cursor_cords = pyqtSignal(float, float)  # define signal
    leftClicked = pyqtSignal(float, float)  # custom signal

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self.cursor_points = []
        
        
        
        
    def activate(self):
        super().activate()
        self.canvas.setFocus()
        
    def deactivate(self):
        self.canvas.unsetMapTool(self)
        self.terminal_dock.command.setFocus()
        
        # Hide snap marker and clear state
        self.cursor_points.clear()
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\nCommand exited and changes saved 😊.\n"
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
                self.terminal_dock.commandOutputText + f'\nSelect first feature to be adjusted no:1\n'
            )
                
        elif len(self.cursor_points) == 1:
            # Clear it
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect any other reference feature no:{len(self.cursor_points)}\n'
            )
        
        
        
        
        
        
        
        
        
        
        
        
    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.deactivate()
            
        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())

            self.cursor_points.append(point)
            if len(self.cursor_points) == 1:
                self.terminal_dock.commandOutputText += f'\nStart point: {round(point.x(),3)}, {round(point.y(),3)}'
                self.terminal_dock.commandDisplay.setText(
                    self.terminal_dock.commandOutputText + f'\nSelect end point: ...\n'
                )
                
            elif len(self.cursor_points) == 2:
                # Clear the rubberband preview
                
                dim_dist = self.cursor_points[0].distance(self.cursor_points[1])
                self.terminal_dock.commandOutputText += f'\nEnd point: {round(point.x(),3)}, {round(point.y(),3)} \nDistance: {round(dim_dist,3)}'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                
                self.cursor_points.clear()
                
                
                