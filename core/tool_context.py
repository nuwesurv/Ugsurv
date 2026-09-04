# -*- coding: utf-8 -*-
"""Shared read-state passed into every tool."""


class ToolContext:
    """Mutable bag of shared state; tools read but never own it."""

    def __init__(self):
        self.active_cad_layer: str = "0"          # cad_layers.name FK
        self.snap_engine = None                    # set by plugin_main
        self.constraints = []                      # active constraint objects
        self.storage_manager = None                # set by plugin_main
        self.selection_model = None                # set by plugin_main
        self.selection_overlay = None              # SelectionOverlay, set by plugin_main
        self.go_home = None                        # callable: return to SelectTool
        self.canvas = None                         # QgsMapCanvas, set by plugin_main
        self.iface = None                          # QgsInterface, set by plugin_main
        self.dyn_widget = None                     # DynamicInputWidget, set by plugin_main
        self.cmd_dock = None                       # CommandLineWidget, set by plugin_main
        self.selected_raster = None                # QgsRasterLayer picked via SelectTool
        self.launch_tool     = None                # callable(tool_key) → activates a tool by key

    # convenience ----------------------------------------------------------
    @property
    def map_crs(self):
        from qgis.core import QgsProject
        return QgsProject.instance().crs()

    @property
    def plugin_extra_layers(self):
        """Valid spatial plugin layers not managed by StorageManager (name starts with '_').
        Used so vertex/selection tools can also reach _dimensions and similar layers."""
        from qgis.core import QgsProject, QgsVectorLayer
        sm_ids = set()
        if self.storage_manager:
            for attr in ("points_layer", "lines_layer"):
                lyr = getattr(self.storage_manager, attr, None)
                if lyr and lyr.isValid():
                    sm_ids.add(lyr.id())
        result = []
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            if lyr.id() in sm_ids:
                continue
            if lyr.name().startswith('_') and lyr.isSpatial() and lyr.isValid():
                result.append(lyr)
        return result
