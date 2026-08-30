# -*- coding: utf-8 -*-
"""
Ugsurv — composition root.

Instantiates every core object, wires signals, registers commands,
builds the toolbar and docks, and handles activate/deactivate.
"""

import os
import contextlib

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon, QKeySequence
from qgis.PyQt.QtWidgets import QAction, QShortcut, QToolBar

from qgis.core import QgsProject
from qgis.gui import QgsMapCanvas

# ── Resources ─────────────────────────────────────────────────────────────
from .resources import *  # noqa: F403


class Ugsurv:
    """QGIS Plugin Implementation — CAD drafting environment."""

    def __init__(self, iface):
        self.iface      = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.canvas: QgsMapCanvas = iface.mapCanvas()

        # locale
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(self.plugin_dir, 'i18n',
                                   f'Ugsurv_{locale}.qm')
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions    = []
        self.menu       = self.tr('&Ugsurv')
        self.first_start = None
        self._active    = False

        # populated in run()
        self._tool_manager  = None
        self._dispatcher    = None
        self._cad_toolbar   = None
        self._cmd_dock      = None
        self._snap_dock     = None
        self._layers_dock   = None
        self._props_dock    = None
        self._dyn_widget    = None
        self._storage       = None
        self._tool_context  = None
        self._shortcuts              = []
        self._tool_changed_slot     = None
        self._tool_for_cmdline_slot = None
        self._unknown_cmd_slot      = None
        self._reload_layers_slot    = None

    # ── Qt i18n helper ────────────────────────────────────────────────────
    def tr(self, message: str) -> str:
        return QCoreApplication.translate('Ugsurv', message)

    # ── QGIS plugin lifecycle ─────────────────────────────────────────────
    def add_action(self, icon_path, text, callback,
                   enabled_flag=True, add_to_menu=True,
                   add_to_toolbar=True, status_tip=None, parent=None):
        icon   = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if status_tip:
            action.setStatusTip(status_tip)
        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        action.setCheckable(True)
        return action

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.add_action(
            icon_path,
            text=self.tr('Ugsurv CAD'),
            callback=self.run,
            parent=self.iface.mainWindow(),
        )
        self.first_start = True

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        self._teardown()

    # ── activate / deactivate ─────────────────────────────────────────────
    def run(self):
        if self._active:
            self._teardown()
            self._active = False
            return
        self._active = True
        self.first_start = False
        self._setup()

    # ── full setup ────────────────────────────────────────────────────────
    def _setup(self):
        iface    = self.iface
        canvas   = self.canvas
        mw       = iface.mainWindow()
        icon_dir = os.path.join(self.plugin_dir, "resources", "icons")

        # 1. Core objects ─────────────────────────────────────────────────
        from .core.tool_context    import ToolContext
        from .core.selection_model import SelectionModel
        from .core.tool_manager    import ToolManager

        from .core.snapping.snap_settings              import SnapSettings
        from .core.snapping.snap_engine                import SnapEngine
        from .core.snapping.providers.vertex_provider       import VertexProvider
        from .core.snapping.providers.midpoint_provider     import MidpointProvider
        from .core.snapping.providers.center_provider       import CenterProvider
        from .core.snapping.providers.intersection_provider import IntersectionProvider
        from .core.snapping.providers.perpendicular_provider import PerpendicularProvider
        from .core.snapping.providers.extension_provider    import ExtensionProvider
        from .core.snapping.providers.grid_provider         import GridProvider
        from .core.snapping.providers.self_snap_provider    import SelfSnapProvider

        from .core.constraints.ortho_constraint import OrthoConstraint
        from .core.constraints.polar_constraint import PolarConstraint

        from .core.input.input_translator   import InputTranslator
        from .core.input.command_registry   import CommandRegistry
        from .core.input.command_dispatcher import CommandDispatcher

        from .core.drawing.storage_manager import StorageManager
        from .core.drawing.entity_factory  import EntityFactory
        from .core.drawing.cad_layers      import CadLayerManager

        ctx = ToolContext()
        ctx.canvas = canvas
        ctx.iface  = iface
        self._tool_context = ctx

        sel = SelectionModel()
        ctx.selection_model = sel

        snap_settings = SnapSettings()
        snap_engine   = SnapEngine(snap_settings, None)
        for key, ProviderClass in [
            ("vertex",        VertexProvider),
            ("midpoint",      MidpointProvider),
            ("center",        CenterProvider),
            ("intersection",  IntersectionProvider),
            ("perpendicular", PerpendicularProvider),
            ("extension",     ExtensionProvider),
            ("grid",          GridProvider),
            ("self",          SelfSnapProvider),
        ]:
            snap_engine.register_provider(key, ProviderClass())
        ctx.snap_engine = snap_engine

        ortho = OrthoConstraint()
        polar = PolarConstraint()
        ctx.constraints = [ortho, polar]

        translator = InputTranslator(snap_engine)
        self._translator = translator

        storage = StorageManager()
        ctx.storage_manager = storage
        self._storage       = storage
        snap_engine._storage = storage

        entity_factory = EntityFactory(storage)
        ctx.entity_factory = entity_factory

        cad_lyr_mgr = CadLayerManager()
        ctx.cad_layer_manager = cad_lyr_mgr

        registry = CommandRegistry()
        tool_mgr = ToolManager(canvas)
        self._tool_manager = tool_mgr

        # 2. Tool factory ─────────────────────────────────────────────────
        tool_cache = {}

        def tool_factory(key: str):
            if key not in tool_cache:
                tool_cache[key] = _make_tool(key, canvas, ctx, translator)
            return tool_cache[key]

        dispatcher = CommandDispatcher(tool_mgr, registry, tool_factory)
        self._dispatcher = dispatcher

        # 3. Register command aliases ─────────────────────────────────────
        _register_commands(registry)

        # 4. UI ───────────────────────────────────────────────────────────
        from .ui.toolbar              import CadToolbar
        from .ui.command_line_widget  import CommandLineWidget
        from .ui.dynamic_input_widget import DynamicInputWidget
        from .ui.properties_panel     import PropertiesPanel
        from .ui.cad_layers_dock      import CadLayersDock
        from .ui.snap_settings_dialog import SnapSettingsDialog

        for _stale in mw.findChildren(QToolBar, "UgsurvCadToolbar"):
            mw.removeToolBar(_stale)
            _stale.deleteLater()

        cad_toolbar = CadToolbar(dispatcher, icon_dir, mw)
        iface.addToolBar(cad_toolbar)
        storage.set_toolbar(cad_toolbar)
        self._cad_toolbar = cad_toolbar

        def _on_tool_changed(tool):
            key = getattr(tool, '_tool_key', None) if tool else None
            cad_toolbar.set_active_tool(key)
        tool_mgr.toolChanged.connect(_on_tool_changed)
        self._tool_changed_slot = _on_tool_changed

        cmd_dock = CommandLineWidget(dispatcher, translator, mw)
        iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, cmd_dock)
        self._cmd_dock = cmd_dock

        dyn = DynamicInputWidget(canvas, translator, canvas)
        self._dyn_widget = dyn

        props = PropertiesPanel(sel, mw)
        iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, props)
        self._props_dock = props

        layers_dock = CadLayersDock(cad_lyr_mgr, sel, storage, mw)
        iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, layers_dock)
        self._layers_dock = layers_dock

        snap_dialog = SnapSettingsDialog(snap_settings, snap_engine, mw)
        self._snap_dock = snap_dialog  # reuse attr so teardown loop still works

        # Toolbar snap button
        snap_icon = os.path.join(icon_dir, "snap.png")
        cad_toolbar.add_snap_button(snap_dialog.open_or_raise, snap_icon)

        # Command-line aliases: SNAP / OS / OSNAP
        cmd_dock.register_ui_command("SNAP", "OS", "OSNAP",
                                     callback=snap_dialog.open_or_raise)

        # HELP command — prints all available commands to the log
        def _on_help():
            # group registry aliases by tool key
            inv: dict[str, list[str]] = {}
            for alias, key in registry._map.items():
                inv.setdefault(key, []).append(alias)

            cmd_dock.log("─" * 44, "#4488cc")
            cmd_dock.log("  Available commands", "#88ccff")
            cmd_dock.log("─" * 44, "#4488cc")
            for key in sorted(inv):
                tags = "  ".join(sorted(inv[key]))
                cmd_dock.log(f"  {key:<18} {tags}", "#aaddff")
            cmd_dock.log("  snap_settings      SNAP  OS  OSNAP", "#aaddff")
            cmd_dock.log("  help               HELP  ?", "#aaddff")
            cmd_dock.log("─" * 44, "#4488cc")

        cmd_dock.register_ui_command("HELP", "?", callback=_on_help)

        # 5. Wire dynamic input to canvas XY ─────────────────────────────
        def _on_canvas_xy(pt):
            dyn.update_position(pt)
            dyn.show_for_tool(tool_mgr.active_tool is not None)
        canvas.xyCoordinates.connect(_on_canvas_xy)
        self._canvas_xy_slot = _on_canvas_xy

        # wire command-line / dynamic-input text to active tool
        def _on_text_input(text: str):
            active = tool_mgr.active_tool
            if active and hasattr(active, '_dispatch') and hasattr(active, '_translator'):
                sem = translator.translate_typed_text(text, None, ctx)
                if sem:
                    active._dispatch(sem)
        cmd_dock.textValueEntered.connect(_on_text_input)
        dyn.valueEntered.connect(_on_text_input)

        # unknown command → log
        _unknown_cmd = lambda t: cmd_dock.log(f"Unknown command: {t}", "#ff6666")
        dispatcher.unknownCommand.connect(_unknown_cmd)
        self._unknown_cmd_slot = _unknown_cmd

        # mid-command flag on cmd_dock
        def _on_tool_for_cmdline(tool):
            if tool:
                name = getattr(tool, '_tool_key', '').upper()
                cmd_dock.set_mid_command(True, name)
            else:
                cmd_dock.set_mid_command(False)
        tool_mgr.toolChanged.connect(_on_tool_for_cmdline)
        self._tool_for_cmdline_slot = _on_tool_for_cmdline

        # 6. Keyboard shortcuts ──────────────────────────────────────────
        self._shortcuts = _install_shortcuts(
            canvas, ortho, polar, snap_settings, tool_mgr
        )

        # 7. Gating hint ─────────────────────────────────────────────────
        def _on_gating(enabled: bool):
            if not enabled:
                iface.messageBar().pushMessage(
                    "UgSurv CAD",
                    "Save the project before drafting.",
                    level=0, duration=3
                )
        storage.gatingChanged.connect(_on_gating)
        if not storage.enabled:
            iface.messageBar().pushMessage(
                "UgSurv CAD",
                "Save the project before drafting.",
                level=0, duration=3
            )

        # 8. Reload CAD layers from GeoPackage when available ────────────
        def _reload_cad_layers(enabled: bool):
            if not enabled:
                return
            tbl = storage.cad_layers_table
            if tbl and tbl.isValid():
                cad_lyr_mgr.load_from_gpkg_layer(tbl)
                layers_dock._refresh()
                cad_lyr_mgr.apply_renderer_to_layers(
                    storage.points_layer,
                    storage.lines_layer,
                    storage.polygons_layer,
                )
        storage.gatingChanged.connect(_reload_cad_layers)
        self._reload_layers_slot = _reload_cad_layers

    # ── teardown ──────────────────────────────────────────────────────────
    def _teardown(self):
        with contextlib.suppress(Exception):
            if self._tool_manager:
                self._tool_manager.deactivate()

        # Disconnect canvas.xyCoordinates FIRST — it references _dyn_widget
        # which will be deleted below.  A Python lambda connected to a
        # persistent QGIS signal is never auto-disconnected, so we must do it
        # explicitly to avoid "wrapped C/C++ object has been deleted" errors
        # on the next mouse-move after reload.
        with contextlib.suppress(Exception):
            slot = getattr(self, '_canvas_xy_slot', None)
            if slot:
                self.canvas.xyCoordinates.disconnect(slot)
        self._canvas_xy_slot = None

        with contextlib.suppress(Exception):
            if self._storage:
                self._storage.unload()

        for sc in self._shortcuts:
            with contextlib.suppress(Exception):
                sc.setEnabled(False)
                sc.deleteLater()
        self._shortcuts.clear()

        # Disconnect all closure-based signals before deleteLater so stale
        # closures never fire on half-destroyed dock child widgets.
        with contextlib.suppress(Exception):
            tm = self._tool_manager
            if tm:
                for attr in ('_tool_changed_slot', '_tool_for_cmdline_slot'):
                    slot = getattr(self, attr, None)
                    if slot:
                        tm.toolChanged.disconnect(slot)
        with contextlib.suppress(Exception):
            if self._dispatcher:
                slot = getattr(self, '_unknown_cmd_slot', None)
                if slot:
                    self._dispatcher.unknownCommand.disconnect(slot)
        with contextlib.suppress(Exception):
            if self._storage:
                slot = getattr(self, '_reload_layers_slot', None)
                if slot:
                    self._storage.gatingChanged.disconnect(slot)
        self._tool_changed_slot     = None
        self._tool_for_cmdline_slot = None
        self._unknown_cmd_slot      = None
        self._reload_layers_slot    = None

        for dock_attr in ("_cmd_dock", "_layers_dock", "_props_dock"):
            dock = getattr(self, dock_attr, None)
            if dock:
                with contextlib.suppress(Exception):
                    self.iface.removeDockWidget(dock)
                    dock.deleteLater()
                setattr(self, dock_attr, None)

        # snap widget is a floating QDialog, not a dock
        if self._snap_dock:
            with contextlib.suppress(Exception):
                self._snap_dock.close()
                self._snap_dock.deleteLater()
            self._snap_dock = None

        if self._cad_toolbar:
            with contextlib.suppress(Exception):
                self.iface.removeToolBar(self._cad_toolbar)
                self._cad_toolbar.deleteLater()
            self._cad_toolbar = None

        if self._dyn_widget:
            with contextlib.suppress(Exception):
                self._dyn_widget.hide()
                self._dyn_widget.deleteLater()
            self._dyn_widget = None

        self._tool_manager = None
        self._dispatcher   = None
        self._storage      = None
        self._tool_context = None


