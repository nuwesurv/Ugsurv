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
        self._dyn_widget         = None
        self._input_buffer       = None
        self._global_key_filter  = None
        self._storage            = None
        self._tool_context       = None
        self._sel_overlay        = None
        self._maptool_action     = None
        self._snap_action        = None
        self._ortho_action       = None
        self._shortcuts              = []
        self._tool_changed_slot     = None
        self._tool_for_cmdline_slot = None
        self._unknown_cmd_slot      = None
        self._reload_layers_slot    = None
        self._props_sel_slot        = None
        self._sel                   = None
        self._extra_docks           = []
        self._revert_tool           = None
        self._tfix_tool             = None
        self._crs_mgr               = None
        self._action_history        = None

    # ── Qt i18n helper ────────────────────────────────────────────────────
    def tr(self, message: str) -> str:
        return QCoreApplication.translate('Ugsurv', message)

    # ── QGIS plugin lifecycle ─────────────────────────────────────────────
    def add_action(self, icon_path, text, callback,
                   enabled_flag=True, add_to_menu=True,
                   add_to_toolbar=True, status_tip=None, parent=None):
        icon   = icon_path if isinstance(icon_path, QIcon) else QIcon(icon_path)
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
        from .core.snapping.providers.vertex_provider        import VertexProvider
        from .core.snapping.providers.point_provider         import PointProvider
        from .core.snapping.providers.midpoint_provider      import MidpointProvider
        from .core.snapping.providers.center_provider        import CenterProvider
        from .core.snapping.providers.intersection_provider  import IntersectionProvider
        from .core.snapping.providers.perpendicular_provider import PerpendicularProvider
        from .core.snapping.providers.extension_provider     import ExtensionProvider
        from .core.snapping.providers.grid_provider          import GridProvider
        from .core.snapping.providers.self_snap_provider     import SelfSnapProvider
        from .core.snapping.providers.nearest_provider       import NearestProvider

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

        # CRS enforcement — must happen before storage or tools touch any layer
        from .core.crs_manager import CrsManager
        crs_mgr = CrsManager(iface)
        crs_mgr.enforce_project_crs()
        self._crs_mgr = crs_mgr
        ctx.crs_manager = crs_mgr

        sel = SelectionModel()
        ctx.selection_model = sel

        from .core.selection_overlay import SelectionOverlay
        self._sel_overlay = SelectionOverlay(canvas, sel)
        ctx.selection_overlay = self._sel_overlay

        snap_settings = SnapSettings()
        snap_engine   = SnapEngine(snap_settings, None)
        for key, ProviderClass in [
            ("vertex",        VertexProvider),
            ("point",         PointProvider),
            ("midpoint",      MidpointProvider),
            ("center",        CenterProvider),
            ("intersection",  IntersectionProvider),
            ("perpendicular", PerpendicularProvider),
            ("extension",     ExtensionProvider),
            ("grid",          GridProvider),
            ("self",          SelfSnapProvider),
            ("nearest",       NearestProvider),
        ]:
            snap_engine.register_provider(key, ProviderClass())
        ctx.snap_engine = snap_engine

        ortho = OrthoConstraint()
        polar = PolarConstraint()
        ctx.constraints = [ortho, polar]

        translator = InputTranslator(snap_engine)
        self._translator = translator

        storage = StorageManager()
        storage.set_crs_manager(crs_mgr)
        ctx.storage_manager = storage
        self._storage       = storage
        snap_engine._storage = storage

        from .core.action_history import ActionHistory
        action_history = ActionHistory()
        ctx.action_history = action_history
        storage.set_action_history(action_history)
        self._action_history = action_history
        QgsProject.instance().cleared.connect(action_history.clear)
        QgsProject.instance().readProject.connect(action_history.clear)

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
        from .ui.cad_layers_dock      import CadLayersDock
        from .module_wz_dialogs.properties_panel import PropertiesDock
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
        ctx.cmd_dock = cmd_dock

        dyn = DynamicInputWidget(canvas, translator, canvas)
        self._dyn_widget = dyn
        ctx.dyn_widget = dyn
        # Sync CRS label now and keep it live on every project CRS change
        dyn.set_crs_label(crs_mgr.short_label)
        crs_mgr.crsLabelChanged.connect(dyn.set_crs_label)

        # Shared input buffer — single source of truth for all typed text
        from .core.input.input_buffer import InputBuffer
        buf = InputBuffer()
        self._input_buffer = buf

        # Global keyboard interceptor — installed directly on the canvas so it
        # fires before QgsMapCanvas.keyPressEvent (which handles QGIS's own
        # snap 'S' shortcut).  QApplication-level filtering is unreliable in
        # QGIS because QgsApplication.notify() can process events before
        # application event filters see them.
        from .core.input.global_key_filter import GlobalKeyFilter
        # undo/redo callbacks are assigned after _do_undo/_do_redo are defined (below)
        _key_filter = GlobalKeyFilter(
            tool_mgr, buf,
            cmd_widget_getter=lambda: self._cmd_dock,
            dyn_getter=lambda: self._dyn_widget,
        )
        canvas.installEventFilter(_key_filter)
        self._global_key_filter = _key_filter

        props_dock = PropertiesDock(mw)
        self._props_dock = props_dock

        def _on_props_sel_changed():
            try:
                if sel.is_empty():
                    props_dock.clear_selection()
                    return
                lid, fid = next(iter(sel))
                layer = QgsProject.instance().mapLayer(lid)
                if layer is not None:
                    props_dock.update_feature(layer, fid)
                else:
                    props_dock.clear_selection()
            except RuntimeError:
                pass

        sel.selectionChanged.connect(_on_props_sel_changed)
        self._props_sel_slot = _on_props_sel_changed

        def _on_geom_changed_by_props(layer, fid):
            tool = canvas.mapTool()
            if tool and hasattr(tool, '_rebuild_grips'):
                tool._rebuild_grips()

        props_dock.geometry_changed.connect(_on_geom_changed_by_props)
        self._props_geom_slot = _on_geom_changed_by_props
        self._sel = sel

        layers_dock = CadLayersDock(cad_lyr_mgr, sel, storage, mw)
        iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, props_dock)
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, layers_dock)
        mw.splitDockWidget(props_dock, layers_dock, Qt.Vertical)
        layers_dock.hide()   # hidden by default; open with CADLAYERS / CL
        self._layers_dock = layers_dock

        snap_dialog = SnapSettingsDialog(snap_settings, snap_engine, mw)
        self._snap_dock = snap_dialog  # reuse attr so teardown loop still works

        # Maptool (select) button — first extra button after the plugin icon
        from .core import style as _cstyle
        maptool_act = QAction(_cstyle.maptool_toolbar_icon(), "Select", mw)
        maptool_act.setToolTip("Select  (Esc)")
        maptool_act.setCheckable(True)
        maptool_act.triggered.connect(tool_mgr.force_home)
        iface.addToolBarIcon(maptool_act)
        self._maptool_action = maptool_act
        tool_mgr.toolChanged.connect(
            lambda tool: maptool_act.setChecked(
                getattr(tool, '_tool_key', None) == 'select'
            )
        )

        # Snap button — Plugins toolbar
        snap_act = QAction(_cstyle.snap_toolbar_icon(), "Snap Settings", mw)
        snap_act.setToolTip("Snap Settings  (SNAP / OS)")
        snap_act.triggered.connect(snap_dialog.open_or_raise)
        iface.addToolBarIcon(snap_act)
        self._snap_action = snap_act

        # Ortho toggle button — right of snap icon
        ortho_act = QAction(_cstyle.ortho_toolbar_icon(), "Ortho  (F8)", mw)
        ortho_act.setToolTip("Toggle Ortho  (F8)")
        ortho_act.setCheckable(True)
        ortho_act.setChecked(ortho.enabled)
        ortho_act.triggered.connect(lambda checked: setattr(ortho, 'enabled', checked))
        iface.addToolBarIcon(ortho_act)
        self._ortho_action = ortho_act

        # Command-line aliases: SNAP / OS / OSNAP
        cmd_dock.register_ui_command("SNAP", "OS", "OSNAP",
                                     callback=snap_dialog.open_or_raise)

        # CAD Layers panel — hidden by default, toggle with CADLAYERS / CL
        def _toggle_cad_layers():
            if layers_dock.isVisible():
                layers_dock.hide()
            else:
                layers_dock.show()
                layers_dock.raise_()
        cmd_dock.register_ui_command("CADLAYERS", "CL",
                                     callback=_toggle_cad_layers)

        # Properties panel — hidden by default, toggle with PROPS / PR
        def _toggle_props():
            if props.isVisible():
                props.hide()
            else:
                props.show()
                props.raise_()
        cmd_dock.register_ui_command("PROPS", "PR", callback=_toggle_props)

        # HELP command — prints all available commands to the log
        def _on_help():
            inv: dict[str, list[str]] = {}
            for alias, key in registry._map.items():
                inv.setdefault(key, []).append(alias)

            cmd_dock.log("─" * 44, "#4488cc")
            cmd_dock.log("  Drawing & Modify tools", "#88ccff")
            cmd_dock.log("─" * 44, "#4488cc")
            for key in sorted(inv):
                tags = "  ".join(sorted(inv[key]))
                cmd_dock.log(f"  {key:<18} {tags}", "#aaddff")

            # Utility panels — read dynamically so new tools show automatically
            _builtin_ui = {"SNAP", "OS", "OSNAP", "HELP", "?"}
            ui_by_cb: dict[int, list[str]] = {}
            for alias, cb in cmd_dock._ui_commands.items():
                if alias not in _builtin_ui:
                    ui_by_cb.setdefault(id(cb), []).append(alias)
            if ui_by_cb:
                cmd_dock.log("─" * 44, "#4488cc")
                cmd_dock.log("  Utility panels & tools", "#88ccff")
                cmd_dock.log("─" * 44, "#4488cc")
                for aliases in sorted(ui_by_cb.values(), key=lambda a: sorted(a)[0]):
                    cmd_dock.log(f"  {'  '.join(sorted(aliases))}", "#aaddff")

            cmd_dock.log("─" * 44, "#4488cc")
            cmd_dock.log("  Built-in", "#88ccff")
            cmd_dock.log("─" * 44, "#4488cc")
            cmd_dock.log("  snap_settings      SNAP  OS  OSNAP", "#aaddff")
            cmd_dock.log("  help               HELP  ?", "#aaddff")
            cmd_dock.log("  clear log          CLS  CLEAR", "#aaddff")
            cmd_dock.log("  zoom extents       ZE  ZA  ZOOMEXTENTS", "#aaddff")
            cmd_dock.log("─" * 44, "#4488cc")

        cmd_dock.register_ui_command("HELP", "?", callback=_on_help)

        cmd_dock.register_ui_command("CLS", "CLEAR", callback=cmd_dock.clear_log)

        def _zoom_extents():
            from qgis.core import (QgsProject, QgsRectangle,
                                   QgsCoordinateTransform, QgsVectorLayer,
                                   QgsWkbTypes)
            canvas_crs = canvas.mapSettings().destinationCrs()
            extent = QgsRectangle()
            _GEOM = {QgsWkbTypes.GeometryType.PointGeometry,
                     QgsWkbTypes.GeometryType.LineGeometry,
                     QgsWkbTypes.GeometryType.PolygonGeometry}
            for layer in QgsProject.instance().mapLayers().values():
                if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                    continue
                if QgsWkbTypes.geometryType(layer.wkbType()) not in _GEOM:
                    continue
                try:
                    lyr_ext = layer.extent()
                    if lyr_ext.isNull() or lyr_ext.isEmpty():
                        continue
                    xform = QgsCoordinateTransform(
                        layer.crs(), canvas_crs, QgsProject.instance()
                    )
                    extent.combineExtentWith(
                        xform.transformBoundingBox(lyr_ext)
                    )
                except Exception:
                    continue
            if not extent.isNull() and not extent.isEmpty():
                canvas.setExtent(extent)
                canvas.refresh()

        cmd_dock.register_ui_command(
            "ZOOMEXTENTS", "ZE", "ZA",
            callback=_zoom_extents,
        )

        # 5. Wire InputBuffer to both display widgets ─────────────────────
        buf.textChanged.connect(dyn.on_buffer_text_changed)
        buf.cancelled.connect(dyn.on_buffer_cancelled)
        cmd_dock.connect_buffer(buf)

        # Clear buffer on tool switch (e.g. Esc mid-type)
        buf_ref = buf
        tool_mgr.toolChanged.connect(lambda *_: buf_ref.clear())

        # Update floating widget position on every canvas mouse-move
        def _on_canvas_xy(pt):
            dyn.update_position(pt)
        canvas.xyCoordinates.connect(_on_canvas_xy)
        self._canvas_xy_slot = _on_canvas_xy

        # Route committed text to the active tool
        def _on_text_input(text: str):
            active = tool_mgr.active_tool
            if active and hasattr(active, '_dispatch') and hasattr(active, '_translator'):
                last_pt = getattr(active, '_last_input_ref', None)
                sem = translator.translate_typed_text(text, last_pt, ctx)
                if sem:
                    active._dispatch(sem)
        cmd_dock.textValueEntered.connect(_on_text_input)

        # wire tool's inputModeChanged → dynamic input widget + command-line hint
        _dyn_prev_tool = [None]

        def _on_mode_hint(_, prompt: str):
            cmd_dock.log_hint(prompt)

        def _on_tool_for_dyn(tool):
            prev = _dyn_prev_tool[0]
            if prev is not None and hasattr(prev, 'inputModeChanged'):
                with contextlib.suppress(Exception):
                    prev.inputModeChanged.disconnect(dyn.set_mode)
                with contextlib.suppress(Exception):
                    prev.inputModeChanged.disconnect(_on_mode_hint)
            if prev is not None and hasattr(prev, 'promptChanged'):
                with contextlib.suppress(Exception):
                    prev.promptChanged.disconnect(dyn.set_prompt)
            if tool is not None and hasattr(tool, 'inputModeChanged'):
                tool.inputModeChanged.connect(dyn.set_mode)
                tool.inputModeChanged.connect(_on_mode_hint)
                if hasattr(tool, 'promptChanged'):
                    tool.promptChanged.connect(dyn.set_prompt)
                # Re-sync: activate() fires inputModeChanged BEFORE toolChanged
                # connects the signal, so the widget missed it — replay it now.
                mode   = getattr(tool, '_last_input_mode',   "")
                prompt = getattr(tool, '_last_input_prompt', "")
                dyn.set_mode(mode, prompt)
                cmd_dock.log_hint(prompt)
            else:
                dyn.set_mode("", "")   # hide when no tool / select tool
                cmd_dock.clear_hint()
            _dyn_prev_tool[0] = tool

        tool_mgr.toolChanged.connect(_on_tool_for_dyn)
        self._tool_for_dyn_slot = _on_tool_for_dyn

        # Wire DynamicInputWidget coordinate/value output → active tool dispatch
        from .core.events import SemanticEvent, EventType
        from qgis.core import QgsPointXY
        import math as _math

        def _on_dyn_xy(x: float, y: float):
            active = tool_mgr.active_tool
            if active is None or not hasattr(active, '_dispatch'):
                return
            sem = SemanticEvent(EventType.COORDINATE_ENTERED,
                                point=QgsPointXY(x, y), value=f"{x},{y}")
            active._dispatch(sem)

        def _on_dyn_polar(dist: float, bearing_deg: float):
            # bearing_deg: 0=North, clockwise → dx=sin(b)*dist, dy=cos(b)*dist
            active = tool_mgr.active_tool
            if active is None or not hasattr(active, '_dispatch'):
                return
            last_pt = getattr(active, '_last_input_ref', None)
            if last_pt:
                b   = _math.radians(bearing_deg)
                pt  = QgsPointXY(last_pt.x() + dist * _math.sin(b),
                                 last_pt.y() + dist * _math.cos(b))
            else:
                pt = QgsPointXY(0.0, dist)   # default: dist northward
            sem = SemanticEvent(EventType.COORDINATE_ENTERED, point=pt,
                                value=f"@{dist}<{bearing_deg}")
            active._dispatch(sem)

        def _on_dyn_value(v: float):
            active = tool_mgr.active_tool
            if active is None or not hasattr(active, '_dispatch'):
                return
            sem = SemanticEvent(EventType.VALUE_ENTERED, value=v)
            active._dispatch(sem)

        def _on_dyn_text(s: str):
            active = tool_mgr.active_tool
            if active is None or not hasattr(active, '_dispatch'):
                return
            sem = SemanticEvent(EventType.VALUE_ENTERED, value=s)
            active._dispatch(sem)

        dyn.coordinateEntered.connect(_on_dyn_xy)
        dyn.polarEntered.connect(_on_dyn_polar)
        dyn.valueEntered.connect(_on_dyn_value)
        dyn.textEntered.connect(_on_dyn_text)

        # unknown command → log
        _unknown_cmd = lambda t: cmd_dock.log(f"Unknown command: {t}", "#ff6666")
        dispatcher.unknownCommand.connect(_unknown_cmd)
        self._unknown_cmd_slot = _unknown_cmd

        # mid-command flag on cmd_dock
        # SelectTool is the idle/default state — command line stays open for
        # new commands.  Every other active tool is "mid-command" so typed
        # input is routed to that tool rather than dispatched as a new command.
        def _on_tool_for_cmdline(tool):
            key = getattr(tool, '_tool_key', '') if tool else ''
            if not tool or key == 'select':
                cmd_dock.set_mid_command(False)
            else:
                cmd_dock.set_mid_command(True, key.upper())
        tool_mgr.toolChanged.connect(_on_tool_for_cmdline)
        self._tool_for_cmdline_slot = _on_tool_for_cmdline

        # 6. Keyboard shortcuts ──────────────────────────────────────────
        self._shortcuts = _install_shortcuts(
            canvas, ortho, polar, snap_settings, tool_mgr, ortho_act
        )

        # Undo / Redo — command line aliases + Ctrl+Z / Ctrl+Y via GlobalKeyFilter
        # Ctrl+Z and Ctrl+Y are handled in GlobalKeyFilter (claimed in ShortcutOverride
        # so QGIS's own Edit→Undo/Redo ApplicationShortcut never fires alongside ours).
        _hist     = action_history
        _tool_mgr = tool_mgr
        _cmd      = cmd_dock

        def _do_undo():
            active = _tool_mgr.active_tool
            home   = getattr(_tool_mgr, 'home_tool', None)
            if active is not None and active is not home:
                # Mid-command: forward to the tool's own undo-step handler
                if hasattr(active, '_on_undo_step'):
                    active._on_undo_step()
                return
            if _hist.undo():
                _cmd.log("  Undone.", "#88ccff")
            else:
                _cmd.log("  Nothing to undo.", "#888888")

        def _do_redo():
            if _hist.redo():
                _cmd.log("  Redone.", "#88ccff")
            else:
                _cmd.log("  Nothing to redo.", "#888888")

        cmd_dock.register_ui_command("UNDO", "U", callback=_do_undo)
        cmd_dock.register_ui_command("REDO", "RE", callback=_do_redo)

        # Wire callbacks into GlobalKeyFilter so Ctrl+Z/Y are fully owned by us
        _key_filter._undo_callback = _do_undo
        _key_filter._redo_callback = _do_redo

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

        # 8. Extra utility panels (docks & tools) ────────────────────────
        self._register_extra_tools(cmd_dock, iface, canvas, mw)

        # 9. Reload CAD layers from GeoPackage when available ─────────────
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
                )
        storage.gatingChanged.connect(_reload_cad_layers)
        self._reload_layers_slot = _reload_cad_layers

        # 10. SelectTool is the permanent home — one instance, never via factory
        from .tools.selection.select_tool import SelectTool as _SelectTool
        _home = _SelectTool(canvas, ctx, translator)
        _home._tool_key = 'select'
        tool_mgr.set_home(_home)
        ctx.go_home    = tool_mgr.go_home
        ctx.launch_tool = dispatcher.dispatch_tool_key

    # ── extra utility panels ─────────────────────────────────────────────
    def _register_extra_tools(self, cmd_dock, iface, canvas, mw):
        """Register free utility tools immediately; gated tools only after sign-in."""
        from qgis.PyQt.QtCore import Qt
        from .core.auth_manager import AuthManager

        # Aliases that belong exclusively to gated tools.
        _GATED_ALIASES = (
            "PARCEL", "PP",   # Parcel Plotter
            "SLV",    "ST",   # Solve Topology
            "SPIKY",  "SG",   # Spiky Geometry
            "OA",             # Overlap Area
            "TS",     "TF",   # Topology Fixer
        )
        _gated_registered = [False]

        def _make_toggle(factory):
            state = [None]
            def _toggle():
                if state[0] is None:
                    state[0] = factory()
                    iface.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, state[0])
                    if self._extra_docks:
                        mw.tabifyDockWidget(self._extra_docks[0], state[0])
                    self._extra_docks.append(state[0])
                state[0].show()
                state[0].raise_()
            return _toggle

        # ── Free tools (always visible in suggestions) ────────────────────

        from .module_wz_dialogs.append_geometry import GeometryAppenderDock
        cmd_dock.register_ui_command("APPEND", "AG",
            callback=_make_toggle(lambda: GeometryAppenderDock(mw, iface, canvas)))

        from .module_wz_dialogs.crs_adjust import CrsAdjustDock
        cmd_dock.register_ui_command("CRSADJ", "CRS",
            callback=_make_toggle(lambda: CrsAdjustDock(mw)))

        from .module_wz_dialogs.feature_navigator import FeatureNavigatorDock
        cmd_dock.register_ui_command("NAV", "FN",
            callback=_make_toggle(lambda: FeatureNavigatorDock(canvas, mw)))

        def _activate_revert():
            if self._revert_tool is None:
                from .module_wz_dialogs.revert_geometry import RevertMapTool
                self._revert_tool = RevertMapTool(canvas, iface, cmd_dock)
            canvas.setMapTool(self._revert_tool)
        cmd_dock.register_ui_command("RV", "REV", callback=_activate_revert)

        _pd = self._props_dock
        cmd_dock.register_ui_command("PROPSDOCK", "PD",
            callback=lambda: (_pd.show(), _pd.raise_()))

        # Basemap — add Google Satellite or Hybrid XYZ tile layer
        def _add_basemap():
            from urllib.parse import quote
            from qgis.PyQt.QtWidgets import (
                QDialog, QVBoxLayout, QRadioButton,
                QDialogButtonBox, QLabel,
            )
            from qgis.core import QgsRasterLayer, QgsProject

            _BASEMAPS = [
                ("Google Satellite",        "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"),
                ("Google Satellite Hybrid", "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"),
            ]

            dlg = QDialog(mw)
            dlg.setWindowTitle("Add Basemap")
            dlg.setMinimumWidth(280)
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel("Select basemap to add:"))

            radios = []
            for name, _ in _BASEMAPS:
                rb = QRadioButton(name)
                layout.addWidget(rb)
                radios.append(rb)
            radios[0].setChecked(True)

            bbox = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel
            )
            bbox.accepted.connect(dlg.accept)
            bbox.rejected.connect(dlg.reject)
            layout.addWidget(bbox)

            if dlg.exec() != QDialog.DialogCode.Accepted:
                return

            chosen_name, chosen_url = next(
                (n, u) for rb, (n, u) in zip(radios, _BASEMAPS) if rb.isChecked()
            )
            encoded = quote(chosen_url, safe='/:')
            uri = f"type=xyz&url={encoded}&zmin=0&zmax=19"
            lyr = QgsRasterLayer(uri, chosen_name, 'wms')
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                cmd_dock.log(f'Basemap added: {chosen_name}', '#aaddff')
            else:
                cmd_dock.log(f'Failed to add basemap: {lyr.error().message()}', '#ff6666')

        cmd_dock.register_ui_command("BASEMAP", "BMP", callback=_add_basemap)

        def _open_feedback():
            from .module_wz_dialogs.feedback_dialog import FeedbackDialog
            FeedbackDialog(mw).exec()
        cmd_dock.register_ui_command("FEEDBACK", "FB", callback=_open_feedback)

        from .module_wz_dialogs.overlap_points import OverlapPointsDock
        cmd_dock.register_ui_command("OVERLAP", "OVP",
            callback=_make_toggle(lambda: OverlapPointsDock(canvas, mw)))

        def _open_layout_importer():
            from .module_wz_dialogs.layout_importer import ImportPrintDialog
            ImportPrintDialog(cmd_dock=cmd_dock, parent=mw).exec()
        cmd_dock.register_ui_command("IMPORT", "LI", callback=_open_layout_importer)

        _georef_ref = [None]
        def _activate_georef():
            if _georef_ref[0] is None:
                from .tools.georef.georeference_tool import GeoreferenceTool
                _georef_ref[0] = GeoreferenceTool(canvas, self._tool_context, self._translator)
                _georef_ref[0]._tool_key = 'georef'
            self._tool_manager.activate_tool(_georef_ref[0])
        cmd_dock.register_ui_command("GEOREF", "GR", callback=_activate_georef)

        _align_ref = [None]
        def _activate_align():
            if _align_ref[0] is None:
                from .tools.georef.align_tool import AlignTool
                _align_ref[0] = AlignTool(canvas, self._tool_context, self._translator)
                _align_ref[0]._tool_key = 'align'
            self._tool_manager.activate_tool(_align_ref[0])
        cmd_dock.register_ui_command("ALIGN", "AL", callback=_activate_align)

        # ── Gated tool callbacks (defined now, registered only after sign-in) ──

        def _open_parcel_plotter():
            from .module_wz_dialogs.parcel_plotter import ParcelPlotterDialog
            ParcelPlotterDialog(mw).exec()

        from .module_wz_dialogs.solve_topology_issues import SolveTopologyDock
        _cb_slv = _make_toggle(lambda: SolveTopologyDock(mw))

        from .module_wz_dialogs.spiky_geometry import SpikyGeomsDock
        _cb_spiky = _make_toggle(lambda: SpikyGeomsDock(canvas, mw))

        from .module_wz_dialogs.overlap_area import OverlapAreaDock
        _cb_oa = _make_toggle(lambda: OverlapAreaDock(mw))

        def _open_layout_importer():
            from .module_wz_dialogs.layout_importer import ImportPrintDialog
            ImportPrintDialog(cmd_dock=cmd_dock, parent=mw).exec()

        _tfix_ref = [None]
        def _activate_tfix():
            if _tfix_ref[0] is None:
                from .module_wz_dialogs.topology_solver import TopologySolver
                _tfix_ref[0] = TopologySolver(canvas, iface, cmd_dock)
                self._tfix_tool = _tfix_ref[0]
            canvas.setMapTool(_tfix_ref[0])

        _georef_ref = [None]
        def _activate_georef():
            if _georef_ref[0] is None:
                from .tools.georef.georeference_tool import GeoreferenceTool
                _georef_ref[0] = GeoreferenceTool(canvas, self._tool_context, self._translator)
                _georef_ref[0]._tool_key = 'georef'
            self._tool_manager.activate_tool(_georef_ref[0])

        _align_ref = [None]
        def _activate_align():
            if _align_ref[0] is None:
                from .tools.georef.align_tool import AlignTool
                _align_ref[0] = AlignTool(canvas, self._tool_context, self._translator)
                _align_ref[0]._tool_key = 'align'
            self._tool_manager.activate_tool(_align_ref[0])

        def _do_register_gated():
            if _gated_registered[0]:
                return
            _gated_registered[0] = True
            cmd_dock.register_ui_command("PARCEL", "PP", callback=_open_parcel_plotter)
            cmd_dock.register_ui_command("SLV",    "ST", callback=_cb_slv)
            cmd_dock.register_ui_command("SPIKY",  "SG", callback=_cb_spiky)
            cmd_dock.register_ui_command("OA",         callback=_cb_oa)
            cmd_dock.register_ui_command("TS",     "TF", callback=_activate_tfix)

        # ── WHOAMI — only entry point to sign-in ──────────────────────────

        def _whoami():
            mgr = AuthManager.get()
            if mgr.is_authenticated():
                cmd_dock.log(f"Signed in as: {mgr.username()}", "#44cc88")
            else:
                from .module_wz_dialogs.auth_dialog import AuthDialog
                from qgis.PyQt.QtWidgets import QDialog
                dlg = AuthDialog(mgr, mw)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    mgr.record_session()
                    _do_register_gated()
        cmd_dock.register_ui_command("WHOAMI", "WI", callback=_whoami)

        # ── SIGNOUT — hides gated tools from suggestions ──────────────────

        def _signout():
            mgr = AuthManager.get()
            if mgr.is_authenticated():
                name = mgr.username()
                mgr.clear_username()
                _gated_registered[0] = False
                cmd_dock.unregister_ui_command(*_GATED_ALIASES)
                cmd_dock.log(f"Signed out ({name}).", "#ffaa00")
            else:
                cmd_dock.log("Not signed in.", "#aaaaaa")
        cmd_dock.register_ui_command("SIGNOUT", "SO", callback=_signout)

        # Re-register gated tools immediately if already signed in from a prior session
        if AuthManager.get().is_authenticated():
            _do_register_gated()
            AuthManager.get().record_session()


    # ── teardown ──────────────────────────────────────────────────────────
    def _teardown(self):
        with contextlib.suppress(Exception):
            from .core.auth_manager import AuthManager
            AuthManager.reset()

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
            if self._crs_mgr:
                self._crs_mgr.unload()
        self._crs_mgr = None

        with contextlib.suppress(Exception):
            if self._action_history:
                QgsProject.instance().cleared.disconnect(self._action_history.clear)
                QgsProject.instance().readProject.disconnect(self._action_history.clear)
                self._action_history.clear()
        self._action_history = None

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
        with contextlib.suppress(Exception):
            sel = getattr(self, '_sel', None)
            if sel:
                slot = getattr(self, '_props_sel_slot', None)
                if slot:
                    sel.selectionChanged.disconnect(slot)
        self._tool_changed_slot     = None
        self._tool_for_cmdline_slot = None
        self._unknown_cmd_slot      = None
        self._reload_layers_slot    = None
        self._props_sel_slot        = None
        self._sel                   = None

        if self._maptool_action:
            with contextlib.suppress(Exception):
                self.iface.removeToolBarIcon(self._maptool_action)
                self._maptool_action.deleteLater()
            self._maptool_action = None

        if self._snap_action:
            with contextlib.suppress(Exception):
                self.iface.removeToolBarIcon(self._snap_action)
                self._snap_action.deleteLater()
            self._snap_action = None

        if self._ortho_action:
            with contextlib.suppress(Exception):
                self.iface.removeToolBarIcon(self._ortho_action)
                self._ortho_action.deleteLater()
            self._ortho_action = None

        if self._sel_overlay:
            with contextlib.suppress(Exception):
                self._sel_overlay.destroy()
            self._sel_overlay = None

        # Extra utility docks (lazily created by _register_extra_tools)
        for dock in getattr(self, '_extra_docks', []):
            with contextlib.suppress(Exception):
                self.iface.removeDockWidget(dock)
                dock.close()
                dock.deleteLater()
        self._extra_docks = []

        # Revert map tool
        if getattr(self, '_revert_tool', None) is not None:
            with contextlib.suppress(Exception):
                self.canvas.unsetMapTool(self._revert_tool)
            self._revert_tool = None

        # Topology fixer map tool
        if getattr(self, '_tfix_tool', None) is not None:
            with contextlib.suppress(Exception):
                self.canvas.unsetMapTool(self._tfix_tool)
            self._tfix_tool = None

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

        if self._global_key_filter:
            with contextlib.suppress(Exception):
                self.canvas.removeEventFilter(self._global_key_filter)
            self._global_key_filter = None

        if self._dyn_widget:
            with contextlib.suppress(Exception):
                self._dyn_widget.hide()
                self._dyn_widget.deleteLater()
            self._dyn_widget = None
            if self._tool_context:
                self._tool_context.dyn_widget = None

        self._tool_manager  = None
        self._dispatcher    = None
        self._storage       = None
        self._tool_context  = None
        self._input_buffer  = None


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
    from .tools.modify.crop_tool          import CropTool
    from .tools.modify.rotate_tool        import RotateTool
    from .tools.modify.scale_tool         import ScaleTool
    from .tools.modify.mirror_tool        import MirrorTool
    from .tools.modify.offset_tool        import OffsetTool
    from .tools.modify.trim_tool          import TrimTool
    from .tools.modify.extend_tool        import ExtendTool
    from .tools.modify.fillet_tool        import FilletTool
    from .tools.modify.array_tool         import ArrayTool
    from .tools.selection.erase_tool      import EraseTool
    from .tools.selection.stretch_tool    import StretchTool
    from .tools.vertex_edit.grip_edit_tool     import GripEditTool
    from .tools.vertex_edit.break_tool         import BreakTool
    from .tools.vertex_edit.join_tool          import JoinTool
    from .tools.modify.chamfer_tool        import ChamferTool
    from .tools.modify.explode_tool        import ExplodeTool
    from .tools.annotation.dimension_tool import DimensionTool, AutoDimensionTool
    from .tools.annotation.text_tool      import TextTool
    from .tools.drawing.auto_point        import AutoPointTool

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
        "erase":         EraseTool,       "crop":          CropTool,
        "stretch":       StretchTool,
        "grip_edit":     GripEditTool,
        "break":         BreakTool,
        "join":          JoinTool,
        "chamfer":       ChamferTool,     "explode":       ExplodeTool,
        "dimension":     DimensionTool,   "adimension":    AutoDimensionTool,
        "text":          TextTool,
        "autopoint":     AutoPointTool,
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
        ("fillet",        "FILLET",  "F"),
        ("array",         "ARRAY",   "AR"),
        ("erase",         "ERASE",   "E", "DEL"),
        ("crop",          "CROP",    "CR"),
        ("stretch",       "STRETCH", "S"),
        ("grip_edit",     "GRIPS",   "V"),
        ("break",         "BREAK",   "BR"),
        ("join",          "JOIN",    "J"),
        ("chamfer",       "CHAMFER", "CH"),
        ("explode",       "EXPLODE", "XP"),
        ("dimension",     "DIM",     "DIMLINEAR"),
        ("adimension",    "ADIM"),
        ("text",          "TEXT",    "T", "MTEXT"),
        ("autopoint",     "APOINT",  "APT"),
    ]:
        registry.register(key, *aliases)


