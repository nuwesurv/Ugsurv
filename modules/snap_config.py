from qgis.core import QgsProject, QgsSnappingConfig, QgsTolerance
from qgis.core import QgsSettings
from qgis.PyQt.QtGui import QColor

_SNAP_COLOR = QColor(66, 135, 245)

def snapSettingConfig():
    '''
    Sets up base snapping infrastructure (mode, tolerance, self-snap), then
    delegates type flags to snap_manager so user choices from the toolbar are
    preserved across tool activations.
    '''
    project = QgsProject.instance()
    snapping_config = project.snappingConfig()
    snapping_config.setEnabled(True)
    snapping_config.setMode(QgsSnappingConfig.AllLayers)
    snapping_config.setSelfSnapping(True)
    if snapping_config.tolerance() < 12:
        snapping_config.setTolerance(12)
        snapping_config.setUnits(QgsTolerance.Pixels)
    project.setSnappingConfig(snapping_config)

    # Let snap_manager apply the user-selected type flags and intersection setting
    from . import snap_manager
    snap_manager._apply_to_qgis()

    QgsSettings().setValue("/qgis/digitizing/snap_color", _SNAP_COLOR)