"""Plugin-wide snap mode state."""

from qgis.core import QgsProject

ENDPOINT     = 'endpoint'
MIDPOINT     = 'midpoint'
CENTER       = 'center'
INTERSECTION = 'intersection'
NEAREST      = 'nearest'

_state = {
    ENDPOINT:     True,
    MIDPOINT:     True,
    CENTER:       True,
    INTERSECTION: True,
    NEAREST:      True,
}


def is_enabled(key):
    return _state.get(key, False)


def set_enabled(key, value):
    _state[key] = bool(value)
    _apply_to_qgis()


def _apply_to_qgis():
    """Disable QGIS native snapping — all snapping is handled by snap_utils."""
    project = QgsProject.instance()
    if project is None:
        return
    cfg = project.snappingConfig()
    cfg.setEnabled(False)
    project.setSnappingConfig(cfg)
