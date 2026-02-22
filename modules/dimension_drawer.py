from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.core import QgsPointXY

class CoordinateTracker(QgsMapTool):
    # cursor_cords = pyqtSignal(float, float)  # define signal
    # leftClicked = pyqtSignal(float, float)  # custom signal
    cursor_cords = []

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas

    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        x = point.x()
        y = point.y()

        
    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())
            x = point.x()
            y = point.y()

            # self.leftClicked.emit(x, y)  # fire signal
            
            
