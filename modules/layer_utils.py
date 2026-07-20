import os
import math
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from qgis.core import (
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsCoordinateTransformContext,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsSingleSymbolRenderer,
    QgsRuleBasedRenderer,
    QgsSvgMarkerSymbolLayer,
    QgsEllipseSymbolLayer,
    QgsLinePatternFillSymbolLayer,
    QgsPointPatternFillSymbolLayer,
    QgsSimpleFillSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsProperty,
    QgsSymbolLayer,
    QgsPalLayerSettings,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
    QgsUnitTypes,
)

def enable_feature_render_order(layer):
    """Sort features by z_index so higher values are drawn on top within the layer."""
    renderer = layer.renderer()
    if renderer is None:
        return
    order = QgsFeatureRequest.OrderBy([
        QgsFeatureRequest.OrderByClause("z_index", True)
    ])
    renderer.setOrderBy(order)
    renderer.setOrderByEnabled(True)


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



def _line_style_renderer(layer, default_color):
    """Single-symbol line renderer with data-defined color, thickness, and line type."""
    sym = QgsLineSymbol.createSimple({"color": default_color, "width": "0.4", "line_style": "solid"})
    sl = sym.symbolLayer(0)
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeColor,
        QgsProperty.fromExpression(
            f'if("color" IS NOT NULL AND "color" != \'\', "color", \'{default_color}\')'
        ),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeWidth,
        QgsProperty.fromExpression(
            'if("line_thickness" IS NOT NULL AND "line_thickness" > 0, "line_thickness", 0.4)'
        ),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeStyle,
        QgsProperty.fromExpression(
            'if("line_type" IS NOT NULL AND "line_type" != \'\', "line_type", \'solid\')'
        ),
    )
    renderer = QgsSingleSymbolRenderer(sym)
    layer.setRenderer(renderer)
    layer.setLegend(None)


def apply_circle_color_renderer(layer):
    _line_style_renderer(layer, "#E05C00")
    enable_feature_render_order(layer)


def apply_polyline_color_renderer(layer):
    _line_style_renderer(layer, "#E05C00")
    enable_feature_render_order(layer)


