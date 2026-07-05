from qgis.core import QgsProject

_GROUP_NAME = "UgSurv"


def add_to_plugin_group(layer):
    """Add a layer to the UgSurv layer group (created at top of tree if absent)."""
    root = QgsProject.instance().layerTreeRoot()
    group = root.findGroup(_GROUP_NAME)
    if group is None:
        group = root.insertGroup(0, _GROUP_NAME)
    QgsProject.instance().addMapLayer(layer, False)
    group.addLayer(layer)
