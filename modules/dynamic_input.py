"""
Shared floating dynamic-input widget for QGIS map tools.

Create one DynamicInput per tool, configure with named fields, then:
  show(x, y)                 – display near a canvas position; focuses first field
  update(x, y, placeholders) – called every canvasMoveEvent to follow cursor
                               and refresh placeholder text live
  hide()                     – dismiss without committing
  destroy()                  – delete the widget (call in tool deactivate)

Assign callbacks before or after construction:
  on_commit  = fn(values: dict[key -> str])   – fires on Enter or Space
  on_cancel  = fn()                           – fires on Escape

Key behaviour
─────────────
  Tab / Shift-Tab  cycle focus forward / backward between fields
  Down / Right     move to next field
  Up / Left        move to previous field
  Enter or Space   commit  (placeholder value used when field is empty)
  Escape           cancel

Implementation note
───────────────────
The widget is a plain QWidget parented to the canvas (NOT a QGraphicsProxyWidget).
This is intentional: proxy-embedded widgets never hold real OS keyboard focus —
the canvas does — so QGIS QAction shortcuts (Tab, letter keys, …) fire before any
proxy QLineEdit can intercept them.  A canvas-child QWidget gets genuine OS focus
when clicked or focused explicitly, which lets the QLineEdit's built-in
ShortcutOverride acceptance block QGIS shortcuts naturally.

Terminal sync
─────────────
While visible the widget mirrors its fields to/from terminal_dock.command
using comma-separated text (one value per field).
"""