# ── Keyboard shortcuts ────────────────────────────────────────────────────
def _install_shortcuts(canvas, ortho, polar, snap_settings, tool_mgr,
                       ortho_action=None):
    shortcuts = []

    def _sc(seq, slot):
        sc = QShortcut(QKeySequence(seq), canvas)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(slot)
        shortcuts.append(sc)

    def _toggle(obj, attr):
        setattr(obj, attr, not getattr(obj, attr))

    def _toggle_ortho():
        ortho.enabled = not ortho.enabled
        if ortho_action is not None:
            ortho_action.setChecked(ortho.enabled)

    _sc("F8", _toggle_ortho)
    _sc("F10", lambda: _toggle(polar, "enabled"))
    _sc("F3",  lambda: [snap_settings.set_enabled(k, not snap_settings.any_enabled())
                        for k in ["vertex", "midpoint", "center"]])
    def _do_escape():
        t = tool_mgr.active_tool
        if t is tool_mgr.home_tool:
            # Already home: let SelectTool clear selection/grips
            if hasattr(t, '_handle_esc'):
                t._handle_esc()
        elif t is not None:
            # Cancel current command and return home (deferred so tool's
            # keyPressEvent processes Esc before the tool is deactivated)
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, tool_mgr.go_home)

    _sc("Escape", _do_escape)
    return shortcuts
