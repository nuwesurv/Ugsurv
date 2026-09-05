# -*- coding: utf-8 -*-
"""
AutoPointTool — places labelled points at every vertex of a clicked polyline.

Command : APOINT  /  APT
Flow    :
  1. Activate  → dynamic input shows free-text "Prefix" field (placeholder "pt")
  2. Type prefix + Enter  → accepts any text  (default "pt" on plain Enter)
  3. Click a polyline     → points created at every vertex, labelled prefix0, prefix1, …
  4. Tool goes home.
"""

from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsPointXY,
    QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class AutoPointTool(BaseTool):
    CURSOR = Qt.CursorShape.CrossCursor

    def activate(self):
        super().activate()
        self._prefix = "pt"
        self._awaiting_prefix = True
        self._transition(ToolState.ACTING)
        self._request_input("text", "Prefix:")

    def _on_event(self, sem: SemanticEvent):
        if self._awaiting_prefix:
            if sem.type == EventType.VALUE_ENTERED:
                # textEntered routes here with value as str;
                # valueEntered (numeric) also arrives here — both become the prefix
                v = sem.value
                self._prefix = v.strip() if isinstance(v, str) else _clean_num(v)
                self._prefix = self._prefix or "pt"
                self._awaiting_prefix = False
                self._request_input("no_value", f"Click polyline  [{self._prefix}]:")

            elif sem.type == EventType.CONFIRM:
                # plain Enter with no text → keep "pt"
                self._awaiting_prefix = False
                self._request_input("no_value", "Click polyline  [pt]:")

            elif sem.type == EventType.POINT_PICKED and sem.point:
                # direct click → skip prefix prompt, use default
                self._awaiting_prefix = False
                self._place_points(sem.point)
                self._go_home()

        else:
            if sem.type == EventType.POINT_PICKED and sem.point:
                self._place_points(sem.point)
                self._go_home()
            elif sem.type == EventType.CONFIRM:
                self._go_home()

    def _on_hover(self, sem: SemanticEvent):
        pass

    # ── core logic ────────────────────────────────────────────────────────

    def _place_points(self, map_pt: QgsPointXY):
        tolerance = self.canvas().mapUnitsPerPixel() * 15
        geom = self._find_nearest_line(map_pt, tolerance)
        if geom is None:
            self._log("No polyline found at click. Make sure a line layer is active.", '#ffaaaa')
            return

        vertices  = self._extract_vertices(geom)
        prefix    = self._prefix
        cad_layer = self._ctx.active_cad_layer or "0"
        sm        = self._ctx.storage_manager

        placed = 0
        skipped = 0
        for i, pt in enumerate(vertices):
            if self._point_exists(pt, sm):
                skipped += 1
                continue
            pt_geom = QgsGeometry.fromPointXY(pt)
            pt_geom.convertToMultiType()
            sm.add_point(pt_geom, cad_layer, description=f"{prefix}{i + 1}")
            placed += 1

        parts = [f"Placed {placed} point{'s' if placed != 1 else ''}"]
        if skipped:
            parts.append(f"skipped {skipped} (already exist)")
        n = len(vertices)
        self._log(
            "  ".join(parts) +
            f"  '{prefix}1' … '{prefix}{n}'  on cad_layer '{cad_layer}'  [not committed]",
        )

    def _point_exists(self, pt: QgsPointXY, sm) -> bool:
        """Return True if the points layer already contains a point within 1 cm of pt."""
        points_layer = sm.points_layer
        if points_layer is None:
            return False
        tol = 0.01  # 1 cm in layer CRS (UTM, metres)
        rect = QgsRectangle(pt.x() - tol, pt.y() - tol, pt.x() + tol, pt.y() + tol)
        req = QgsFeatureRequest().setFilterRect(rect)
        for _ in points_layer.getFeatures(req):
            return True
        return False

    def _find_nearest_line(self, map_pt: QgsPointXY, tolerance: float):
        project = QgsProject.instance()
        project_crs = project.crs()
        rect_project = QgsRectangle(
            map_pt.x() - tolerance, map_pt.y() - tolerance,
            map_pt.x() + tolerance, map_pt.y() + tolerance,
        )
        for layer in self._candidate_layers():
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.GeometryType.LineGeometry:
                continue
            # Transform rect from project CRS into the layer's own CRS
            layer_crs = layer.crs()
            if layer_crs != project_crs:
                xform = QgsCoordinateTransform(project_crs, layer_crs, project)
                try:
                    rect = xform.transformBoundingBox(rect_project)
                except Exception:
                    rect = rect_project
            else:
                rect = rect_project
            req = QgsFeatureRequest().setFilterRect(rect)
            for feat in layer.getFeatures(req):
                g = feat.geometry()
                if not g.isEmpty():
                    return g
        return None

    def _candidate_layers(self):
        layers = []
        iface = getattr(self._ctx, 'iface', None)
        if iface:
            active = iface.activeLayer()
            if isinstance(active, QgsVectorLayer):
                layers.append(active)
        sm = getattr(self._ctx, 'storage_manager', None)
        if sm is not None:
            lines_lyr = sm.lines_layer
            if lines_lyr is not None and lines_lyr not in layers:
                layers.append(lines_lyr)
        return layers

    @staticmethod
    def _extract_vertices(geom: QgsGeometry):
        if QgsWkbTypes.isMultiType(geom.wkbType()):
            lines = geom.asMultiPolyline()
        else:
            lines = [geom.asPolyline()]
        pts = []
        for line in lines:
            pts.extend(line)
        return pts

    def _log(self, msg: str, color: str = '#aaddff'):
        cmd_dock = getattr(self._ctx, 'cmd_dock', None)
        if cmd_dock:
            cmd_dock.log(msg, color)


def _clean_num(v) -> str:
    """Format a float prefix cleanly: 3.0 → '3', 3.5 → '3.5'."""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)
