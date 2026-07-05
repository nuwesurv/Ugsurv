from qgis.core import QgsProject, QgsSnappingConfig, QgsTolerance
from qgis.core import QgsSettings
from qgis.PyQt.QtGui import QColor

_SNAP_COLOR = QColor(66, 135, 245)

def snapSettingConfig():
    '''
    This function helps to setup the tolerance to be 12px min and also
    sets the types to nodes and Segments because its the minimum we require
    '''
    # Get the project instance
    project = QgsProject.instance()
    snapping_config = project.snappingConfig()
    # Ensure snapping is enabled
    snapping_config.setEnabled(True)
    # Set snapping mode to All Layers (or choose CurrentLayer / ActiveLayer if needed)
    snapping_config.setMode(QgsSnappingConfig.AllLayers)
    snapping_config.setIntersectionSnapping(True)
    snapping_config.setSelfSnapping(True)
    # Ensure the snapping type is Vertex and Segment
    current_type = snapping_config.type()
    if current_type != QgsSnappingConfig.VertexAndSegment:
        snapping_config.setType(QgsSnappingConfig.VertexAndSegment)

    # Access the current snapping configuration
    if snapping_config.tolerance() < 12:
        snapping_config.setTolerance(12)
        snapping_config.setUnits(QgsTolerance.Pixels)
    # Important: set it back to the project
    project.setSnappingConfig(snapping_config)

    # Set snap indicator color used by QgsSnapIndicator (read at construction time)
    QgsSettings().setValue("/qgis/digitizing/snap_color", _SNAP_COLOR)