from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtWidgets import (
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

_STYLE = (
    "QWidget   { background: #1a1a2e; border: 1px solid #4a9eff; }"
    "QLabel    { color: #7fa8d4; font-size: 9px; border: none;"
    "            padding: 2px 6px 1px 6px; }"
    "QLineEdit { background: transparent; color: #e8e8e8; border: none;"
    "            border-top: 1px solid #2a2a4e; padding: 3px 6px;"
    "            font-size: 11px; min-width: 120px; }"
)


class DynamicInput(QObject):
    """
    Floating multi-field AutoCAD-style input widget for QGIS map tools.

    Parameters
    ----------
    canvas : QgsMapCanvas
    terminal_dock : TerminalDialog
    fields : list of {"key": str, "label": str}
        Ordered field specifications.
    """

    def __init__(self, canvas, terminal_dock, fields: list):
        super().__init__()
        self.canvas    = canvas
        self._terminal = terminal_dock
        self._fields   = fields
        self._keys     = [f["key"] for f in fields]
        self._lines    = {}   # key -> QLineEdit
        self._syncing  = False

        self.on_commit = None   # callable(values: dict[key -> str])
        self.on_cancel = None   # callable()

        self._widget = self._build()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _build(self) -> QWidget:
        # Plain QWidget parented to canvas so it floats above map content
        # and holds real OS keyboard focus.
        container = QWidget(self.canvas)
        container.setStyleSheet(_STYLE)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        for f in self._fields:
            layout.addWidget(QLabel(f["label"]))
            line = QLineEdit()
            line.returnPressed.connect(self._commit)
            line.installEventFilter(self)
            layout.addWidget(line)
            self._lines[f["key"]] = line

        container.adjustSize()
        container.hide()
        return container

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._widget.isVisible()

    def show(self, canvas_x: float, canvas_y: float):
        """Show the widget at canvas pixel position and keep focus on terminal."""
        for line in self._lines.values():
            line.clear()
        self._widget.move(int(canvas_x) + 15, int(canvas_y) + 15)
        self._widget.show()
        self._widget.raise_()
        self._connect_terminal()
        # Keep focus on the terminal so QGIS keyboard shortcuts don't interfere.
        self._terminal.command.setFocus()

    def hide(self):
        """Dismiss without committing."""
        self._widget.hide()
        self._disconnect_terminal()
        for line in self._lines.values():
            line.clear()

    def update(self, canvas_x: float, canvas_y: float, placeholders: dict):
        """
        Reposition and refresh placeholder texts every canvasMoveEvent.

        The placeholder value is what gets committed when the user presses
        Enter without having typed anything.
        """
        if not self._widget.isVisible():
            return
        self._widget.move(int(canvas_x) + 15, int(canvas_y) + 15)
        for key, ph in placeholders.items():
            if key in self._lines:
                self._lines[key].setPlaceholderText(str(ph))

    def destroy(self):
        """Delete the widget (call from tool deactivate)."""
        self.hide()
        try:
            self._widget.deleteLater()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Commit / cancel
    # ------------------------------------------------------------------

    def _commit(self):
        """Read all field values (falling back to placeholder) and fire on_commit."""
        values = {
            key: (line.text() or line.placeholderText())
            for key, line in self._lines.items()
        }
        self.hide()
        if self.on_commit:
            self.on_commit(values)

    # ------------------------------------------------------------------
    # Event filter — installed on each QLineEdit
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        if obj not in self._lines.values():
            return super().eventFilter(obj, event)

        # ShortcutOverride: accept every key so QGIS QAction shortcuts (Tab,
        # letter keys, etc.) cannot fire while one of our fields has focus.
        # Returning False lets the KeyPress event still arrive normally.
        if event.type() == QEvent.ShortcutOverride:
            event.accept()
            return False

        if event.type() != QEvent.KeyPress:
            return super().eventFilter(obj, event)

        key  = event.key()
        mods = event.modifiers()

        if key == Qt.Key_Escape:
            self.hide()
            if self.on_cancel:
                self.on_cancel()
            return True

        if key == Qt.Key_Tab:
            self._cycle_focus(forward=not bool(mods & Qt.ShiftModifier))
            return True

        if key in (Qt.Key_Down, Qt.Key_Right):
            self._cycle_focus(forward=True)
            return True

        if key in (Qt.Key_Up, Qt.Key_Left):
            self._cycle_focus(forward=False)
            return True

        if key == Qt.Key_Space:
            self._commit()
            return True

        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Focus cycling
    # ------------------------------------------------------------------

    def _focused_key(self):
        for key, line in self._lines.items():
            if line.hasFocus():
                return key
        return None

    def _focused_line(self):
        for line in self._lines.values():
            if line.hasFocus():
                return line
        return None

    def _cycle_focus(self, forward: bool = True):
        keys    = self._keys
        current = self._focused_key()
        if current is None or current not in keys:
            self._lines[keys[0]].setFocus()
            return
        idx     = keys.index(current)
        new_idx = (idx + (1 if forward else -1)) % len(keys)
        self._lines[keys[new_idx]].setFocus()

    # ------------------------------------------------------------------
    # Terminal bidirectional sync
    # ------------------------------------------------------------------

    def _connect_terminal(self):
        self._terminal.command.textChanged.connect(self._sync_from_terminal)
        for line in self._lines.values():
            line.textChanged.connect(self._sync_to_terminal)

    def _disconnect_terminal(self):
        try:
            self._terminal.command.textChanged.disconnect(self._sync_from_terminal)
        except Exception:
            pass
        for line in self._lines.values():
            try:
                line.textChanged.disconnect(self._sync_to_terminal)
            except Exception:
                pass

    def _sync_from_terminal(self, text: str):
        """Mirror terminal command field → floating fields (comma-split)."""
        if self._syncing:
            return
        self._syncing = True
        parts = text.split(',')
        for i, key in enumerate(self._keys):
            self._lines[key].setText(parts[i].strip() if i < len(parts) else '')
        self._syncing = False

    def _sync_to_terminal(self, _=None):
        """Mirror floating fields → terminal command field (comma-joined)."""
        if self._syncing:
            return
        self._syncing = True
        texts = [self._lines[k].text() for k in self._keys]
        while texts and not texts[-1]:
            texts.pop()
        self._terminal.command.setText(','.join(texts))
        self._syncing = False
