# -*- coding: utf-8 -*-
"""
BaseTool — abstract superclass for every drafting/modify/vertex tool.

State machine:
    IDLE → SELECTING → ACTING → PENDING_CONFIRM → (commit | cancel) → IDLE

Subclasses override _on_event() and _on_hover().  They never parse raw Qt
events directly; that is InputTranslator's job.
"""

from enum import Enum, auto
from abc import abstractmethod

from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor
from qgis.core import QgsWkbTypes

from .events import SemanticEvent, EventType, SnapType
from . import style as _style


class ToolState(Enum):
    IDLE            = auto()
    SELECTING       = auto()
    ACTING          = auto()
    PENDING_CONFIRM = auto()


class BaseTool(QgsMapTool):
    """All CAD tools inherit from this class."""

    # Subclasses set this to give the cursor a distinct shape
    CURSOR = Qt.CursorShape.CrossCursor

    # Emitted when the tool changes what kind of input it expects.
    # args: (mode, prompt)  mode ∈ {"xy", "polar", "value"}
    inputModeChanged = pyqtSignal(str, str)

    # Emitted to update only the prompt label without rebuilding fields.
    promptChanged = pyqtSignal(str)

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas)
        self._ctx            = tool_context
        self._translator     = input_translator
        self._state          = ToolState.IDLE
        self._pending        = None
        self._rubber_bands   = []
        self._esc_count      = 0
        self._snap_marker: QgsVertexMarker | None = None
        self._last_input_ref = None   # last committed point; used for @rel / polar entry
        self._ext_guide_rb   = None   # extension-line guide rubber band
        self.setCursor(self._get_cursor())

    # ── public read ──────────────────────────────────────────────────────
    @property
    def state(self) -> ToolState:
        return self._state

    # ── lifecycle ────────────────────────────────────────────────────────
    def activate(self):
        super().activate()
        self._transition(ToolState.IDLE)
        self.canvas().setCursor(self._get_cursor())

    def deactivate(self):
        self._do_cancel()
        self._clear_snap_marker()
        super().deactivate()

    # ── modal-interrupt suspend/resume ───────────────────────────────────
    def suspend(self) -> dict:
        """Called by ToolManager for a modal interrupt (pan/zoom/osnap)."""
        return {
            "state":   self._state,
            "pending": self._pending,
            "esc":     self._esc_count,
        }

    def resume(self, snapshot: dict):
        """Restore state after modal interrupt ends."""
        self._state     = snapshot["state"]
        self._pending   = snapshot["pending"]
        self._esc_count = snapshot["esc"]

    # ── Qt canvas entry points (route through InputTranslator) ───────────
    def canvasPressEvent(self, event):
        sem = self._translator.translate_press(event, self._ctx)
        self._dispatch(sem)

    def canvasMoveEvent(self, event):
        sem = self._translator.translate_move(event, self._ctx)
        self._update_snap_marker(sem)
        self._on_hover(sem)

    def canvasReleaseEvent(self, event):
        pass  # most tools act on press; subclasses may override

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()   # prevent QGIS canvas from unloading the tool on Esc
        # Key routing to DynamicInputWidget is handled by GlobalKeyFilter
        # (installed on QApplication), which fires before this method.
        # Anything reaching here was intentionally passed through.
        sem = self._translator.translate_key(event, self._ctx)
        self._dispatch(sem)

    # ── internal dispatch ────────────────────────────────────────────────
    def _dispatch(self, sem: SemanticEvent):
        if sem is None:
            return

        if sem.type == EventType.CANCEL:
            self._handle_esc()
            return
        if sem.type == EventType.UNDO_STEP:
            self._on_undo_step()
            return

        self._esc_count = 0          # any non-Esc event resets the counter
        self._on_event(sem)

    def _handle_esc(self):
        self._esc_count += 1
        self._do_cancel()
        if self._esc_count >= 2:
            self._transition(ToolState.IDLE)
        # Return to the permanent home tool (SelectTool) after this event cycle
        go_home = getattr(self._ctx, 'go_home', None)
        if go_home and callable(go_home):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go_home)

    # ── rubber-band helpers ───────────────────────────────────────────────
    def _new_rubber_band(self, geom_type, color=None, width=None):
        rb = QgsRubberBand(self.canvas(), geom_type)
        if color is None:
            color = _style.RB_DRAW
        if width is None:
            width = _style.RB_WIDTH
        rb.setColor(color)
        rb.setWidth(width)
        rb.setLineStyle(_style.RB_LINE_STYLE)
        self._rubber_bands.append(rb)
        return rb

    def _clear_rubber_bands(self):
        scene = self.canvas().scene()
        for rb in self._rubber_bands:
            try:
                scene.removeItem(rb)
            except Exception:
                try:
                    rb.reset()
                    rb.hide()
                except Exception:  # nosec B110
                    pass
        self._rubber_bands.clear()

    # ── snap marker ───────────────────────────────────────────────────────
    _SNAP_STYLES = {
        SnapType.VERTEX:        (_style.SNAP_ICON['endpoint'],     _style._CC_COLOR),
        SnapType.POINT:         (_style.SNAP_ICON['point'],        _style._CC_COLOR),
        SnapType.MIDPOINT:      (_style.SNAP_ICON['midpoint'],     _style._CC_COLOR),
        SnapType.CENTER:        (_style.SNAP_ICON['center'],       _style._CC_COLOR),
        SnapType.INTERSECTION:  (_style.SNAP_ICON['intersection'], _style._CC_COLOR),
        SnapType.PERPENDICULAR: (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
        SnapType.EXTENSION:     (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
        SnapType.GRID:          (QgsVertexMarker.ICON_CROSS,       _style.SNAP_GRID_COLOR),
        SnapType.SELF:          (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
        SnapType.NEAREST:       (_style.SNAP_ICON['nearest'],      _style._CC_COLOR),
    }

    def _get_cursor(self) -> QCursor:
        if self.CURSOR == Qt.CursorShape.CrossCursor:
            return _style.cad_crosshair_cursor()
        return QCursor(self.CURSOR)

    def _update_snap_marker(self, sem: SemanticEvent):
        if sem is None or sem.snap_type == SnapType.NONE or sem.point is None:
            if self._snap_marker is not None:
                self._snap_marker.hide()
            return
        if self._snap_marker is None:
            self._snap_marker = QgsVertexMarker(self.canvas())
            self._snap_marker.setIconSize(_style.SNAP_ICON_SIZE)
            self._snap_marker.setPenWidth(_style.SNAP_PEN_WIDTH)
        icon_type, color = self._SNAP_STYLES.get(
            sem.snap_type, (QgsVertexMarker.ICON_X, _style._CC_COLOR)
        )
        self._snap_marker.setIconType(icon_type)
        self._snap_marker.setColor(color)
        self._snap_marker.setCenter(sem.point)
        self._snap_marker.show()

    def _clear_snap_marker(self):
        if self._snap_marker is not None:
            try:
                self.canvas().scene().removeItem(self._snap_marker)
            except Exception:  # nosec B110
                pass
            self._snap_marker = None

    # ── state helper ─────────────────────────────────────────────────────
    def _transition(self, new_state: ToolState):
        self._state = new_state
        self._esc_count = 0

    def cancel(self):
        """Public cancel — used by ToolManager and keyboard shortcuts."""
        self._handle_esc()

    def _go_home(self):
        """Return to the home (select) tool after a command finishes."""
        go_home = getattr(self._ctx, 'go_home', None)
        if go_home and callable(go_home):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go_home)

    def _do_cancel(self):
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        self._clear_rubber_bands()
        self._ext_guide_rb = None   # cleared by _clear_rubber_bands; null the ref
        self._on_cancel_hook()
        self._transition(ToolState.IDLE)

    # ── extension-snap helpers ────────────────────────────────────────────
    def _update_extension_guide(self, snap_type, snap_point):
        """Show a guide line from the extension endpoint to the cursor.
        Call from _on_hover whenever the snap type may be EXTENSION."""
        snap_engine = getattr(self._ctx, 'snap_engine', None)
        if snap_type != SnapType.EXTENSION or snap_point is None or not snap_engine:
            if self._ext_guide_rb:
                self._ext_guide_rb.reset(QgsWkbTypes.LineGeometry)
            return
        ext_prov = snap_engine._providers.get("extension")
        if not ext_prov or not ext_prov.active_endpoint:
            if self._ext_guide_rb:
                self._ext_guide_rb.reset(QgsWkbTypes.LineGeometry)
            return
        if self._ext_guide_rb is None:
            self._ext_guide_rb = self._new_rubber_band(
                QgsWkbTypes.LineGeometry, _style.RB_EDGE, _style.RB_WIDTH
            )
        self._ext_guide_rb.reset(QgsWkbTypes.LineGeometry)
        self._ext_guide_rb.addPoint(ext_prov.active_endpoint, False)
        self._ext_guide_rb.addPoint(snap_point, True)

    def _try_extension_distance(self, value):
        """Convert a typed distance to a point along the active extension line.
        Returns QgsPointXY if extension snap is active, else None."""
        snap_engine = getattr(self._ctx, 'snap_engine', None)
        if not snap_engine:
            return None
        ext_prov = snap_engine._providers.get("extension")
        if not ext_prov or not ext_prov.active_endpoint or not ext_prov.active_direction:
            return None
        try:
            dist = float(value)
        except (TypeError, ValueError):
            return None
        from qgis.core import QgsPointXY
        ep       = ext_prov.active_endpoint
        ux, uy   = ext_prov.active_direction
        return QgsPointXY(ep.x() + dist * ux, ep.y() + dist * uy)

    # ── dynamic input ─────────────────────────────────────────────────────
    def _request_input(self, mode: str, prompt: str = ""):
        """Tell the DynamicInputWidget what fields to show next."""
        self._last_input_mode   = mode
        self._last_input_prompt = prompt
        self.inputModeChanged.emit(mode, prompt)

    def _update_prompt(self, prompt: str):
        """Update only the prompt label text without rebuilding fields."""
        self.promptChanged.emit(prompt)

    # ── abstract interface for subclasses ─────────────────────────────────
    @abstractmethod
    def _on_event(self, sem: SemanticEvent):
        """Handle a non-Esc semantic event.  Must be implemented by every tool."""

    def _on_hover(self, sem: SemanticEvent):
        """Update live preview on mouse-move.  Default: no-op."""

    def _on_undo_step(self):
        """Ctrl+Z mid-command.  Default: cancel step."""
        self._do_cancel()

    def _on_cancel_hook(self):
        """Called just before state resets to IDLE.  Subclasses may clean up."""