# ── Tool factory ──────────────────────────────────────────────────────────
def _make_tool(key: str, canvas, ctx, translator):
    """Instantiate and return a tool by key, or None if key unknown."""
    from .tools.drawing.point_tool        import PointTool
    from .tools.drawing.line_tool         import LineTool
    from .tools.drawing.polyline_tool     import PolylineTool
    from .tools.drawing.circle_tool       import CircleTool
    from .tools.drawing.arc_tool          import ArcTool
    from .tools.drawing.rectangle_tool    import RectangleTool
    from .tools.drawing.polygon_tool      import PolygonTool
    from .tools.modify.move_tool          import MoveTool
    from .tools.modify.copy_tool          import CopyTool
    from .tools.modify.rotate_tool        import RotateTool
    from .tools.modify.scale_tool         import ScaleTool
    from .tools.modify.mirror_tool        import MirrorTool
    from .tools.modify.offset_tool        import OffsetTool
    from .tools.modify.trim_tool          import TrimTool
    from .tools.modify.extend_tool        import ExtendTool
    from .tools.modify.fillet_tool        import FilletTool
    from .tools.modify.array_tool         import ArrayTool
    from .tools.selection.select_tool     import SelectTool
    from .tools.selection.erase_tool      import EraseTool
    from .tools.selection.stretch_tool    import StretchTool
    from .tools.vertex_edit.grip_edit_tool     import GripEditTool
    from .tools.vertex_edit.add_vertex_tool    import AddVertexTool
    from .tools.vertex_edit.remove_vertex_tool import RemoveVertexTool
    from .tools.vertex_edit.break_tool         import BreakTool
    from .tools.vertex_edit.join_tool          import JoinTool
    from .tools.annotation.dimension_tool import DimensionTool
    from .tools.annotation.text_tool      import TextTool

    _MAP = {
        "point":         PointTool,
        "line":          LineTool,        "polyline":      PolylineTool,
        "circle":        CircleTool,      "arc":           ArcTool,
        "rectangle":     RectangleTool,   "polygon":       PolygonTool,
        "move":          MoveTool,        "copy":          CopyTool,
        "rotate":        RotateTool,      "scale":         ScaleTool,
        "mirror":        MirrorTool,      "offset":        OffsetTool,
        "trim":          TrimTool,        "extend":        ExtendTool,
        "fillet":        FilletTool,      "array":         ArrayTool,
        "select":        SelectTool,      "erase":         EraseTool,
        "stretch":       StretchTool,
        "grip_edit":     GripEditTool,    "add_vertex":    AddVertexTool,
        "remove_vertex": RemoveVertexTool,"break":         BreakTool,
        "join":          JoinTool,
        "dimension":     DimensionTool,   "text":          TextTool,
    }
    cls = _MAP.get(key)
    if cls is None:
        return None
    tool = cls(canvas, ctx, translator)
    tool._tool_key = key
    return tool


