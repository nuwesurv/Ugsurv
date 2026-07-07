"""Shared circle-center snap helpers used by all manipulating tools."""

from qgis.core import QgsPointXY, QgsProject, QgsVectorLayer
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtGui import QColor

_CC_COLOR   = QColor(66, 135, 245)
_CC_SIZE    = 14
_CC_PEN_W   = 2
_CC_SNAP_PX = 20


def find_circle_center_snap(canvas, map_pt):
    """Return center QgsPointXY of the nearest _circles feature within 20 px, or None.

    Iterates all features (no spatial filter) so edit-buffer features are included.
    Uses bounding-box centre which is always correct for circular geometry types.
    """
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
