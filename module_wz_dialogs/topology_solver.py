from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsPointLocator,
    QgsWkbTypes,
    QgsCoordinateTransform,
)
from qgis.gui import QgsMapTool, QgsMapToolIdentifyFeature, QgsRubberBand
from qgis.PyQt.QtGui import QIcon, QFont
from ..core import style as _style
import math


class TopologySolver(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, iface, cmd_dock):
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self._cmd_dock = cmd_dock
        self.cursor_points = []
        self.selected_geoms = []
        self.adj_feature_properties = {}
        self._picked_fids = set()
        self._maptool = None

        self.rubber_band1 = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band1.setColor(_style.RB_IDENTIFY_RED)
        self.rubber_band1.setWidth(_style.RB_IDENTIFY_WIDTH)
        self.rubber_band1.setLineStyle(_style.RB_LINE_STYLE)
        self.rubber_band1.setFillColor(_style.RB_IDENTIFY_RED_FILL)

        self.rubber_band2 = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band2.setColor(_style.RB_IDENTIFY_BLUE)
        self.rubber_band2.setWidth(_style.RB_IDENTIFY_WIDTH)
        self.rubber_band2.setLineStyle(_style.RB_LINE_STYLE)
        self.rubber_band2.setFillColor(_style.RB_IDENTIFY_BLUE_FILL)

    def _log(self, msg):
        self._cmd_dock.log(msg, '#aaddff')

    def showRubberBandPolygon(self, geometry, rubber_band):
        rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        rubber_band.addGeometry(geometry, None)
        rubber_band.show()

    def activate(self):
        super().activate()
        self.canvas.setFocus()

    def deactivate(self):
        if self._maptool:
            self._maptool.clear_tool()
        else:
            self.canvas.unsetMapTool(self)
            try:
                self._cmd_dock._input.setFocus()
            except Exception:
                pass

        self.rubber_band1.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band2.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

        self.cursor_points.clear()
        self.selected_geoms.clear()
        self.adj_feature_properties = {}
        self._picked_fids.clear()

        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.deactivate()

    def canvasMoveEvent(self, event):
        pass

    def canvasPressEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.RightButton:
                self.solveTopology()

                self.rubber_band1.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
                self.rubber_band2.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

                self.cursor_points.clear()
                self.selected_geoms.clear()
                self.adj_feature_properties = {}
                self._picked_fids.clear()

                self._cmd_dock.log('─' * 40, '#555555')
                return

            if event.button() == Qt.MouseButton.LeftButton:
                point = self.toMapCoordinates(event.pos())

                active_layer = self.iface.activeLayer()
                if not active_layer:
                    self._log('No active layer selected in the Layers panel.')
                    return

                results = self.identify(
                    event.x(),
                    event.y(),
                    [active_layer],
                    QgsMapToolIdentifyFeature.IdentifyMode.TopDownAll,
                )

                if len(results) > 1:
                    results = [results[0]]

                if results:
                    feature = results[0].mFeature
                    feat_layer = results[0].mLayer
                    fid_key = (feat_layer.id(), feature.id())
                    if fid_key in self._picked_fids:
                        return
                    self._picked_fids.add(fid_key)
                    self.cursor_points.append(point)
                    geom = QgsGeometry(feature.geometry())
                    project_crs = QgsProject.instance().crs()
                    feat_crs = feat_layer.crs()
                    if feat_crs != project_crs:
                        transform = QgsCoordinateTransform(feat_crs, project_crs, QgsProject.instance())
                        geom.transform(transform)
                    self.selected_geoms.append(geom)
                    if len(self.selected_geoms) == 1:
                        self.adj_feature_properties['layer'] = feat_layer.name()
                        self.adj_feature_properties['fid'] = feature.id()
                        self.showRubberBandPolygon(geom, self.rubber_band1)
                    else:
                        merged_geom = QgsGeometry.unaryUnion(self.selected_geoms[1:])
                        self.showRubberBandPolygon(merged_geom, self.rubber_band2)
                else:
                    self._log('No feature detected.')

                self._log(f'Feature {len(self.cursor_points)}: {results[0].mLayer.name()}')

        except Exception as e:
            self._log(f'Error: {e}')

    def solveTopology(self):
        if len(self.selected_geoms) <= 1:
            self._log('Select at least two features!')
            return

        layer_name = self.adj_feature_properties['layer']
        fid = self.adj_feature_properties['fid']
        layers = QgsProject.instance().mapLayersByName(layer_name)

        if layers:
            layer = layers[0]
        else:
            self._log('Selected layer not found!')
            return

        adj_feature = layer.getFeature(fid).geometry()
        project_crs = QgsProject.instance().crs()
        layer_crs = layer.crs()
        if layer_crs != project_crs:
            to_project = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())
            adj_feature.transform(to_project)

        other_features = self.selected_geoms[1:]
        merged_features = QgsGeometry.unaryUnion(other_features)

        adj_feature1 = adj_feature.difference(merged_features)
        if adj_feature1.isEmpty():
            adj_feature1 = adj_feature

        union = adj_feature1.combine(merged_features)

        hole_geoms = []
        if not union.isMultipart():
            polygons = union.asPolygon()
            inner_rings = polygons[1:]
            for ring in inner_rings:
                hole_geom = QgsGeometry.fromPolygonXY([ring])
                hole_geoms.append(hole_geom)

        if hole_geoms:
            merged_gaps = QgsGeometry.unaryUnion(hole_geoms)
            adj_feature2 = adj_feature1.combine(merged_gaps)
        else:
            distance = adj_feature1.distance(merged_features)
            if distance > 0:
                close_dist = distance + 0.001
                combined = QgsGeometry.unaryUnion([adj_feature1, merged_features])
                closed = combined.buffer(close_dist, 16).buffer(-close_dist, 16)
                if not closed.isEmpty():
                    candidate = closed.difference(merged_features)
                    adj_feature2 = candidate if not candidate.isEmpty() else adj_feature1
                else:
                    adj_feature2 = adj_feature1
            else:
                adj_feature2 = adj_feature1

        adj_feature2 = adj_feature2.makeValid()
        adj_feature2 = adj_feature2.simplify(0.001)

        adj_feature3 = self.snap_function(adj_feature2, merged_features)

        if layer_crs != project_crs:
            to_layer = QgsCoordinateTransform(project_crs, layer_crs, QgsProject.instance())
            adj_feature3.transform(to_layer)

        layer.startEditing()
        layer.beginEditCommand("Fix parcel topology")
        layer.changeGeometry(self.adj_feature_properties['fid'], adj_feature3)
        layer.endEditCommand()
        layer.triggerRepaint()

    def snap_function(self, geom1: QgsGeometry, ref_geom: QgsGeometry, tolerance=0.1):
        geom1 = QgsGeometry(geom1)

        ref_vertices = []
        if ref_geom.isMultipart():
            for ring_group in ref_geom.asMultiPolygon():
                ref_vertices.extend(ring_group[0])
        else:
            poly = ref_geom.asPolygon()
            if poly:
                ref_vertices = poly[0]

        for ref_qpoint in ref_vertices:
            dist, closest_pt, after_vertex, _ = geom1.closestSegmentWithContext(ref_qpoint)
            if dist <= tolerance ** 2:
                vertex_id = geom1.closestVertex(ref_qpoint)[1]
                vertex_point = geom1.closestVertex(ref_qpoint)[0]
                if vertex_point.distance(ref_qpoint) <= tolerance:
                    geom1.moveVertex(ref_qpoint.x(), ref_qpoint.y(), vertex_id)
                else:
                    geom1.insertVertex(closest_pt.x(), closest_pt.y(), after_vertex)
                    geom1.moveVertex(ref_qpoint.x(), ref_qpoint.y(), after_vertex)

        adj_poly = geom1.asPolygon()
        if adj_poly:
            for vertex_id, adj_point in enumerate(adj_poly[0]):
                dist, closest_pt, _, _ = ref_geom.closestSegmentWithContext(adj_point)
                if dist <= tolerance ** 2:
                    geom1.moveVertex(closest_pt.x(), closest_pt.y(), vertex_id)

        return geom1
