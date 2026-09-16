# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsWkbTypes
from qgis.gui import QgsMapToolIdentifyFeature, QgsRubberBand
from ..core import style as _style


class GeomPrintTool(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, iface, cmd_dock):
        super().__init__(canvas)
        self.iface     = iface
        self.canvas    = canvas
        self._cmd_dock = cmd_dock

        self._rb = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self._rb.setColor(_style.RB_IDENTIFY_RED)
        self._rb.setWidth(_style.RB_IDENTIFY_WIDTH)
        self._rb.setLineStyle(_style.RB_LINE_STYLE)
        self._rb.setFillColor(_style.RB_IDENTIFY_RED_FILL)

    def _log(self, msg, color='#aaddff'):
        self._cmd_dock.log(msg, color)

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        self._log('GEOM: click a feature to print its geometry.')

    def deactivate(self):
        self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        try:
            self._cmd_dock._input.setFocus()
        except Exception:
            pass
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.deactivate()

    def canvasMoveEvent(self, event):
        pass

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.deactivate()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        active_layer = self.iface.activeLayer()
        if not active_layer:
            self._log('No active layer selected in the Layers panel.')
            return

        results = self.identify(
            event.x(), event.y(),
            [active_layer],
            QgsMapToolIdentifyFeature.IdentifyMode.TopDownAll,
        )
        if not results:
            self._log('No feature detected.')
            return

        feature    = results[0].mFeature
        feat_layer = results[0].mLayer
        geom       = feature.geometry()

        self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        if geom and not geom.isEmpty():
            self._rb.addGeometry(geom, feat_layer)
            self._rb.show()

        self._log(f'Layer: {feat_layer.name()}  fid={feature.id()}', '#888888')
        self._log(geom.asWkt() if (geom and not geom.isEmpty()) else 'NULL', '#ffff88')
