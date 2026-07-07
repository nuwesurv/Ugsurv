import os
import math
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QColor
from qgis.core import (
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsCoordinateTransformContext,
    QgsGeometry,
    QgsPointXY,
    QgsRuleBasedRenderer,
    QgsLineSymbol,
    QgsMarkerSymbol,
)

def polyline_attrs(geom):
    """Return computed attribute dict for any polyline geometry.

    Safe to call on any LineString; fields that don't exist on a layer are
    simply skipped by the caller.  Always computes enclosed area — closing
    the ring virtually when the polyline is open.
    """
    length = geom.length()
    pts = geom.asPolyline()
    if len(pts) >= 2:
        p0, pn = pts[0], pts[-1]
        is_closed = (len(pts) >= 4
                     and abs(p0.x() - pn.x()) < 1e-9
                     and abs(p0.y() - pn.y()) < 1e-9)
        ring = list(pts)
        if abs(ring[0].x() - ring[-1].x()) > 1e-9 or abs(ring[0].y() - ring[-1].y()) > 1e-9:
            ring.append(QgsPointXY(ring[0].x(), ring[0].y()))
        area_sqm = QgsGeometry.fromPolygonXY([ring]).area()
    else:
        is_closed = False
        area_sqm  = 0.0
    area_acres = area_sqm * 0.000247105
    return {
        "length":     round(length, 3),
        "vertices":   len(pts),
        "closed":     is_closed,
        "area_sqm":   round(area_sqm, 3),
        "area_acres": round(area_acres, 6),
    }


def circle_attrs(cx, cy, radius):
    diameter      = radius * 2
    circumference = 2 * math.pi * radius
    area_sqm      = math.pi * radius ** 2
    area_acres    = area_sqm * 0.000247105
    return {
        "center_x":     round(cx, 3),
        "center_y":     round(cy, 3),
        "radius":        round(radius, 3),
        "diameter":      round(diameter, 3),
        "circumference": round(circumference, 3),
        "area_sqm":      round(area_sqm, 3),
        "area_acres":    round(area_acres, 6),
    }


_recalc_connected = set()  # layer IDs that already have the recalc signal wired up


def connect_polyline_recalc(layer):
    """Keep computed fields in sync with geometry for any tool that edits this layer.

    Two hooks:
    - geometryChanged  — updates attributes live in the edit buffer as each
                         geometry change is applied (vertex edit, move, offset, etc.).
    - beforeCommitChanges — guaranteed pass over every changed geometry right
                            before save, so attributes are always correct on disk.

    Safe to call multiple times — only wires up once per layer ID.
    """
    if layer.id() in _recalc_connected:
        return

    def _write_attrs(fid, geom):
        attrs = polyline_attrs(geom)
        for fname, val in attrs.items():
            idx = layer.fields().indexOf(fname)
            if idx >= 0:
                layer.changeAttributeValue(fid, idx, val)

    def _on_geom_changed(fid, geom):
        if not geom.isNull() and not geom.isEmpty():
            # Defer to the next event loop tick so the edit buffer has fully
            # settled before we write attributes (avoids silent reentrancy failures).
            QTimer.singleShot(0, lambda fid=fid, geom=geom: _write_attrs(fid, geom))

    def _before_commit():
        buf = layer.editBuffer()
        if buf is None:
            return
        for fid, geom in buf.changedGeometries().items():
            if not geom.isNull() and not geom.isEmpty():
                _write_attrs(fid, geom)

    layer.geometryChanged.connect(_on_geom_changed)
    layer.beforeCommitChanges.connect(_before_commit)
    _recalc_connected.add(layer.id())


def _color_rule_renderer(layer, make_symbol, default_color):
    """Build a QgsRuleBasedRenderer with one rule per unique color value in the layer."""
    color_idx = layer.fields().indexOf("color")
    colors = set()
    if color_idx >= 0:
        for feat in layer.getFeatures():
            val = feat.attribute(color_idx)
            if val:
                c = QColor(str(val))
                if c.isValid():
                    colors.add(str(val))
    if not colors:
        colors = {default_color}

    root = QgsRuleBasedRenderer.Rule(None)
    for color in sorted(colors):
        rule = QgsRuleBasedRenderer.Rule(make_symbol(color))
        rule.setFilterExpression(f'"color" = \'{color}\'')
        root.appendChild(rule)

    else_rule = QgsRuleBasedRenderer.Rule(make_symbol(default_color), elseRule=True)
    root.appendChild(else_rule)

    return QgsRuleBasedRenderer(root)


def apply_circle_color_renderer(layer):
    def sym(color):
        return QgsLineSymbol.createSimple({"color": color, "width": "0.4"})
    layer.setRenderer(_color_rule_renderer(layer, sym, "#E05C00"))
    layer.setLegend(None)


def apply_polyline_color_renderer(layer):
    def sym(color):
        return QgsLineSymbol.createSimple({"color": color, "width": "0.4", "line_style": "solid"})
    layer.setRenderer(_color_rule_renderer(layer, sym, "#E05C00"))
    layer.setLegend(None)


def apply_point_color_renderer(layer):
    def sym(color):
        return QgsMarkerSymbol.createSimple({"color": color, "outline_style": "no", "size": "2"})
    layer.setRenderer(_color_rule_renderer(layer, sym, "#008cdc"))
    layer.setLegend(None)


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


def restore_no_legend_layers(*_):
    """Re-apply setLegend(None) to all UgSurv group layers after project load."""
    root = QgsProject.instance().layerTreeRoot()
    group = root.findGroup(_GROUP_NAME)
    if group is None:
        return
    for layer_node in group.findLayers():
        layer = layer_node.layer()
        if layer:
            layer.setLegend(None)


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
