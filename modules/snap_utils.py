"""Shared snap helpers used by all manipulating tools."""

from qgis.core import (
    QgsGeometry, QgsPointXY, QgsProject, QgsRectangle,
    QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtGui import QColor
from . import snap_manager

_CC_COLOR  = QColor(66, 135, 245)
_CC_SIZE   = 14
_CC_PEN_W  = 2
_SNAP_PX   = 20   # pixel radius for all custom snap types

# Marker icon for each snap type — used by all tools to set the snap indicator
SNAP_ICON = {
    'endpoint':     QgsVertexMarker.ICON_BOX,
    'center':       QgsVertexMarker.ICON_CROSS,
    'midpoint':     QgsVertexMarker.ICON_TRIANGLE,
    'intersection': QgsVertexMarker.ICON_X,
    'nearest':      QgsVertexMarker.ICON_DOUBLE_TRIANGLE,
}


def _tol(canvas):
    return _SNAP_PX * canvas.mapUnitsPerPixel()


def _spatial_layers():
    for lyr in QgsProject.instance().mapLayers().values():
        if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial():
            yield lyr


def _non_point_layers():
    for lyr in _spatial_layers():
        gt = QgsWkbTypes.geometryType(lyr.wkbType())
        if gt in (QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry):
            yield lyr


def find_endpoint_snap(canvas, map_pt):
    """Return nearest vertex of any feature within snap tolerance, or None."""
    if not snap_manager.is_enabled(snap_manager.ENDPOINT):
        return None
    tol = _tol(canvas)
    rect = QgsRectangle(map_pt.x() - tol, map_pt.y() - tol,
                        map_pt.x() + tol, map_pt.y() + tol)
    best_pt, best_dist = None, tol
    for lyr in _spatial_layers():
        for feat in lyr.getFeatures(rect):
            geom = feat.geometry()
            if geom.isNull() or geom.isEmpty():
                continue
            for v in geom.vertices():
                pt = QgsPointXY(v.x(), v.y())
                dist = map_pt.distance(pt)
                if dist < best_dist:
                    best_dist = dist
                    best_pt = pt
    return best_pt


def find_circle_center_snap(canvas, map_pt):
    """Return center QgsPointXY of the nearest _circles feature within snap tolerance, or None."""
    if not snap_manager.is_enabled(snap_manager.CENTER):
        return None
    tol = _tol(canvas)
    best_center, best_dist = None, tol
    for lyr in QgsProject.instance().mapLayers().values():
        if not isinstance(lyr, QgsVectorLayer) or lyr.name() != "_circles":
            continue
        for feat in lyr.getFeatures():
            geom = feat.geometry()
            if geom.isNull() or geom.isEmpty():
                continue
            bbox = geom.boundingBox()
            if bbox.isEmpty():
                continue
            c = bbox.center()
            center = QgsPointXY(c.x(), c.y())
            dist = map_pt.distance(center)
            if dist < best_dist:
                best_dist = dist
                best_center = center
    return best_center


def find_midpoint_snap(canvas, map_pt):
    """Return nearest segment midpoint within snap tolerance, or None."""
    if not snap_manager.is_enabled(snap_manager.MIDPOINT):
        return None
    tol = _tol(canvas)
    # 2× radius: midpoint can lie outside the cursor circle while its segment passes through
    rect = QgsRectangle(map_pt.x() - 2*tol, map_pt.y() - 2*tol,
                        map_pt.x() + 2*tol, map_pt.y() + 2*tol)
    best_pt, best_dist = None, tol
    for lyr in _non_point_layers():
        for feat in lyr.getFeatures(rect):
            geom = feat.geometry()
            if geom.isNull() or geom.isEmpty():
                continue
            verts = list(geom.vertices())
            for i in range(len(verts) - 1):
                mx = (verts[i].x() + verts[i+1].x()) / 2
                my = (verts[i].y() + verts[i+1].y()) / 2
                mid = QgsPointXY(mx, my)
                dist = map_pt.distance(mid)
                if dist < best_dist:
                    best_dist = dist
                    best_pt = mid
    return best_pt


def find_nearest_snap(canvas, map_pt):
    """Return nearest point on any edge within snap tolerance, or None."""
    if not snap_manager.is_enabled(snap_manager.NEAREST):
        return None
    tol = _tol(canvas)
    rect = QgsRectangle(map_pt.x() - tol, map_pt.y() - tol,
                        map_pt.x() + tol, map_pt.y() + tol)
    best_pt, best_dist = None, tol
    pt_geom = QgsGeometry.fromPointXY(map_pt)
    for lyr in _non_point_layers():
        for feat in lyr.getFeatures(rect):
            geom = feat.geometry()
            if geom.isNull() or geom.isEmpty():
                continue
            nearest = geom.nearestPoint(pt_geom)
            if nearest.isNull() or nearest.isEmpty():
                continue
            pt = nearest.asPoint()
            dist = map_pt.distance(pt)
            if dist < best_dist:
                best_dist = dist
                best_pt = pt
    return best_pt


def find_intersection_snap(canvas, map_pt):
    """Return nearest feature-to-feature intersection within snap tolerance, or None."""
    if not snap_manager.is_enabled(snap_manager.INTERSECTION):
        return None
    tol = _tol(canvas)
    rect = QgsRectangle(map_pt.x() - 2*tol, map_pt.y() - 2*tol,
                        map_pt.x() + 2*tol, map_pt.y() + 2*tol)
    candidates = []
    for lyr in _non_point_layers():
        for feat in lyr.getFeatures(rect):
            geom = feat.geometry()
            if not geom.isNull() and not geom.isEmpty():
                candidates.append(geom)
    best_pt, best_dist = None, tol
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            inter = candidates[i].intersection(candidates[j])
            if inter.isNull() or inter.isEmpty():
                continue
            for v in inter.vertices():
                pt = QgsPointXY(v.x(), v.y())
                dist = map_pt.distance(pt)
                if dist < best_dist:
                    best_dist = dist
                    best_pt = pt
    return best_pt


def snap_point(canvas, map_pt):
    """Apply the full priority chain and return (snapped_pt, icon).

    Priority: endpoint > circle_center > midpoint > intersection > nearest
    Returns (map_pt, None) when no snap fires.
    """
    pt = find_endpoint_snap(canvas, map_pt)
    if pt:
        return pt, SNAP_ICON['endpoint']

    pt = find_circle_center_snap(canvas, map_pt)
    if pt:
        return pt, SNAP_ICON['center']

    pt = find_midpoint_snap(canvas, map_pt)
    if pt:
        return pt, SNAP_ICON['midpoint']

    pt = find_intersection_snap(canvas, map_pt)
    if pt:
        return pt, SNAP_ICON['intersection']

    pt = find_nearest_snap(canvas, map_pt)
    if pt:
        return pt, SNAP_ICON['nearest']

    return map_pt, None


def make_cc_marker(canvas):
    """Create and return a circle-center snap marker (blue cross, hidden by default)."""
    m = QgsVertexMarker(canvas)
    m.setColor(_CC_COLOR)
    m.setIconType(QgsVertexMarker.ICON_CROSS)
    m.setIconSize(_CC_SIZE)
    m.setPenWidth(_CC_PEN_W)
    m.setVisible(False)
    return m


def rm_cc_marker(canvas, marker):
    """Remove a circle-center snap marker from the canvas scene."""
    if marker is not None:
        try:
            canvas.scene().removeItem(marker)
        except Exception:
            pass
