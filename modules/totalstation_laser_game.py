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
from qgis.core import QgsProject, QgsCoordinateReferenceSystem
from PyQt5.QtCore import QVariant
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QIcon, QFont, QColor
from qgis.core import QgsTextAnnotation
from qgis.core import QgsPointXY
from qgis.PyQt.QtGui import QTextDocument
    
import math
import random
import time

from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtCore import QTimer

from .game_objects.Target import Target
from .game_objects.LaserPhoton import LaserPhoton




class Game1(QgsMapTool):

    def __init__(self, canvas, terminal_dock, operation_type):
        super().__init__(canvas)
        self.canvas = canvas
        extent = self.canvas.extent()
        # canvas_width = extent.width()
        # canvas_height = extent.height()

        # Set the coordinate sytem to 36N
        crs = QgsCoordinateReferenceSystem("EPSG:32636")
        QgsProject.instance().setCrs(crs)
        
        self.game_extent = QgsRectangle(445200, 25200, 446800, 26200)
        # self.game_extent = self.canvas.extent()
        self.telescope_center = QgsPointXY(self.game_extent.xMinimum() + (self.game_extent.xMaximum() - self.game_extent.xMinimum()) / 2, 
                                           self.game_extent.yMinimum() + 90)
        
        self.terminal_dock = terminal_dock
        self.operation_type = operation_type
        self.cursor_points = []
        
        # The game loop is here.
        self.life_percent = 100
        self.game_start_time = time.time()
        self.is_playing = False
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self.game_loop)
        self.animation_interval = 50  # milliseconds
        self.targets = [
                        Target('Target1' ,self.canvas, self.get_rand_startxy(), self.game_extent, [0, -12]),
                        Target('Target2', self.canvas, self.get_rand_startxy(), self.game_extent,[0,-11]),
                        Target('Target3', self.canvas, self.get_rand_startxy(), self.game_extent, [0, -10]),
                        ]
        self.laserphotons = []
        self.start_game()
        
        # Target line from the total station
        self.target_line = self.createRubberBand(QgsWkbTypes.LineGeometry, QColor(255,0,0), 0.5, Qt.DashLine)
        
        # Create the Telescope of the total station
        self.laser_telescope = self.createRubberBand(QgsWkbTypes.PolygonGeometry, QColor(1,1,1), 0.5, Qt.SolidLine, QColor(234,182,28))
        self.laser_telescope.setToGeometry(self.create_laser_telescope(0), None)
        # Create the ts_body of the total station
        self.ts_body = self.createRubberBand(QgsWkbTypes.PolygonGeometry, QColor(1,1,1), 0.5, Qt.SolidLine, QColor(234,182,28))
        self.ts_body.setToGeometry(self.create_ts_tripod(), None)
        # Create the loadbar_boundary of the game
        self.load_bar_bndry = self.createRubberBand(QgsWkbTypes.PolygonGeometry, QColor(1,1,1), 0.5, Qt.SolidLine, QColor(255,255,255))
        self.load_bar_bndry.setToGeometry(self.create_loadbar(self.life_percent), None)
        # Create the loadbar of the game
        self.load_bar = self.createRubberBand(QgsWkbTypes.PolygonGeometry, QColor(1,1,1), 0.5, Qt.SolidLine, QColor(234,182,28))
        self.load_bar.setToGeometry(self.create_loadbar(self.life_percent), None)

        # self.line_rb.setToGeometry(line, None)
        self.zoom_to_extent()
        
        
    def get_rand_startxy(self):
        x_dist = self.game_extent.xMaximum() -self.game_extent.xMinimum()
        return [
            random.randint(int(self.game_extent.xMinimum()+x_dist*0.2), int(self.game_extent.xMaximum() - x_dist*0.2)),
            random.randint(int(self.game_extent.yMaximum()), int(self.game_extent.yMaximum()) + 100)
        ]
    
    def zoom_to_extent(self):
        self.canvas.setExtent(self.game_extent)
        self.canvas.refresh()


        
    def createRubberBand(self, geom_type, color=QColor(255, 0, 0), width=2, linestyle=Qt.SolidLine, fill_color=None):
        rb = QgsRubberBand(self.canvas, geom_type)
        rb.setColor(color)
        rb.setWidth(width)
        rb.setLineStyle(linestyle)
        if fill_color and geom_type == QgsWkbTypes.PolygonGeometry:
            rb.setFillColor(fill_color)
        return rb
        
        
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
            if photon:
                self.laserphotons.remove(photon)
                photon.remove()
            
        self.laser_telescope.reset(QgsWkbTypes.LineGeometry)
        self.load_bar.reset(QgsWkbTypes.LineGeometry)
        self.load_bar_bndry.reset(QgsWkbTypes.LineGeometry)
        self.ts_body.reset(QgsWkbTypes.LineGeometry)
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
            speed = 200
            dx = speed * math.cos(angle_rad)
            dy = speed * math.sin(angle_rad)
            
            
            self.laserphotons.append(LaserPhoton(f'laser_photon{len(self.laserphotons)}' ,self.canvas, angle_rad, self.telescope_center, self.game_extent, [dx, dy]))
            
            
            
            
            
            
            
    # This is the game loop running the objects.
    def game_loop(self):
        if not self.is_playing:
            return
        # Update the time played
        duration = time.time() - self.game_start_time
        # self.update_text(self.timer_text, f"Time: {duration:.1f}s")
        print(duration)
        
        # Update the score.
        ...

        # ---- Move targets ----
        for target in self.targets:
            if target.got_hit():
                print(f"{target.name} has been hit")
                target.reset_target(self.get_rand_startxy())
                self.life_percent -= 4
                self.load_bar.reset(QgsWkbTypes.LineGeometry)
                self.load_bar.setToGeometry(self.create_loadbar(self.life_percent), None)
            else:
                
                # dx, dy = [0, -15]
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
                    self.life_percent = self.life_percent + 2 if self.life_percent+2 <= 100 else self.life_percent
                    self.load_bar.reset(QgsWkbTypes.LineGeometry)
                    self.load_bar.setToGeometry(self.create_loadbar(self.life_percent), None)
                    target.reset_target(self.get_rand_startxy())


        # ---- Remove photons safely ----
        for photon in photons_to_remove:
            if photon in self.laserphotons:
                self.laserphotons.remove(photon)
                photon.remove()
                
                
                
    
    
    
    
    
    
    
    
    
    
    def create_laser_telescope(self, angle):
        half_h = 10
        half_w = 70
        cx, cy = self.telescope_center.x(), self.telescope_center.y()
        # rectangle before rotation
        points = [
            [ 446003.42405499482993, 25268.684040363874374 ], 
            [ 445997.500226084317546, 25268.684040363874374 ], 
            [ 445995.553784213436302, 25331.19801672146059 ], 
            [ 445989.75348517607199, 25338.102633168495231 ], 
            [ 445989.790513134968933, 25340.769291311862617 ], 
            [ 446008.920650639745872, 25340.724574416995893 ], 
            [ 446008.910689904063474, 25338.372218503212935 ], 
            [ 446003.679993852449115, 25331.570956577463221 ], 
            [ 446003.42405499482993, 25268.684040363874374 ] 
            ]
        

        theta = math.radians(angle-90)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        rotated_points = []

        for x, y in points:
            xr = cx + (x-cx)*cos_t - (y-cy)*sin_t
            yr = cy + (x-cx)*sin_t + (y-cy)*cos_t
            rotated_points.append(QgsPointXY(xr, yr))

        poly = QgsGeometry.fromPolygonXY([rotated_points])
        return poly
    
    def create_ts_tripod(self):
        half_h = 10
        half_w = 70
        cx, cy = self.telescope_center.x(), self.telescope_center.y()
        # rectangle before rotation
        points = [ [ 445963.513991734071169, 25197.548683803292079 ], 
                  [ 445965.165679075347725, 25201.757949205726618 ], 
                  [ 445965.684308810101356, 25204.502367331140704 ], 
                  [ 445987.336701418564189, 25262.975571233815572 ], 
                  [ 445997.02574070985429, 25263.115863152175734 ], 
                  [ 445997.079553201445378, 25268.646740838783444 ], 
                  [ 445987.939194861857686, 25271.049491672125441 ], 
                  [ 445988.012446512060706, 25309.129420743272931 ], 
                  [ 445996.082142662373371, 25314.228700513787771 ], 
                  [ 446003.609589791449253, 25314.271925371216639 ], 
                  [ 446012.160244654107373, 25309.34294490569664 ], 
                  [ 446012.081919694959652, 25270.979319965346804 ], 
                  [ 446012.036197629699018, 25270.722751929984952 ], 
                  [ 446011.876872513443232, 25270.627033144286543 ], 
                  [ 446003.873673596885055, 25268.652148973658768 ], 
                  [ 446003.991040530090686, 25263.147312789180432 ], 
                  [ 446013.05972310929792, 25263.252621772644488 ], 
                  [ 446035.849969359929673, 25203.221648899689171 ], 
                  [ 446036.064622723206412, 25201.091834810071305 ], 
                  [ 446036.92466792446794, 25199.111652943192894 ], 
                  [ 446035.708328082866501, 25200.892850095428003 ], 
                  [ 446034.017332384828478, 25202.343868308053061 ], 
                  [ 446006.702019088028464, 25246.697206924676721 ], 
                  [ 446001.814295542193577, 25204.443846685378958 ], 
                  [ 446000.906723082938697, 25201.022712428231898 ], 
                  [ 446000.83489212411223, 25196.010617815190926 ], 
                  [ 446000.762562487157993, 25200.879502660860453 ], 
                  [ 445999.806764975422993, 25204.315733656825614 ], 
                  [ 445994.546588226803578, 25246.72810157789354 ], 
                  [ 445967.080421476333868, 25203.74556168676645 ], 
                  [ 445965.420655349094886, 25201.728540040901862 ], 
                  [ 445963.513991734071169, 25197.548683803292079 ] 
                  ]
        
        Qpoints = []
        for x, y in points:
            Qpoints.append(QgsPointXY(x, y))

        poly = QgsGeometry.fromPolygonXY([Qpoints])
        return poly
            
    def create_loadbar(self, percentage):
        # rectangle before rotation
        padding = 20
        height = 20
        standard_width = 600
        width = percentage * standard_width/100
        
        points = [ 
                  (self.canvas.extent().xMinimum()+ padding, self.game_extent.yMaximum()- padding),
                  (self.canvas.extent().xMinimum()+ width+ padding, self.game_extent.yMaximum()- padding),
                  (self.canvas.extent().xMinimum()+ width+ padding, self.game_extent.yMaximum()-height- padding),
                  (self.canvas.extent().xMinimum()+ padding, self.game_extent.yMaximum()-height- padding),
                  ]
        # print(points)
        Qpoints = []
        for x, y in points:
            Qpoints.append(QgsPointXY(x, y))

        poly = QgsGeometry.fromPolygonXY([Qpoints])
        return poly
        