def apply_hatch_renderer(layer):
    """Rule-based fill renderer driven by fill_pattern / element_size / color / angle fields."""
    _COLOR_EXPR = 'if("color" IS NOT NULL AND "color" != \'\', "color", \'#E05C00\')'
    _DIST_EXPR  = 'if("element_size" IS NOT NULL AND "element_size" > 0, "element_size", 1.0)'
    _ANGLE_EXPR = 'if("angle" IS NOT NULL, "angle", 45.0)'

    # PropertyDistance was renamed/reorganised across QGIS versions; probe at runtime.
    def _dist_prop():
        for name in ("PropertyDistance", "PropertyDistanceX"):
            p = getattr(QgsSymbolLayer, name, None)
            if p is not None:
                return p
        return None

    _DIST_PROP = _dist_prop()

    def _line_sl(angle_offset=0):
        lpf = QgsLinePatternFillSymbolLayer()
        lpf.setDataDefinedProperty(
            QgsSymbolLayer.PropertyLineAngle,
            QgsProperty.fromExpression(f'({_ANGLE_EXPR}) + {angle_offset}'),
        )
        if _DIST_PROP is not None:
            lpf.setDataDefinedProperty(
                _DIST_PROP,
                QgsProperty.fromExpression(_DIST_EXPR),
            )
        lpf.setDataDefinedProperty(
            QgsSymbolLayer.PropertyStrokeColor,
            QgsProperty.fromExpression(_COLOR_EXPR),
        )
        lpf.setLineWidth(0.3)
        return lpf

    # lines / diagonal — single set of parallel lines, angle from field
    line_sym = QgsFillSymbol()
    line_sym.deleteSymbolLayer(0)
    line_sym.appendSymbolLayer(_line_sl(0))
    line_rule = QgsRuleBasedRenderer.Rule(line_sym, elseRule=True)

    # crosshatch — two perpendicular sets
    cross_sym = QgsFillSymbol()
    cross_sym.deleteSymbolLayer(0)
    cross_sym.appendSymbolLayer(_line_sl(0))
    cross_sym.appendSymbolLayer(_line_sl(90))
    cross_rule = QgsRuleBasedRenderer.Rule(cross_sym)
    cross_rule.setFilterExpression("\"fill_pattern\" = 'crosshatch'")

    # dots — point pattern fill
    dot_sym = QgsFillSymbol()
    dot_sym.deleteSymbolLayer(0)
    ppf = QgsPointPatternFillSymbolLayer()
    _DX = getattr(QgsSymbolLayer, "PropertyDistanceX", None)
    _DY = getattr(QgsSymbolLayer, "PropertyDistanceY", None)
    if _DX is not None:
        ppf.setDataDefinedProperty(_DX, QgsProperty.fromExpression(_DIST_EXPR))
    if _DY is not None:
        ppf.setDataDefinedProperty(_DY, QgsProperty.fromExpression(_DIST_EXPR))
    dot_marker = ppf.subSymbol()
    dot_marker.deleteSymbolLayer(0)
    dot_sl = QgsSimpleMarkerSymbolLayer()
    dot_sl.setShape(QgsSimpleMarkerSymbolLayer.Circle)
    dot_sl.setSize(0.4)
    dot_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyFillColor,
        QgsProperty.fromExpression(_COLOR_EXPR),
    )
    dot_sl.setStrokeStyle(Qt.NoPen)
    dot_marker.appendSymbolLayer(dot_sl)
    dot_sym.appendSymbolLayer(ppf)
    dot_rule = QgsRuleBasedRenderer.Rule(dot_sym)
    dot_rule.setFilterExpression("\"fill_pattern\" = 'dots'")

    # pavers — running-bond brick pattern using offset rectangular markers
    pav_sym = QgsFillSymbol()
    pav_sym.deleteSymbolLayer(0)

    ppf_pav = QgsPointPatternFillSymbolLayer()
    _DX  = getattr(QgsSymbolLayer, "PropertyDistanceX",    None)
    _DY  = getattr(QgsSymbolLayer, "PropertyDistanceY",    None)
    _DDX = getattr(QgsSymbolLayer, "PropertyDisplacementX", None)
    if _DX  is not None:
        ppf_pav.setDataDefinedProperty(_DX,  QgsProperty.fromExpression(_DIST_EXPR))
    if _DY  is not None:
        # brick height = half the width
        ppf_pav.setDataDefinedProperty(_DY,  QgsProperty.fromExpression(f'({_DIST_EXPR}) * 0.5'))
    if _DDX is not None:
        # offset alternate rows by half a brick width → running bond
        ppf_pav.setDataDefinedProperty(_DDX, QgsProperty.fromExpression(f'({_DIST_EXPR}) * 0.5'))

    # Static fallbacks so the pattern renders correctly even without data-defined support
    ppf_pav.setDistanceX(2.0)
    ppf_pav.setDistanceY(1.0)
    ppf_pav.setDisplacementX(1.0)

    # Sub-symbol: rectangle (ellipse layer with rectangle shape)
    pav_marker = ppf_pav.subSymbol()
    pav_marker.deleteSymbolLayer(0)
    brick_sl = QgsEllipseSymbolLayer()
    # Set rectangle shape — try enum first, then string name
    try:
        brick_sl.setSymbolName(QgsEllipseSymbolLayer.Rectangle)
    except (AttributeError, TypeError):
        try:
            brick_sl.setSymbolName("rectangle")
        except Exception:
            pass
    # Width ≈ 85 % of spacing, height ≈ 40 % (leaves a mortar gap)
    _PW = getattr(QgsSymbolLayer, "PropertyWidth",  None)
    _PH = getattr(QgsSymbolLayer, "PropertyHeight", None)
    if _PW is not None:
        brick_sl.setDataDefinedProperty(_PW, QgsProperty.fromExpression(f'({_DIST_EXPR}) * 0.85'))
    if _PH is not None:
        brick_sl.setDataDefinedProperty(_PH, QgsProperty.fromExpression(f'({_DIST_EXPR}) * 0.4'))
    brick_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyStrokeColor,
        QgsProperty.fromExpression(_COLOR_EXPR),
    )
    from PyQt5.QtGui import QColor as _QColor
    brick_sl.setFillColor(_QColor(0, 0, 0, 0))   # transparent fill — outline only
    brick_sl.setStrokeWidth(0.3)
    pav_marker.appendSymbolLayer(brick_sl)
    pav_sym.appendSymbolLayer(ppf_pav)
    pav_rule = QgsRuleBasedRenderer.Rule(pav_sym)
    pav_rule.setFilterExpression("\"fill_pattern\" = 'pavers'")

    # wetland — faithful recreation of QGIS "topo swamp":
    #   • aqua-green semi-transparent background fill
    #   • scattered reed clumps: 3 vertical line markers of different heights
    #     arranged as a tuft (short right, tall centre, tall left)
    wet_sym = QgsFillSymbol()
    wet_sym.deleteSymbolLayer(0)

    # ── Layer 1: aqua-green background ────────────────────────────────
    bg = QgsSimpleFillSymbolLayer()
    bg.setColor(_QColor(0, 0, 0, 0))   # transparent fill
    bg.setStrokeStyle(Qt.NoPen)
    wet_sym.appendSymbolLayer(bg)

    # ── Layer 2: reed-clump point pattern fill ────────────────────────
    ppf_wet = QgsPointPatternFillSymbolLayer()

    # Grid spacing — scaled to element_size (element_size drives dist_y)
    _WDX  = getattr(QgsSymbolLayer, "PropertyDistanceX",    None)
    _WDY  = getattr(QgsSymbolLayer, "PropertyDistanceY",    None)
    _WDDX = getattr(QgsSymbolLayer, "PropertyDisplacementX", None)
    _WDDY = getattr(QgsSymbolLayer, "PropertyDisplacementY", None)
    if _WDX  is not None:
        ppf_wet.setDataDefinedProperty(_WDX,  QgsProperty.fromExpression(f'({_DIST_EXPR}) * 1.73'))
    if _WDY  is not None:
        ppf_wet.setDataDefinedProperty(_WDY,  QgsProperty.fromExpression(_DIST_EXPR))
    if _WDDX is not None:
        ppf_wet.setDataDefinedProperty(_WDDX, QgsProperty.fromExpression(f'({_DIST_EXPR}) * 0.76'))
    if _WDDY is not None:
        ppf_wet.setDataDefinedProperty(_WDDY, QgsProperty.fromExpression(f'({_DIST_EXPR}) * 0.24'))
    # Static fallbacks in map units
    ppf_wet.setDistanceX(6.0)
    ppf_wet.setDistanceY(3.5)
    ppf_wet.setDisplacementX(2.7)
    ppf_wet.setDisplacementY(0.8)

    # Build the 3-marker reed clump sub-symbol
    clump = ppf_wet.subSymbol()
    clump.deleteSymbolLayer(0)

    # Probe for line shape and offset properties
    _line_shape = (
        getattr(QgsSimpleMarkerSymbolLayer, "Line", None)
        or getattr(getattr(QgsSimpleMarkerSymbolLayer, "Shape", object()), "Line", None)
    )
    _PS  = getattr(QgsSymbolLayer, "PropertySize",    None)
    _POX = getattr(QgsSymbolLayer, "PropertyOffsetX", None)
    _POY = getattr(QgsSymbolLayer, "PropertyOffsetY", None)

    def _reed(size_f, ox_f, oy_f):
        """One vertical line marker, sized and offset as a fraction of element_size."""
        sl = QgsSimpleMarkerSymbolLayer()
        if _line_shape is not None:
            try:
                sl.setShape(_line_shape)
            except Exception:
                pass
        sl.setAngle(90)          # vertical stem
        sl.setStrokeWidth(0.4)
        sl.setFillColor(_QColor(0, 0, 0, 0))
        # Size
        if _PS is not None:
            sl.setDataDefinedProperty(_PS,  QgsProperty.fromExpression(f'({_DIST_EXPR}) * {size_f}'))
        else:
            sl.setSize(size_f * 3.5)
        # Horizontal offset
        if _POX is not None:
            sl.setDataDefinedProperty(_POX, QgsProperty.fromExpression(f'({_DIST_EXPR}) * {ox_f}'))
        # Vertical offset
        if _POY is not None:
            sl.setDataDefinedProperty(_POY, QgsProperty.fromExpression(f'({_DIST_EXPR}) * {oy_f}'))
        # Reed colour driven by "color" field (water blue default via auto-set)
        sl.setDataDefinedProperty(
            QgsSymbolLayer.PropertyStrokeColor,
            QgsProperty.fromExpression(_COLOR_EXPR),
        )
        return sl

    # Proportions from QGIS topo swamp (normalised to dist_y = 1.0):
    #   short reed right:   size=0.33, offset=(+0.12, +0.21)
    #   tall centre reed:   size=0.58, offset=(0, 0)
    #   tall reed left:     size=0.58, offset=(-0.12, +0.27)
    clump.appendSymbolLayer(_reed(0.33,  0.12,  0.21))
    clump.appendSymbolLayer(_reed(0.58,  0.00,  0.00))
    clump.appendSymbolLayer(_reed(0.58, -0.12,  0.27))

    wet_sym.appendSymbolLayer(ppf_wet)

    wet_rule = QgsRuleBasedRenderer.Rule(wet_sym)
    wet_rule.setFilterExpression("\"fill_pattern\" = 'wetland'")

    root = QgsRuleBasedRenderer.Rule(None)
    root.appendChild(cross_rule)
    root.appendChild(dot_rule)
    root.appendChild(pav_rule)
    root.appendChild(wet_rule)
    root.appendChild(line_rule)  # else rule must be last

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLegend(None)
    enable_feature_render_order(layer)


