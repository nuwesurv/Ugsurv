from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.PyQt.QtGui import QColor

import math


class LaserPhoton:
    def __init__(self, name, canvas, angle, start_cords, game_extent, speed_vector,
                 geom_type=QgsWkbTypes.LineGeometry,
                 color=QColor(0, 220, 255), width=3,
                 linestyle=Qt.SolidLine):

        self.name = name
        self.canvas = canvas
        self.game_extent = game_extent
        self.angle = angle

        self.curr_x, self.curr_y = start_cords
        self.dx, self.dy = speed_vector

        self.trail_length = 200

        self.rb = QgsRubberBand(canvas, geom_type)
        self.rb.setColor(color)
        self.rb.setWidth(width)
        self.rb.setLineStyle(linestyle)
        self.geometry = QgsGeometry()
        self.draw()

    def draw(self):
        cx, cy = self.curr_x, self.curr_y
        bx = cx - self.trail_length * math.cos(self.angle)
        by = cy - self.trail_length * math.sin(self.angle)
        geom = QgsGeometry.fromPolylineXY([QgsPointXY(bx, by), QgsPointXY(cx, cy)])
        self.geometry = geom
        self.rb.setToGeometry(geom, None)

    def move(self):
        self.curr_x += self.dx
        self.curr_y += self.dy
        self.draw()

    def got_hit(self):
        ext = self.game_extent
        return (
            self.curr_y >= ext.yMaximum() or
            self.curr_y <= ext.yMinimum() or
            self.curr_x <= ext.xMinimum() or
            self.curr_x >= ext.xMaximum()
        )

    def remove(self):
        if self.rb:
            self.rb.reset(QgsWkbTypes.LineGeometry)
            self.rb.hide()
            self.rb = None
