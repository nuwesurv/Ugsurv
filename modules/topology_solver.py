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
    QgsCoordinateTransform
)
from qgis.gui import QgsMapTool, QgsMapToolIdentifyFeature, QgsRubberBand
from qgis.PyQt.QtGui import QIcon, QFont, QColor
import math


class TopologySolver(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, iface, terminal_dock):
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self.cursor_points = []
        self.selected_geoms = []
        self.adj_feature_properties = {}
        # self.s_layer = self.getSampleLayer()
        
        # Create rubber bands
        # Style the rubberband
        self.rubber_band1 = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band1.setColor(QColor(255, 0, 0))  # Green
        self.rubber_band1.setWidth(2)
        self.rubber_band1.setLineStyle(Qt.DashLine)
        self.rubber_band1.setFillColor(QColor(255, 0, 0, 10))
        
        # Style the rubberband
        self.rubber_band2 = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band2.setColor(QColor(0, 0, 255))  # Blue
        self.rubber_band2.setWidth(2)
        self.rubber_band2.setLineStyle(Qt.DashLine)
        self.rubber_band2.setFillColor(QColor(0, 0, 255, 10))
        
        
    def showRubberBandPolygon(self, geometry, rubber_band):
        rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        rubber_band.addGeometry(geometry, None)
        rubber_band.show()
        
        
        
    def activate(self):
        super().activate()
        self.canvas.setFocus()
        
    def deactivate(self):
        self.canvas.unsetMapTool(self)
        self.terminal_dock.command.setFocus()
        
        # Remove rubberbands
        self.rubber_band1.reset(QgsWkbTypes.PolygonGeometry)
        self.rubber_band2.reset(QgsWkbTypes.PolygonGeometry)
        
        # Clean variables
        self.cursor_points.clear()
        self.selected_geoms.clear()
        self.adj_feature_properties = {}
        
        # Hide snap marker and clear state
        self.cursor_points.clear()
        self.terminal_dock.commandDisplay.setText(
            self.terminal_dock.commandOutputText + "\n...\n"
        )
        # Call parent
        super().deactivate()
        
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.deactivate()
            
            
            
            

    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        
        if len(self.cursor_points) == 0:
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect adjust feature:\n'
            )
        elif len(self.cursor_points) >= 1:
            # Clear it
            self.terminal_dock.commandDisplay.setText(
                self.terminal_dock.commandOutputText + f'\nSelect reference feature no:{len(self.cursor_points)+1}\n'
            )
        
        
        
    def canvasPressEvent(self, event):
        try:
            if event.button() == Qt.RightButton:
                self.solveTopology()
                # self.solveTopology()
                
                # Reset the rubberbands
                self.rubber_band1.reset(QgsWkbTypes.PolygonGeometry)
                self.rubber_band2.reset(QgsWkbTypes.PolygonGeometry)
                
                # Clean variables
                self.cursor_points.clear()
                self.selected_geoms.clear()
                self.adj_feature_properties = {}
                
                
                self.terminal_dock.commandOutputText += f'\n------Next >>>'
                self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                return


            if event.button() == Qt.LeftButton:
                point = self.toMapCoordinates(event.pos())
                # Call identify from parent class
                results = self.identify(
                    event.x(),
                    event.y(),
                    [layer for layer in QgsProject.instance().mapLayers().values()],
                    QgsMapToolIdentifyFeature.TopDownAll
                )
                
                # if the selected are greater htan 1 notify user.
                if len(results)>1:
                    self.terminal_dock.commandOutputText += f'\nMore than 1 feature was selected...'
                    self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                    return
                
                if results:
                    self.cursor_points.append(point)
                    feature = results[0].mFeature
                    feat_layer = results[0].mLayer
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
                    self.terminal_dock.commandOutputText += f'\nNo feature detected?'
                    self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                    
                
                self.terminal_dock.commandOutputText += f'\nFeature{len(self.cursor_points)}: {results[0].mLayer.name()}'
                self.terminal_dock.commandDisplay.setText(
                    self.terminal_dock.commandOutputText + f'\nSelect reference feature no:{len(self.cursor_points)+1}\n'
                )
        except Exception as e:
            self.terminal_dock.commandOutputText += f'\nExperienced error: {e}'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
                
                
                
                
                
    def solveTopology(self):
        if self.selected_geoms.__len__() <= 1:
            self.terminal_dock.commandOutputText += f'\nSelect atleast two features!'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
            return
        
        # Setup the adj_feature
        # adj_feature = self.selected_geoms[0]
        layer_name = self.adj_feature_properties['layer']
        fid = self.adj_feature_properties['fid']
        layers = QgsProject.instance().mapLayersByName(layer_name)

        if layers:
            layer =  layers[0]
        else:
            self.terminal_dock.commandOutputText += f'\nSelect layer not found!'
            self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)
            return
        adj_feature = layer.getFeature(fid).geometry()
        project_crs = QgsProject.instance().crs()
        layer_crs = layer.crs()
        if layer_crs != project_crs:
            to_project = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())
            adj_feature.transform(to_project)

        # Merge all other features (already in project CRS)
        other_features = self.selected_geoms[1:]
        merged_features = QgsGeometry.unaryUnion(other_features)
        
        # Step 1# ========================================
        # Solve overlaps
        adj_feature1 = adj_feature.difference(merged_features)
        if adj_feature1.isEmpty():
            # Nothing was subtracted
            adj_feature1 = adj_feature 
        
        # Step 2#  =======================================
        # Solve gaps
        union = adj_feature1.combine(merged_features)

        hole_geoms = []
        if not union.isMultipart():  # single polygon — gap appears as interior ring
            polygons = union.asPolygon()
            inner_rings = polygons[1:]
            for ring in inner_rings:
                hole_geom = QgsGeometry.fromPolygonXY([ring])
                hole_geoms.append(hole_geom)

        if hole_geoms:
            merged_gaps = QgsGeometry.unaryUnion(hole_geoms)
            adj_feature2 = adj_feature1.combine(merged_gaps)
        else:
            # Features have a gap along their boundary (union is multipart or no inner rings).
            # Bridge the gap by buffering both features and filling the zone between them.
            distance = adj_feature1.distance(merged_features)
            if distance > 0:
                bridge = distance * 1.5
                adj_buf = adj_feature1.buffer(bridge, 16)
                ref_buf = merged_features.buffer(bridge, 16)
                gap_zone = adj_buf.intersection(ref_buf)
                gap_zone = gap_zone.difference(merged_features)
                if not gap_zone.isEmpty():
                    adj_feature2 = adj_feature1.combine(gap_zone)
                else:
                    adj_feature2 = adj_feature1
            else:
                adj_feature2 = adj_feature1
        
        # Clean up the Geometries.
        adj_feature2 = adj_feature2.makeValid()
        adj_feature2 = adj_feature2.simplify(0.001)
        
        # Step 2#  =======================================
        # Solve topological relation.
        # i want to use qgis processing to snap adj_feature2 to merged_features
        adj_feature3 = self.snap_function(adj_feature2, merged_features)

        # Transform result back to the layer's native CRS before saving
        if layer_crs != project_crs:
            to_layer = QgsCoordinateTransform(project_crs, layer_crs, QgsProject.instance())
            adj_feature3.transform(to_layer)

        layer.startEditing()
        layer.beginEditCommand("Fix parcel topology")
        
        layer.changeGeometry(self.adj_feature_properties['fid'], adj_feature3)
        layer.endEditCommand()
        layer.triggerRepaint()
        
            
        
        
        # new_feature = QgsFeature(self.s_layer.fields())
        # new_feature.setGeometry(adj_feature2)

        # self.s_layer.startEditing()
        # self.s_layer.addFeature(new_feature)
        # self.s_layer.triggerRepaint()
        
        
    # def getSampleLayer(self):
    #     layer_name = "sample_layer"
    #     layers = QgsProject.instance().mapLayersByName(layer_name)

    #     if layers:
    #         return layers[0]

    #     self.s_layer = QgsVectorLayer("Polygon?crs=EPSG:32636", layer_name, "memory")
    #     QgsProject.instance().addMapLayer(self.s_layer)
        
    #     self.s_layer.triggerRepaint()
    #     self.s_layer.startEditing()

    #     return self.s_layer
    
    def snap_function(self, geom1: QgsGeometry, ref_geom: QgsGeometry, tolerance=0.1):
        geom1 = QgsGeometry(geom1)

        # Collect all exterior-ring vertices from reference (handles single and multipart)
        ref_vertices = []
        if ref_geom.isMultipart():
            for ring_group in ref_geom.asMultiPolygon():
                ref_vertices.extend(ring_group[0])
        else:
            poly = ref_geom.asPolygon()
            if poly:
                ref_vertices = poly[0]

        # Case: reference node near an adjustment segment → insert/move vertex
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

        # Case: adjustment vertex near a reference segment → move onto it
        adj_poly = geom1.asPolygon()
        if adj_poly:
            for vertex_id, adj_point in enumerate(adj_poly[0]):
                dist, closest_pt, _, _ = ref_geom.closestSegmentWithContext(adj_point)
                if dist <= tolerance ** 2:
                    geom1.moveVertex(closest_pt.x(), closest_pt.y(), vertex_id)

        return geom1
            
        