def apply_point_color_renderer(layer):
    _COLOR_EXPR = 'if("color" IS NOT NULL AND "color" != \'\', "color", \'#008cdc\')'
    _SIZE_EXPR  = 'if("symbol_size" IS NOT NULL AND "symbol_size" > 0, "symbol_size", 2.0)'

    # ── SVG rule — active when symbol_svg field contains a path ──────
    svg_sym = QgsMarkerSymbol()
    svg_sym.deleteSymbolLayer(0)
    svg_sl = QgsSvgMarkerSymbolLayer("")
    svg_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyName,
        QgsProperty.fromExpression('"symbol_svg"'),
    )
    svg_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertySize,
        QgsProperty.fromExpression(_SIZE_EXPR),
    )
    svg_sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyFillColor,
        QgsProperty.fromExpression(_COLOR_EXPR),
    )
    svg_sym.appendSymbolLayer(svg_sl)

    svg_rule = QgsRuleBasedRenderer.Rule(svg_sym)
    svg_rule.setFilterExpression('"symbol_svg" IS NOT NULL AND "symbol_svg" != \'\'')

    # ── Simple marker rule — everything else ─────────────────────────
    simple_sym = QgsMarkerSymbol.createSimple(
        {"color": "#008cdc", "outline_style": "no", "size": "2", "name": "circle"}
    )
    sl = simple_sym.symbolLayer(0)
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyFillColor,
        QgsProperty.fromExpression(_COLOR_EXPR),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertyName,
        QgsProperty.fromExpression(
            'if("symbol" IS NOT NULL AND "symbol" != \'\', "symbol", \'circle\')'
        ),
    )
    sl.setDataDefinedProperty(
        QgsSymbolLayer.PropertySize,
        QgsProperty.fromExpression(_SIZE_EXPR),
    )
    simple_rule = QgsRuleBasedRenderer.Rule(simple_sym, elseRule=True)

    root = QgsRuleBasedRenderer.Rule(None)
    root.appendChild(svg_rule)
    root.appendChild(simple_rule)

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLegend(None)
    enable_feature_render_order(layer)


