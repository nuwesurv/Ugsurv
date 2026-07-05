import os
from qgis.core import (
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsCoordinateTransformContext,
)

_DATA_DIR  = r"C:\UgSurv"
_GPKG_PATH = os.path.join(_DATA_DIR, "ugsurv_layers.gpkg")
_GROUP_NAME = "UgSurv"


def _ensure_dir():
    os.makedirs(_DATA_DIR, exist_ok=True)


def add_to_plugin_group(layer):
    """Add a layer to the UgSurv group at the top of the layer tree (created if absent)."""
    root = QgsProject.instance().layerTreeRoot()
    group = root.findGroup(_GROUP_NAME)
    if group is None:
        group = root.insertGroup(0, _GROUP_NAME)
    QgsProject.instance().addMapLayer(layer, False)
    group.addLayer(layer)


def open_layer_from_gpkg(layer_name):
    """Return a file-backed layer from the plugin GPKG, or None if it doesn't exist yet."""
    if not os.path.exists(_GPKG_PATH):
        return None
    uri = f"{_GPKG_PATH}|layername={layer_name}"
    lyr = QgsVectorLayer(uri, layer_name, "ogr")
    return lyr if lyr.isValid() else None


def create_layer_in_gpkg(mem_layer):
    """
    Write a memory layer's schema to the plugin GPKG and return the file-backed layer.
    Falls back to the original memory layer if the write fails (e.g. unsupported geom type).
    """
    _ensure_dir()
    layer_name = mem_layer.name()

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName   = "GPKG"
    options.layerName    = layer_name
    options.fileEncoding = "UTF-8"
    options.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteLayer
        if os.path.exists(_GPKG_PATH)
        else QgsVectorFileWriter.CreateOrOverwriteFile
    )

    error, _msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem_layer,
        _GPKG_PATH,
        QgsCoordinateTransformContext(),
        options,
    )

    if error != QgsVectorFileWriter.NoError:
        return mem_layer  # fallback to memory layer

    uri = f"{_GPKG_PATH}|layername={layer_name}"
    file_lyr = QgsVectorLayer(uri, layer_name, "ogr")
    return file_lyr if file_lyr.isValid() else mem_layer