# ── Command registry ──────────────────────────────────────────────────────
def _register_commands(registry):
    for key, *aliases in [
        ("point",         "POINT",   "PT",  "PO"),
        ("line",          "LINE",    "L"),
        ("polyline",      "PLINE",   "PL"),
        ("circle",        "CIRCLE",  "C"),
        ("arc",           "ARC",     "A"),
        ("rectangle",     "REC",     "RECTANGLE", "RECT"),
        ("polygon",       "POLYGON", "POL"),
        ("move",          "MOVE",    "M"),
        ("copy",          "COPY",    "CO", "CP"),
        ("rotate",        "ROTATE",  "RO"),
        ("scale",         "SCALE",   "SC"),
        ("mirror",        "MIRROR",  "MI"),
        ("offset",        "OFFSET",  "O"),
        ("trim",          "TRIM",    "TR"),
        ("extend",        "EXTEND",  "EX"),
        ("fillet",        "FILLET",  "F"),
        ("array",         "ARRAY",   "AR"),
        ("select",        "SELECT",  "SS"),
        ("erase",         "ERASE",   "E", "DEL"),
        ("stretch",       "STRETCH", "S"),
        ("grip_edit",     "GRIPS",   "V"),
        ("add_vertex",    "ADDV",    "AV"),
        ("remove_vertex", "REMV",    "RV"),
        ("break",         "BREAK",   "BR"),
        ("join",          "JOIN",    "J"),
        ("dimension",     "DIM",     "DIMLINEAR"),
        ("text",          "TEXT",    "T", "MTEXT"),
    ]:
        registry.register(key, *aliases)


# ── Keyboard shortcuts ────────────────────────────────────────────────────
def _install_shortcuts(canvas, ortho, polar, snap_settings, tool_mgr):
    shortcuts = []

    def _sc(seq, slot):
        sc = QShortcut(QKeySequence(seq), canvas)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(slot)
        shortcuts.append(sc)

    def _toggle(obj, attr):
        setattr(obj, attr, not getattr(obj, attr))

    _sc("F8",  lambda: _toggle(ortho, "enabled"))
    _sc("F10", lambda: _toggle(polar, "enabled"))
    _sc("F3",  lambda: [snap_settings.set_enabled(k, not snap_settings.any_enabled())
                        for k in ["vertex", "midpoint", "center"]])
    def _do_escape():
        t = tool_mgr.active_tool
        if t and hasattr(t, 'cancel'):
            t.cancel()
        tool_mgr.deactivate()

    _sc("Escape", _do_escape)
    return shortcuts
