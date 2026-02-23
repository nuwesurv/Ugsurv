from qgis.core import QgsProject, QgsSnappingConfig, QgsTolerance

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