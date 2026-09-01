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

    # convenience ----------------------------------------------------------
    @property
    def map_crs(self):
        from qgis.core import QgsProject
        return QgsProject.instance().crs()
