from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor

import math
import random
import time

_rng = random.SystemRandom()

from qgis.PyQt.QtCore import QTimer

from .game_objects.Target import Target
from .game_objects.LaserPhoton import LaserPhoton


class Game1(QgsMapTool):

    MAX_PHOTONS = 5
    LIFE_DRAIN_PER_MISS = 5
    LIFE_HEAL_PER_HIT = 3
    KILLS_PER_WAVE = 5

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas = canvas

        crs = QgsCoordinateReferenceSystem("EPSG:32636")
        QgsProject.instance().setCrs(crs)

        self.game_extent = QgsRectangle(445200, 25200, 446800, 26200)
        cx = (self.game_extent.xMinimum() + self.game_extent.xMaximum()) / 2
        cy = self.game_extent.yMinimum() + 90
        self.telescope_center = QgsPointXY(cx, cy)

        self.terminal_dock = terminal_dock
        self._maptool = None  # set by UgsurvMaptool.set_tool()

        # Game state
        self.score = 0
        self.combo = 0
        self.wave = 0
        self.life_percent = 100
        self.game_start_time = time.time()
        self.is_playing = False

        self._base_speeds = [[0, -12], [0, -11], [0, -10]]

        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self.game_loop)
        self.animation_interval = 50

        self.targets = [
            Target('Target1', canvas, self._rand_start(), self.game_extent, list(self._base_speeds[0])),
            Target('Target2', canvas, self._rand_start(), self.game_extent, list(self._base_speeds[1])),
            Target('Target3', canvas, self._rand_start(), self.game_extent, list(self._base_speeds[2])),
        ]
        self.laserphotons = []

        # Rubber bands
        self.target_line = self._make_rb(QgsWkbTypes.GeometryType.LineGeometry, QColor(255, 80, 80), 1, Qt.PenStyle.DashLine)

        self.laser_telescope = self._make_rb(
            QgsWkbTypes.GeometryType.PolygonGeometry, QColor(60, 60, 60), 1, Qt.PenStyle.SolidLine, QColor(234, 182, 28))
        self.laser_telescope.setToGeometry(self._telescope_geom(0), None)

        self.ts_body = self._make_rb(
            QgsWkbTypes.GeometryType.PolygonGeometry, QColor(60, 60, 60), 1, Qt.PenStyle.SolidLine, QColor(234, 182, 28))
        self.ts_body.setToGeometry(self._tripod_geom(), None)

        # Life bar: dark boundary always at 100%, colored fill scales with life
        self.load_bar_bndry = self._make_rb(
            QgsWkbTypes.GeometryType.PolygonGeometry, QColor(80, 80, 80), 2, Qt.PenStyle.SolidLine, QColor(40, 40, 40))
        self.load_bar_bndry.setToGeometry(self._loadbar_geom(100), None)

        self.load_bar = self._make_rb(
            QgsWkbTypes.GeometryType.PolygonGeometry, QColor(0, 0, 0), 0, Qt.PenStyle.SolidLine, QColor(0, 200, 0))
        self.load_bar.setToGeometry(self._loadbar_geom(self.life_percent), None)

        self.zoom_to_extent()
        self._update_hud()
        self.start_game()

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _rand_start(self):
        x_dist = self.game_extent.xMaximum() - self.game_extent.xMinimum()
        margin = x_dist * 0.1
        return [
            _rng.randint(int(self.game_extent.xMinimum() + margin),
                         int(self.game_extent.xMaximum() - margin)),
            _rng.randint(int(self.game_extent.yMaximum()),
                         int(self.game_extent.yMaximum()) + 100),
        ]

    def zoom_to_extent(self):
        self.canvas.setExtent(self.game_extent)
        self.canvas.refresh()

    def _make_rb(self, geom_type, color, width=2, linestyle=Qt.PenStyle.SolidLine, fill_color=None):
        rb = QgsRubberBand(self.canvas, geom_type)
        rb.setColor(color)
        rb.setWidth(width)
        rb.setLineStyle(linestyle)
        if fill_color and geom_type == QgsWkbTypes.GeometryType.PolygonGeometry:
            rb.setFillColor(fill_color)
        return rb

    # ------------------------------------------------------------------
    # Game lifecycle
    # ------------------------------------------------------------------

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
        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            self.terminal_dock.command.setFocus()

        self.stop_game()

        for target in self.targets:
            target.remove()

        for photon in list(self.laserphotons):
            photon.remove()
        self.laserphotons.clear()

        self.laser_telescope.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.ts_body.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.load_bar.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.load_bar_bndry.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.target_line.reset(QgsWkbTypes.GeometryType.LineGeometry)

        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\n........\n"
        )
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.deactivate()

    # ------------------------------------------------------------------
    # Input events
    # ------------------------------------------------------------------

    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        cx, cy = self.telescope_center.x(), self.telescope_center.y()

        dx = point.x() - cx
        dy = point.y() - cy
        angle_rad = math.atan2(dy, dx)
        angle_deg = math.degrees(angle_rad)

        self.laser_telescope.setToGeometry(self._telescope_geom(angle_deg), None)

        max_dist = 0.3 * (self.game_extent.yMaximum() - self.game_extent.yMinimum())
        dist = math.hypot(dx, dy)

        if dist < max_dist:
            end = point
        else:
            end = QgsPointXY(
                cx + max_dist * math.cos(angle_rad),
                cy + max_dist * math.sin(angle_rad),
            )

        self.target_line.setToGeometry(
            QgsGeometry.fromPolylineXY([self.telescope_center, end]), None
        )

    def canvasPressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.is_playing:
            return
        if len(self.laserphotons) >= self.MAX_PHOTONS:
            return

        point = self.toMapCoordinates(event.pos())
        cx, cy = self.telescope_center.x(), self.telescope_center.y()
        angle_rad = math.atan2(point.y() - cy, point.x() - cx)

        speed = 200
        photon = LaserPhoton(
            f'photon_{len(self.laserphotons)}',
            self.canvas,
            angle_rad,
            [cx, cy],
            self.game_extent,
            [speed * math.cos(angle_rad), speed * math.sin(angle_rad)],
        )
        self.laserphotons.append(photon)

    # ------------------------------------------------------------------
    # Game loop
    # ------------------------------------------------------------------

    def game_loop(self):
        if not self.is_playing:
            return

        # Check wave advancement
        new_wave = self.score // self.KILLS_PER_WAVE
        if new_wave > self.wave:
            self.wave = new_wave
            self._advance_wave()

        # Move targets — game loop owns hit detection, not Target.move()
        for target in self.targets:
            if target.got_hit():
                target.reset_target(self._rand_start())
                self.combo = 0
                self._drain_life(self.LIFE_DRAIN_PER_MISS)
            else:
                target.move()

        # Move photons and detect collisions
        photons_to_remove = []
        for photon in self.laserphotons:
            if photon.got_hit():
                photons_to_remove.append(photon)
                continue
            photon.move()
            for target in self.targets:
                if target.geometry.intersects(photon.geometry):
                    self.combo += 1
                    bonus = 1 if self.combo % 3 == 0 else 0
                    self.score += 1 + bonus
                    self._heal_life(self.LIFE_HEAL_PER_HIT)
                    target.reset_target(self._rand_start())
                    photons_to_remove.append(photon)
                    break

        for photon in photons_to_remove:
            if photon in self.laserphotons:
                self.laserphotons.remove(photon)
                photon.remove()

        self._update_hud()

        if self.life_percent <= 0:
            self._game_over()

    # ------------------------------------------------------------------
    # Game state helpers
    # ------------------------------------------------------------------

    def _drain_life(self, amount):
        self.life_percent = max(0, self.life_percent - amount)
        self._update_life_bar()

    def _heal_life(self, amount):
        self.life_percent = min(100, self.life_percent + amount)
        self._update_life_bar()

    def _update_life_bar(self):
        self.load_bar.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.load_bar.setToGeometry(self._loadbar_geom(self.life_percent), None)

        if self.life_percent >= 60:
            color = QColor(0, 200, 0)
        elif self.life_percent >= 30:
            color = QColor(230, 150, 0)
        else:
            color = QColor(220, 30, 30)
        self.load_bar.setFillColor(color)

    def _update_hud(self):
        elapsed = time.time() - self.game_start_time
        combo_text = f'  Combo x{self.combo}!' if self.combo >= 3 else ''
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText +
            f'\n[GAME]  Score: {self.score}  |  Life: {self.life_percent}%'
            f'  |  Wave: {self.wave + 1}  |  Time: {elapsed:.0f}s{combo_text}'
        )

    def _advance_wave(self):
        speed_factor = 1.0 + self.wave * 0.2
        for i, target in enumerate(self.targets):
            base = self._base_speeds[i] if i < len(self._base_speeds) else [0, -10]
            target.dx = base[0] * speed_factor
            target.dy = base[1] * speed_factor

        # Wave 2: add a 4th target with slight horizontal drift
        if self.wave == 2:
            drift = _rng.choice([-4, 4])
            self.targets.append(
                Target('Target4', self.canvas, self._rand_start(), self.game_extent,
                       [drift * speed_factor, -13 * speed_factor],
                       fill_color=QColor(255, 220, 100))
            )
            self._base_speeds.append([drift, -13])
        # Wave 4: add a fast red target
        elif self.wave == 4:
            drift = _rng.choice([-6, 6])
            self.targets.append(
                Target('Target5', self.canvas, self._rand_start(), self.game_extent,
                       [drift * speed_factor, -16 * speed_factor],
                       color=QColor(200, 0, 0), fill_color=QColor(255, 80, 80))
            )
            self._base_speeds.append([drift, -16])

    def _game_over(self):
        self.stop_game()
        elapsed = time.time() - self.game_start_time
        self.terminal_dock.commandOutputText += (
            f'\n\n=== GAME OVER ==='
            f'\n  Final Score : {self.score}'
            f'\n  Waves reached: {self.wave + 1}'
            f'\n  Time played : {elapsed:.1f}s'
            f'\n  Press ESC to exit\n'
        )
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    # ------------------------------------------------------------------
    # Geometry builders
    # ------------------------------------------------------------------

    def _telescope_geom(self, angle_deg):
        cx, cy = self.telescope_center.x(), self.telescope_center.y()
        # Shape defined as absolute coords, rotated around telescope_center
        raw = [
            [446003.42405499, 25268.68404036],
            [445997.50022608, 25268.68404036],
            [445995.55378421, 25331.19801672],
            [445989.75348518, 25338.10263317],
            [445989.79051313, 25340.76929131],
            [446008.92065064, 25340.72457442],
            [446008.91068990, 25338.37221850],
            [446003.67999385, 25331.57095658],
            [446003.42405499, 25268.68404036],
        ]
        theta = math.radians(angle_deg - 90)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        points = [
            QgsPointXY(
                cx + (x - cx) * cos_t - (y - cy) * sin_t,
                cy + (x - cx) * sin_t + (y - cy) * cos_t,
            )
            for x, y in raw
        ]
        return QgsGeometry.fromPolygonXY([points])

    def _tripod_geom(self):
        raw = [
            [445963.51399173, 25197.54868380], [445965.16567908, 25201.75794921],
            [445965.68430881, 25204.50236733], [445987.33670142, 25262.97557123],
            [445997.02574071, 25263.11586315], [445997.07955320, 25268.64674084],
            [445987.93919486, 25271.04949167], [445988.01244651, 25309.12942074],
            [445996.08214266, 25314.22870051], [446003.60958979, 25314.27192537],
            [446012.16024465, 25309.34294491], [446012.08191969, 25270.97931997],
            [446012.03619763, 25270.72275193], [446011.87687251, 25270.62703314],
            [446003.87367360, 25268.65214897], [446003.99104053, 25263.14731279],
            [446013.05972311, 25263.25262177], [446035.84996936, 25203.22164890],
            [446036.06462272, 25201.09183481], [446036.92466792, 25199.11165294],
            [446035.70832808, 25200.89285010], [446034.01733238, 25202.34386831],
            [446006.70201909, 25246.69720692], [446001.81429554, 25204.44384669],
            [446000.90672308, 25201.02271243], [446000.83489212, 25196.01061782],
            [446000.76256249, 25200.87950266], [445999.80676498, 25204.31573366],
            [445994.54658823, 25246.72810158], [445967.08042148, 25203.74556169],
            [445965.42065535, 25201.72854004], [445963.51399173, 25197.54868380],
        ]
        return QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in raw]])

    def _loadbar_geom(self, percentage):
        padding = 20
        height = 20
        full_width = 600
        width = percentage * full_width / 100
        x0 = self.canvas.extent().xMinimum() + padding
        y_top = self.game_extent.yMaximum() - padding
        y_bot = y_top - height
        points = [
            QgsPointXY(x0, y_top),
            QgsPointXY(x0 + width, y_top),
            QgsPointXY(x0 + width, y_bot),
            QgsPointXY(x0, y_bot),
        ]
        return QgsGeometry.fromPolygonXY([points])
