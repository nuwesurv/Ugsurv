"""Plugin-wide snap mode state. Keeps QGIS snapping config in sync."""

from qgis.core import QgsProject, QgsSnappingConfig

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
    """Push current snap state into the QGIS project snapping config."""
    project = QgsProject.instance()
    if project is None:
        return
    cfg = project.snappingConfig()

    # Collect active QGIS snap types as actual enum values (not Python ints),
    # so that ORing them produces a valid QFlags for setType().
    active_types = []
    if _state[ENDPOINT]:
        active_types.append(QgsSnappingConfig.Vertex)
    if _state[NEAREST]:
        active_types.append(QgsSnappingConfig.Segment)
    mid_flag = getattr(QgsSnappingConfig, 'MiddleOfSegments', None)
    if mid_flag is not None and _state[MIDPOINT]:
        active_types.append(mid_flag)

    if active_types:
        cfg.setEnabled(True)
        combined = active_types[0]
        for t in active_types[1:]:
            combined = combined | t
        cfg.setType(combined)
    else:
        cfg.setEnabled(False)

    cfg.setIntersectionSnapping(_state[INTERSECTION])
    project.setSnappingConfig(cfg)
