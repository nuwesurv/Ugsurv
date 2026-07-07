"""Shared circle-center snap helpers used by all manipulating tools."""

from qgis.core import QgsPointXY, QgsProject, QgsVectorLayer, QgsPointLocator
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtGui import QColor
from . import snap_manager

_CC_COLOR   = QColor(66, 135, 245)
_CC_SIZE    = 14
_CC_PEN_W   = 2
_CC_SNAP_PX = 20

# Snap types that have higher priority than circle center
_HIGH_PRI_TYPES = {QgsPointLocator.Vertex}
_le = getattr(QgsPointLocator, 'LineEndpoint', None)
if _le is not None:
    _HIGH_PRI_TYPES.add(_le)


def find_circle_center_snap(canvas, map_pt):
    """Return center QgsPointXY of the nearest _circles feature within 20 px, or None.

    Returns None when center snap is disabled or when QGIS has a higher-priority
    vertex/endpoint snap active at map_pt (endpoint always beats circle center).
    Iterates all features (no spatial filter) so edit-buffer features are included.
    """
    if not snap_manager.is_enabled(snap_manager.CENTER):
        return None
    # Endpoint has higher priority — let QGIS vertex snap win
    qgis_match = canvas.snappingUtils().snapToMap(map_pt)
    if qgis_match.isValid() and qgis_match.type() in _HIGH_PRI_TYPES:
        return None
    tol = _CC_SNAP_PX * canvas.mapUnitsPerPixel()
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
