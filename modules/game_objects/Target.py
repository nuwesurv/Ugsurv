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


class Target():
    def __init__(self, name, canvas, start_cords,
                 game_extent,
                 speed_vector,
                 geom_type=QgsWkbTypes.PolygonGeometry,
                 color=QColor(190, 96, 23), width=1,
                 linestyle=Qt.SolidLine, fill_color=QColor(255,255,255)):

        self.name = name
        self.canvas = canvas
        self.game_extent = game_extent

        # Position
        self.prev_x, self.prev_y = start_cords
        self.curr_x, self.curr_y = start_cords

        # Size
        self.half_w = self.half_h = 0.04*(self.game_extent.yMaximum() - self.game_extent.yMinimum())
        # self.half_w = 40
        self.half_w1 = self.half_h1 = 0.008*(self.game_extent.yMaximum() - self.game_extent.yMinimum())
        # self.half_w1 = 8
        
        # Speed
        self.dx, self.dy = speed_vector

        # Rubberband
        self.rb = QgsRubberBand(canvas, geom_type)
        self.rb.setColor(color)
        self.rb.setWidth(width)
        self.rb.setLineStyle(linestyle)
        self.geometry = QgsGeometry()

        if fill_color and geom_type == QgsWkbTypes.PolygonGeometry:
            self.rb.setFillColor(fill_color)
        self.draw()
    

    def draw(self):
        """Create polygon geometry from current position"""
        cx = self.curr_x
        cy = self.curr_y

        outer_points = [
            QgsPointXY(cx - self.half_w, cy + self.half_h),
            QgsPointXY(cx + self.half_w, cy + self.half_h),
            QgsPointXY(cx + self.half_w, cy - self.half_h),
            QgsPointXY(cx - self.half_w, cy - self.half_h),
            QgsPointXY(cx - self.half_w, cy + self.half_h)
        ]
        inner_points = [
            QgsPointXY(cx - self.half_w1, cy),
            QgsPointXY(cx, cy ),
            QgsPointXY(cx, cy + self.half_h1),
            QgsPointXY(cx, cy ),
            QgsPointXY(cx + self.half_w1, cy),
            QgsPointXY(cx, cy ),
            QgsPointXY(cx, cy - self.half_h1),
            QgsPointXY(cx, cy ),
            QgsPointXY(cx - self.half_w1, cy),
        ]

        poly = QgsGeometry.fromPolygonXY([outer_points, inner_points])
        self.geometry = poly
        self.rb.setToGeometry(poly, None)


    def move(self):
        """Move target by dx dy"""
        self.prev_x = self.curr_x
        self.prev_y = self.curr_y
        
        # Check if object is still in the game extents
        if self.got_hit():
            # remove rubberband
            self.remove()
        else:
            self.curr_x += self.dx
            self.curr_y += self.dy
            self.draw()
            
            
    def got_hit(self):
        """Check if target is hit by dx dy movement collision into obstacle"""
        # Check if object is still in the game extents
        if self.curr_y + self.dy <= self.game_extent.yMinimum():
            # remove rubberband
            return True
        else:
            return False



    def reset_target(self, start_cords):
        """Reset the rubberband"""
        self.prev_x, self.prev_y = start_cords
        self.curr_x, self.curr_y = start_cords
        self.rb.reset(QgsWkbTypes.PolygonGeometry)
    
    
    def remove(self):
        """Delete the rubberband"""
        self.rb.reset(QgsWkbTypes.PolygonGeometry)
    
