# -*- coding: utf-8 -*-
"""
CadLayers — manages the in-memory cad_layers table and drives the
rule-based renderer applied to the three geometry QGIS layers.

The cad_layers concept is AutoCAD-style layering (color/linetype/
visibility/lock) that cuts across the three geometry tables (points/lines/
polygons).  It is NOT a QGIS layer — it is stored as a non-spatial table
in the GeoPackage alongside the three geometry tables.
"""

from dataclasses import dataclass, field
from qgis.core import (
    QgsProject, QgsVectorLayer,
    QgsRuleBasedRenderer, QgsSymbol,
    QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
)

@dataclass
class CadLayer:
    name:       str
    color:      str  = "#ffffff"   # hex colour string
    linetype:   str  = "solid"
    visible:    bool = True
    locked:     bool = False
    sort_order: int  = 0


class CadLayerManager:
    """In-memory model of the cad_layers table."""

    def __init__(self):
        self._layers: list[CadLayer] = []
        self._active: str = "0"
        # ensure layer "0" always exists
        self._layers.append(CadLayer(name="0", color="#ffffff", sort_order=0))

    # ── CRUD ─────────────────────────────────────────────────────────────
    def add(self, name: str, color: str = "#ffffff", linetype: str = "solid",
            sort_order: int = None) -> CadLayer:
        if self.get(name):
            return self.get(name)
        if sort_order is None:
            sort_order = len(self._layers)
        layer = CadLayer(name=name, color=color, linetype=linetype,
                         sort_order=sort_order)
        self._layers.append(layer)
        return layer

    def get(self, name: str) -> CadLayer | None:
        for lyr in self._layers:
            if lyr.name == name:
                return lyr
        return None

    def all_layers(self) -> list[CadLayer]:
        return sorted(self._layers, key=lambda l: l.sort_order)

    def locked_names(self) -> set[str]:
        return {l.name for l in self._layers if l.locked}

    def visible_names(self) -> set[str]:
        return {l.name for l in self._layers if l.visible}

    # ── active layer ─────────────────────────────────────────────────────
    @property
    def active(self) -> str:
        return self._active

    @active.setter
    def active(self, name: str):
        if self.get(name):
            self._active = name

    # ── sync from GeoPackage non-spatial table ────────────────────────────
    def load_from_gpkg_layer(self, non_spatial_layer: QgsVectorLayer):
        """Populate from the cad_layers table row-by-row."""
        self._layers.clear()
        for feat in non_spatial_layer.getFeatures():
            self._layers.append(CadLayer(
                name       = feat["name"],
                color      = feat["color"]      or "#ffffff",
                linetype   = feat["linetype"]   or "solid",
                visible    = bool(feat["visible"]),
                locked     = bool(feat["locked"]),
                sort_order = int(feat["sort_order"] or 0),
            ))
        if not self._layers:
            self._layers.append(CadLayer(name="0"))

    def save_to_gpkg_layer(self, non_spatial_layer: QgsVectorLayer):
        """Write current in-memory state back to the GeoPackage table."""
        if not non_spatial_layer.isEditable():
            non_spatial_layer.startEditing()
        # delete all, re-insert
        fids = [f.id() for f in non_spatial_layer.getFeatures()]
        non_spatial_layer.deleteFeatures(fids)
        from qgis.core import QgsFeature
        fields = non_spatial_layer.fields()
        for lyr in self._layers:
            f = QgsFeature(fields)
            f["name"]       = lyr.name
            f["color"]      = lyr.color
            f["linetype"]   = lyr.linetype
            f["visible"]    = 1 if lyr.visible else 0
            f["locked"]     = 1 if lyr.locked  else 0
            f["sort_order"] = lyr.sort_order
            non_spatial_layer.addFeature(f)

    # ── rule-based renderer builder ───────────────────────────────────────
    @staticmethod
    def build_rule_renderer(geom_type_str: str,
                             cad_layers: list[CadLayer]) -> QgsRuleBasedRenderer:
        """
        Build a QgsRuleBasedRenderer with one rule per CAD layer.
        geom_type_str: "Point", "Line", or "Polygon"
        """
        root = QgsRuleBasedRenderer.Rule(None)
        for lyr in cad_layers:
            if geom_type_str == "Point":
                sym = QgsMarkerSymbol.createSimple({'color': lyr.color})
            elif geom_type_str == "Line":
                sym = QgsLineSymbol.createSimple({'color': lyr.color})
            else:
                sym = QgsFillSymbol.createSimple({
                    'color': 'transparent', 'outline_color': lyr.color
                })

            rule = QgsRuleBasedRenderer.Rule(sym)
            rule.setLabel(lyr.name)
            rule.setFilterExpression(f'"cad_layer" = \'{lyr.name}\'')
            rule.setActive(lyr.visible)
            root.appendChild(rule)

        renderer = QgsRuleBasedRenderer(root)
        return renderer

    def apply_renderer_to_layers(self, points_layer, lines_layer, poly_layer):
        lyrs = self.all_layers()
        if points_layer:
            points_layer.setRenderer(self.build_rule_renderer("Point", lyrs))
            points_layer.triggerRepaint()
        if lines_layer:
            lines_layer.setRenderer(self.build_rule_renderer("Line", lyrs))
            lines_layer.triggerRepaint()
        if poly_layer:
            poly_layer.setRenderer(self.build_rule_renderer("Polygon", lyrs))
            poly_layer.triggerRepaint()
