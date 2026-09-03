# -*- coding: utf-8 -*-
"""
GlobalKeyFilter — canvas-level event filter that routes keyboard input to
either the CommandLineWidget (idle mode) or the shared InputBuffer (drawing
mode).

Installed on the QgsMapCanvas widget directly (NOT on QApplication).  This
guarantees the filter runs BEFORE QgsMapCanvas.keyPressEvent(), which is
where QGIS processes its own 'S' snap toggle and other canvas shortcuts.
The application-level notify() pipeline is bypassed entirely.

Idle mode  (home / select tool active)
  Type-anywhere command entry — user can type commands on the canvas without
  clicking the command-line dock first:
    Printable char → append to CommandLineWidget._input
    Backspace      → remove last char from CommandLineWidget._input
    Space / Enter  → execute CommandLineWidget._on_enter()
    Escape         → clear CommandLineWidget._input

Drawing mode  (any non-home tool active)
    Printable char → InputBuffer.append(ch)
    Backspace      → InputBuffer.backspace()
    Space / Enter  → InputBuffer.submit()
    Escape         → InputBuffer.cancel() then pass through so BaseTool
                     keyPressEvent also fires (returns False)

All other keys (arrows, F-keys, Tab, modifiers) pass through in both modes.
Ctrl and Alt combinations always pass through (QGIS / OS shortcuts).
Modal dialogs (attribute forms, save dialogs) always pass through.
"""

from qgis.PyQt.QtCore import QObject, QEvent, Qt
from qgis.PyQt.QtWidgets import QApplication


class GlobalKeyFilter(QObject):
    """
    Install on the map canvas; removed on plugin unload.

    Parameters
    ----------
    tool_manager : ToolManager
    input_buffer : InputBuffer
    cmd_widget_getter : callable -> CommandLineWidget | None
        Zero-argument callable; called lazily so the widget doesn't need to
        exist at construction time.  May also be None.
    """

    def __init__(self, tool_manager, input_buffer,
                 cmd_widget_getter=None, dyn_getter=None):
        super().__init__()
        self._tool_mgr  = tool_manager
        self._buffer    = input_buffer
        self._get_cmd   = cmd_widget_getter  # () -> CommandLineWidget
        self._get_dyn   = dyn_getter         # () -> DynamicInputWidget
        print("[UgSurv] GlobalKeyFilter created — canvas-level key capture")

    # ── Qt entry point ─────────────────────────────────────────────────────

    def eventFilter(self, obj, event) -> bool:
        try:
            return self._filter(event)
        except Exception as exc:
            import traceback
            print(f"[UgSurv GlobalKeyFilter] exception: {exc}")
            traceback.print_exc()
            return False

    # ── routing ────────────────────────────────────────────────────────────

    def _filter(self, event) -> bool:
        # ── ShortcutOverride ──────────────────────────────────────────────
        # Qt sends ShortcutOverride BEFORE KeyPress to decide whether a
        # registered QAction shortcut (like QGIS's 'S' snap toggle) should
        # fire.  If we accept() it here, Qt skips shortcut matching and
        # delivers the normal KeyPress instead — which our filter then owns.
        if event.type() == QEvent.ShortcutOverride:
            return self._claim_shortcut(event)

        # Only process key-press events from here on
        if event.type() != QEvent.KeyPress:
            return False

        # Never steal from modal dialogs (attribute forms, file dialogs, …)
        if QApplication.activeModalWidget() is not None:
            return False

        # Let Ctrl / Alt combos through — QGIS / OS shortcuts must work
        mods = event.modifiers()
        if mods & (Qt.ControlModifier | Qt.AltModifier):
            return False

        tool = self._tool_mgr.active_tool
        home = getattr(self._tool_mgr, 'home_tool', None)

        if tool is None or tool is home:
            return self._idle(event)
        return self._drawing(event, tool)

    def _claim_shortcut(self, event) -> bool:
        """Accept ShortcutOverride for every key we handle.

        Accepting tells Qt "this widget owns the key — do not fire any
        registered shortcut for it."  The matching KeyPress is then delivered
        normally and caught by _idle / _drawing.

        We claim in both idle and drawing modes so that QGIS shortcuts like
        'S' (snap toggle) never fire while the plugin canvas is active.
        """
        if QApplication.activeModalWidget() is not None:
            return False
        mods = event.modifiers()
        if mods & (Qt.ControlModifier | Qt.AltModifier):
            return False
        if self._tool_mgr.active_tool is None:
            return False

        key = event.key()
        ch  = event.text()
        if (key in (Qt.Key_Backspace, Qt.Key_Return, Qt.Key_Enter,
                    Qt.Key_Space, Qt.Key_Escape, Qt.Key_Tab)
                or (ch and ch.isprintable() and ch != '\t')):
            event.accept()   # block QGIS shortcut; KeyPress still follows
        return False         # do not consume — let the event reach the canvas

    # ── idle mode (home / select tool) ─────────────────────────────────────

    def _idle(self, event) -> bool:
        """Type-anywhere: forward canvas keystrokes to the command-line input."""
        # DynamicInputWidget gets first refusal even in idle mode — handles
        # radius entry while a circle grip is armed on the select/home tool.
        dyn = self._get_dyn() if self._get_dyn else None
        if dyn is not None and dyn.isVisible() and dyn.handle_key(event):
            return True

        cmd = self._get_cmd() if self._get_cmd else None
        if cmd is None:
            return False
        inp = getattr(cmd, '_input', None)
        if inp is None:
            return False

        key = event.key()

        if key == Qt.Key_Escape:
            if inp.text():
                inp.clear()
                return True
            return False   # nothing to clear — let QGIS / SelectTool handle it

        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            cmd._on_enter()
            return True

        if key == Qt.Key_Backspace:
            t = inp.text()
            if t:
                inp.setText(t[:-1])
                return True
            return False  # empty input — pass through so the tool can handle it

        ch = event.text()
        if ch and ch.isprintable() and ch != '\t':
            inp.setText(inp.text() + ch)
            return True

        return False

    # ── drawing mode (non-home tool active) ────────────────────────────────

    def _drawing(self, event, tool) -> bool:
        """Forward canvas keystrokes to the DynamicInputWidget, then InputBuffer."""
        # DynamicInputWidget gets first refusal: interactive fields for X/Y/value entry
        dyn = self._get_dyn() if self._get_dyn else None
        if dyn is not None and dyn.handle_key(event):
            return True

        # Fall back: route to shared InputBuffer (legacy / non-interactive modes)
        key = event.key()

        if key == Qt.Key_Backspace:
            self._buffer.backspace()
            return True

        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self._buffer.text:
                self._buffer.submit()
                return True
            # Empty buffer — let the key reach the tool's keyPressEvent so it
            # can handle confirmation (e.g. "confirm selected features").
            return False

        if key == Qt.Key_Escape:
            self._buffer.cancel()
            return False   # pass through → BaseTool.keyPressEvent handles it

        ch = event.text()
        if ch and ch.isprintable() and ch != '\t':
            self._buffer.append(ch)
            return True

        return False
