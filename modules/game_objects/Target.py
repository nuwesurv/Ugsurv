from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.PyQt.QtGui import QColor


class Target:
    def __init__(self, name, canvas, start_cords, game_extent, speed_vector,
                 geom_type=QgsWkbTypes.PolygonGeometry,
                 color=QColor(190, 96, 23), width=1,
                 linestyle=Qt.SolidLine, fill_color=QColor(255, 255, 255)):

        self.name = name
        self.canvas = canvas
        self.game_extent = game_extent

        self.curr_x, self.curr_y = start_cords
        self.dx, self.dy = speed_vector

        size = 0.04 * (self.game_extent.yMaximum() - self.game_extent.yMinimum())
        self.half_w = self.half_h = size
        self.inner_size = 0.008 * (self.game_extent.yMaximum() - self.game_extent.yMinimum())

        self.rb = QgsRubberBand(canvas, geom_type)
        self.rb.setColor(color)
        self.rb.setWidth(width)
        self.rb.setLineStyle(linestyle)
        if fill_color:
            self.rb.setFillColor(fill_color)
        self.geometry = QgsGeometry()
        self.draw()

    def draw(self):
        cx, cy = self.curr_x, self.curr_y
        hw, hh = self.half_w, self.half_h
        s = self.inner_size

        outer = [
            QgsPointXY(cx - hw, cy + hh),
            QgsPointXY(cx + hw, cy + hh),
            QgsPointXY(cx + hw, cy - hh),
            QgsPointXY(cx - hw, cy - hh),
            QgsPointXY(cx - hw, cy + hh),
        ]
        # Crosshair ring as inner hole
        inner = [
            QgsPointXY(cx - s, cy),
            QgsPointXY(cx, cy + s),
            QgsPointXY(cx + s, cy),
            QgsPointXY(cx, cy - s),
            QgsPointXY(cx - s, cy),
        ]
        poly = QgsGeometry.fromPolygonXY([outer, inner])
        self.geometry = poly
        self.rb.setToGeometry(poly, None)

    def move(self):
        # Bounce off horizontal walls
        next_x = self.curr_x + self.dx
        if next_x - self.half_w < self.game_extent.xMinimum() or \
                next_x + self.half_w > self.game_extent.xMaximum():
            self.dx = -self.dx

        self.curr_x += self.dx
        self.curr_y += self.dy
        self.draw()

    def got_hit(self):
        return self.curr_y + self.dy <= self.game_extent.yMinimum()

    def reset_target(self, start_cords):
        self.curr_x, self.curr_y = start_cords
        self.rb.reset(QgsWkbTypes.PolygonGeometry)
        self.draw()

    def remove(self):
        self.rb.reset(QgsWkbTypes.PolygonGeometry)