def apply_dimension_style(layer):
    """Transparent line (label anchor only) + map-unit labels for the dimension layer."""
    _COLOR_EXPR = 'if("color" IS NOT NULL AND "color" != \'\', "color", \'#000000\')'
    _SIZE_EXPR  = 'if("text_size" IS NOT NULL AND "text_size" > 0, "text_size", 1.5)'
    _FONT_EXPR  = 'if("font_type" IS NOT NULL AND "font_type" != \'\', "font_type", \'Century Gothic\')'

    # Transparent line — exists only so QGIS has a geometry to hang the label on
    sym = QgsLineSymbol.createSimple({'color': 'transparent', 'width': '0'})
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLegend(None)

    # Read current font from the layer's first feature so re-calls keep the font in sync
    default_font = "Century Gothic"
    for feat in layer.getFeatures():
        v = feat["font_type"] if layer.fields().indexOf("font_type") >= 0 else None
        if v:
            default_font = str(v)
        break

    font = QFont(default_font, 10)
    font.setBold(True)
    font.setItalic(True)

    tf = QgsTextFormat()
    tf.setFont(font)
    tf.setColor(QColor(0, 0, 0))
    tf.setSize(1.5)
    tf.setSizeUnit(QgsUnitTypes.RenderMapUnits)

    pal = QgsPalLayerSettings()
    pal.isExpression = True
    pal.fieldName = 'round("distance", coalesce("decimal_places", 3))'
    pal.placement = QgsPalLayerSettings.Line
    pal.setFormat(tf)

    try:
        dd = pal.dataDefinedProperties()
        dd.setProperty(QgsPalLayerSettings.Color,  QgsProperty.fromExpression(_COLOR_EXPR))
        dd.setProperty(QgsPalLayerSettings.Size,   QgsProperty.fromExpression(_SIZE_EXPR))
        dd.setProperty(QgsPalLayerSettings.Family, QgsProperty.fromExpression(_FONT_EXPR))
    except Exception:
        pass

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
    enable_feature_render_order(layer)
    layer.triggerRepaint()


_DATA_DIR  = os.path.join(os.path.expanduser("~"), "Desktop", "Ugsurv_features")
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
        if layer and layer.isValid():
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
