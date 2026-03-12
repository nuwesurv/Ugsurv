from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsFeature,
    QgsRectangle,
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
import math
import random

from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtCore import QTimer


class LaserPhoton():
    def __init__(self, name, canvas, angle, start_cords,
                 game_extent,
                 speed_vector,
                 geom_type=QgsWkbTypes.LineGeometry,
                 color=QColor(255,0,0), width=2,
                 fill_color=QColor(255,255,255)):

        self.name = name
        self.canvas = canvas
        self.game_extent = game_extent

        # Position
        self.prev_x, self.prev_y = start_cords
        self.curr_x, self.curr_y = start_cords
        self.angle = angle

        # Size
        self.half_h = 20
        self.half_w = 20
        
        # Speed
        self.dx, self.dy = speed_vector

        # Rubberband
        self.rb = QgsRubberBand(canvas, geom_type)
        self.rb.setColor(color)
        self.rb.setWidth(width)
        self.geometry = QgsGeometry()

        if fill_color and geom_type == QgsWkbTypes.LineGeometry:
            self.rb.setFillColor(fill_color)
        self.draw()
    

    def draw(self):
        """Create point geometry from current position"""

        cx = self.curr_x
        cy = self.curr_y
        laser_photon_length = 200
        dx = laser_photon_length * math.cos(self.angle)
        dy = laser_photon_length * math.sin(self.angle)

        bx = cx - dx
        by = cy - dy
        p1 = QgsPointXY(bx, by)
        p2 = QgsPointXY(cx, cy)
        geom = QgsGeometry.fromPolylineXY([p1, p2])

        self.geometry = geom
        self.rb.setToGeometry(geom, None)


    def move(self):
        """Move target by dx dy"""
        if self.got_hit():
            self.remove()
            return

        self.prev_x = self.curr_x
        self.prev_y = self.curr_y

        self.curr_x += self.dx
        self.curr_y += self.dy
        self.draw()
            
            
    def got_hit(self):
        """Check if target is hit by dx dy movement collision into obstacle"""
        # Check if object is still in the game extents
        if self.curr_y + self.dy >= self.game_extent.yMaximum():
            # remove rubberband
            return True
        else:
            return False



    def remove(self):
        """Delete the rubberband"""
        # self.rb.reset(QgsWkbTypes.LineGeometry)
        self.rb.hide()
        self.rb = None
    
