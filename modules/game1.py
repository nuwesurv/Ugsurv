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

from .game_objects.Target import Target
from .game_objects.LaserPhoton import LaserPhoton




class Game1(QgsMapTool):

    def __init__(self, canvas, terminal_dock, operation_type):
        super().__init__(canvas)
        self.canvas = canvas
        extent = self.canvas.extent()
        canvas_width = extent.width()
        canvas_height = extent.height()
        self.game_extent = QgsRectangle(445200, 25200, 446800, 26200)
        self.telescope_center = QgsPointXY(self.game_extent.xMinimum() + (self.game_extent.xMaximum() - self.game_extent.xMinimum()) / 2, 
                                           self.game_extent.yMinimum() + 90)
        
        self.terminal_dock = terminal_dock
        self.operation_type = operation_type
        self.cursor_points = []
        
        # The game loop is here.
        self.is_playing = False
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self.game_loop)
        self.animation_interval = 20  # milliseconds
        self.targets = [
                        Target('Target1' ,self.canvas, self.get_rand_startxy(), self.game_extent, [0, -7]),
                        Target('Target2', self.canvas, self.get_rand_startxy(), self.game_extent,[0,-6]),
                        Target('Target3', self.canvas, self.get_rand_startxy(), self.game_extent, [0, -5]),
                        ]
        self.laserphotons = []
        self.start_game()
        
        # Target line from the total station
        self.target_line = self.createRubberBand(QgsWkbTypes.LineGeometry, QColor(255,0,0), 0.5, Qt.DashLine)
        
        # Create the Telescope of the total station
        self.laser_telescope = self.createRubberBand(QgsWkbTypes.PolygonGeometry, QColor(180,180,180), 1, Qt.SolidLine, QColor(255,255,255))
        self.laser_telescope.setToGeometry(self.create_laser_telescope(45), None)

        # self.line_rb.setToGeometry(line, None)
        self.zoom_to_extent()
        
        
    
    def get_rand_startxy(self):
        return [
            random.randint(int(self.game_extent.xMinimum()), int(self.game_extent.xMaximum())),
            random.randint(int(self.game_extent.yMaximum()), int(self.game_extent.yMaximum()) + 100)
        ]
    
    def zoom_to_extent(self):
        self.canvas.setExtent(self.game_extent)
        self.canvas.refresh()

    def create_laser_telescope(self, angle):
        half_h = 10
        half_w = 70
        cx, cy = self.telescope_center.x(), self.telescope_center.y()
        # rectangle before rotation
        points = [
            (cx-half_w, cy+half_h),
            (cx+half_w, cy+half_h+5),
            (cx+half_w, cy-half_h-5),
            (cx-half_w, cy-half_h),
            (cx-half_w, cy+half_h)
        ]

        theta = math.radians(angle)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        rotated_points = []

        for x, y in points:
            xr = cx + (x-cx)*cos_t - (y-cy)*sin_t
            yr = cy + (x-cx)*sin_t + (y-cy)*cos_t
            rotated_points.append(QgsPointXY(xr, yr))

        poly = QgsGeometry.fromPolygonXY([rotated_points])
        return poly
            
        
        
    def createRubberBand(self, geom_type, color=QColor(255, 0, 0), width=2, linestyle=Qt.SolidLine, fill_color=None):
        rb = QgsRubberBand(self.canvas, geom_type)
        rb.setColor(color)
        rb.setWidth(width)
        rb.setLineStyle(linestyle)
        if fill_color and geom_type == QgsWkbTypes.PolygonGeometry:
            rb.setFillColor(fill_color)
        return rb
        
        
    def checkRubberBandIsWithinBounds(self, geometry):
        if geometry is None or geometry.isEmpty():
            return

        bbox = geometry.boundingBox()

        if (
            bbox.xMinimum() <= self.game_extent.xMinimum() or
            bbox.xMaximum() >= self.game_extent.xMaximum() or
            bbox.yMinimum() <= self.game_extent.yMinimum() or
            bbox.yMaximum() >= self.game_extent.yMaximum()
        ):
            # remove rubberband
            return True
        else:
            return False
        
        
    def start_game(self):
        self.is_playing = True
        self.animation_timer.start(self.animation_interval)

    def stop_game(self):
        self.is_playing = False
        self.animation_timer.stop()
        
    def activate(self):
        super().activate()
        self.canvas.setFocus()
        
    def deactivate(self):
        self.canvas.unsetMapTool(self)
        self.terminal_dock.command.setFocus()
        
        # Hide snap marker and clear state
        self.stop_game()
        # Remove all targets and photons
        for target in self.targets:
            target.remove()
            
        for photon in self.laserphotons:
            photon.remove()
            
        self.laser_telescope.reset(QgsWkbTypes.LineGeometry)
        self.target_line.reset(QgsWkbTypes.LineGeometry)
        # self.target1.reset(QgsWkbTypes.LineGeometry)
        
        # Reste the cursor points that were stored.
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
        self.zoom_to_extent()
        point = self.toMapCoordinates(event.pos())
        
        # Deltas and the angle of the laser.
        dx = point.x() - self.telescope_center[0]
        dy = point.y() - self.telescope_center[1]
        angle_rad = math.atan2(dy, dx)
        angle_deg = math.degrees(angle_rad)
        
        # use angle to rotate telescope polygon
        self.laser_telescope.setToGeometry(
            self.create_laser_telescope(angle_deg),
            None
        )
        # Set up the target line.
        max_dist = 0.3 * (self.game_extent.yMaximum() - self.game_extent.yMinimum())
        laser_dist = math.sqrt(
                                (point.x() - self.telescope_center[0])**2 +
                                (point.y() - self.telescope_center[1])**2
                            )

        dx = max_dist * math.cos(angle_rad)
        dy = max_dist * math.sin(angle_rad)

        x = self.telescope_center[0] + dx
        y = self.telescope_center[1] + dy
        if laser_dist < max_dist:
            target_line_geom = QgsGeometry.fromPolylineXY([QgsPointXY(self.telescope_center[0], self.telescope_center[1]), point])
        else:
            target_line_geom = QgsGeometry.fromPolylineXY([QgsPointXY(self.telescope_center[0], self.telescope_center[1]), QgsPointXY(x, y)])
        
        self.target_line.setToGeometry(
            target_line_geom,
            None
        )

            
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + f'\nSelect end point: {angle_deg}\n'
        )
        
        
        
        
    
    def canvasPressEvent(self, event):
        # if event.button() == Qt.RightButton:
        #     self.deactivate()
            
        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())
            
            # Deltas and the angle of the laser.
            dx = point.x() - self.telescope_center[0]
            dy = point.y() - self.telescope_center[1]
            angle_rad = math.atan2(dy, dx)
            angle_deg = math.degrees(angle_rad)
            speed = 100
            dx = speed * math.cos(angle_rad)
            dy = speed * math.sin(angle_rad)
            
            
            self.laserphotons.append(LaserPhoton(f'laser_photon{len(self.laserphotons)}' ,self.canvas, angle_rad, self.telescope_center, self.game_extent, [dx, dy]))
            
            
            
            
            
            
            
    # This is the game loop running the objects.
    def game_loop(self):
        if not self.is_playing:
            return

        # ---- Move targets ----
        for target in self.targets:
            if target.got_hit():
                print(f"{target.name} has been hit")
                target.reset_target(self.get_rand_startxy())
            else:
                target.move()

        # ---- Move photons ----
        photons_to_remove = []
        for photon in self.laserphotons:
            if photon.got_hit():
                print(f"{photon.name} has been hit")
                photons_to_remove.append(photon)
                continue

            photon.move()
            photon_geom = photon.geometry

            # ---- Collision detection ----
            for target in self.targets:
                if target.geometry.intersects(photon_geom):

                    target.reset_target(self.get_rand_startxy())

                    photons_to_remove.append(photon)
                    break  # stop checking other targets

        # ---- Remove photons safely ----
        for photon in photons_to_remove:
            if photon in self.laserphotons:
                self.laserphotons.remove(photon)
                photon.remove()
                
