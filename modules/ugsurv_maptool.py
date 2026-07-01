from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor, QPixmap, QPainter, QPen
from PyQt5.QtWidgets import QApplication


def _red_crosshair_cursor():
    size, c, gap, box = 41, 20, 5, 3
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setPen(QPen(QColor(220, 30, 30), 1))
    p.drawLine(0, c, c - gap, c)
    p.drawLine(c + gap, c, size - 1, c)
    p.drawLine(c, 0, c, c - gap)
    p.drawLine(c, c + gap, c, size - 1)
    p.drawRect(c - box, c - box, box * 2, box * 2)
    p.end()
    return QCursor(px, c, c)


_RED_CURSOR = _red_crosshair_cursor()


class UgsurvMaptool(QgsMapTool):
    """
    The single permanent map tool for the Ugsurv plugin.

    Set once on the canvas when the plugin activates and stays there for the
    entire session.  Individual drawing tools (CircleDrawer, PolylineDrawer,
    etc.) register themselves here via set_tool() and receive forwarded canvas
    events.  They never own the canvas directly.

    Keyboard routing (CAD-style)
    ----------------------------
    Printable characters typed on the canvas are always redirected to the
    terminal input, so the user can type commands or values without first
    clicking back into the terminal dock.  Enter with no active tool also
    submits the terminal command.  Control keys (Escape, BackSpace, Space,
    Enter during a tool) are forwarded to the active tool so tool-specific
    shortcuts (undo vertex, finish polyline, cancel, etc.) keep working.

    Forward-looking hooks
    ---------------------
    register_key(qt_key, callback)  — bind a Qt.Key_* constant to a callable
                                      that fires BEFORE the active tool sees it.
    unregister_key(qt_key)          — remove a global key binding.
    """

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.setCursor(_RED_CURSOR)
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self._active_tool = None
        self._key_handlers = {}

    def activate(self):
        super().activate()
        self.canvas.setCursor(_RED_CURSOR)

    # ------------------------------------------------------------------
    # Tool slot management
    # ------------------------------------------------------------------

    def set_tool(self, tool):
        self._evict()
        tool._maptool = self
        self._active_tool = tool
        tool.activate()
        self.canvas.setCursor(_RED_CURSOR)   # re-apply after delegate may reset it

    def clear_tool(self):
        self._active_tool = None
        self.canvas.setFocus()
        self.terminal_dock.command.setFocus()

    def _evict(self):
        if self._active_tool is None:
            return
        old = self._active_tool
        self._active_tool = None
        try:
            old.deactivate()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Global key hooks
    # ------------------------------------------------------------------

    def register_key(self, qt_key, callback):
        self._key_handlers[qt_key] = callback

    def unregister_key(self, qt_key):
        self._key_handlers.pop(qt_key, None)

    # ------------------------------------------------------------------
    # QgsMapTool event forwarding
    # ------------------------------------------------------------------

    def canvasMoveEvent(self, event):
        if self._active_tool:
            self._active_tool.canvasMoveEvent(event)

    def canvasPressEvent(self, event):
        if self._active_tool:
            self._active_tool.canvasPressEvent(event)

    def canvasReleaseEvent(self, event):
        if self._active_tool and hasattr(self._active_tool, 'canvasReleaseEvent'):
            self._active_tool.canvasReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._active_tool and hasattr(self._active_tool, 'mouseDoubleClickEvent'):
            self._active_tool.mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        handler = self._key_handlers.get(event.key())
        if handler:
            handler(event)
            return

        key  = event.key()
        text = event.text()

        if text and text.isprintable():
            self._redirect_to_terminal(event)
            return

        if key in (Qt.Key_Return, Qt.Key_Enter) and self._active_tool is None:
            self._redirect_to_terminal(event)
            return

        if self._active_tool:
            self._active_tool.keyPressEvent(event)

    def _redirect_to_terminal(self, event):
        cmd = self.terminal_dock.command
        QApplication.sendEvent(cmd, event)
        cmd.setFocus()

    def keyReleaseEvent(self, event):
        if self._active_tool and hasattr(self._active_tool, 'keyReleaseEvent'):
            self._active_tool.keyReleaseEvent(event)
