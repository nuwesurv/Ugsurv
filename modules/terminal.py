from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout,
    QTextEdit, QLineEdit, QSpacerItem, QSizePolicy,
    QListWidget, QListWidgetItem,
)
from PyQt5.QtGui import QIcon, QTextCursor
from PyQt5.QtCore import Qt, QEvent, QPoint
from qgis.core import QgsPointXY
import os
from .key_filter import KeyPressFilter


class TerminalDialog(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Terminal", parent)

        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        self.setWindowIcon(QIcon(icon_path))

        self.setAllowedAreas(
            Qt.LeftDockWidgetArea |
            Qt.RightDockWidgetArea |
            Qt.BottomDockWidgetArea
        )

        container = QWidget()
        self._container = container          # kept for overlay positioning
        self.setWidget(container)
        self.setMinimumHeight(80)

        main_layout = QVBoxLayout(container)

        # ── Terminal output ──────────────────────────────────────────────
        self.commandDisplay = QTextEdit()
        self.commandDisplay.setReadOnly(True)
        self.commandDisplay.setText("Loading plugin 0.01ms...\nPlugin has been loaded 🧪 0.01ms...")
        self.commandDisplay.setStyleSheet("color: #5f5f5f;")
        self.commandDisplay.setMinimumHeight(30)
        self.commandDisplay.textChanged.connect(self.scrollUpcommandDisplay)
        main_layout.addWidget(self.commandDisplay)

        main_layout.addItem(QSpacerItem(0, 10, QSizePolicy.Minimum, QSizePolicy.Expanding))

        # ── State ────────────────────────────────────────────────────────
        self.commandOutputText = "Loading plugin 0.01ms...\nPlugin has been loaded 🧪 0.01ms..."
        self.commandHistory    = ['']
        self.historyIndex      = 0
        self.active_input_handler = None   # set by tools that need typed input
        self._commands            = []     # populated via set_commands()
        self.on_activate_maptool  = None   # set by Ugsurv; called to restore the map tool
        self.on_canvas_key        = None   # set by Ugsurv; forwards key events to canvas

        # ── Suggestion list (floating overlay — NOT in layout) ───────────
        # Qt.NoFocus: the list never steals keyboard focus from the command
        # input, so Up/Down navigation is done purely through setCurrentRow().
        self.suggestion_list = QListWidget(container)
        self.suggestion_list.setFocusPolicy(Qt.NoFocus)
        self.suggestion_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.suggestion_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.suggestion_list.setStyleSheet("""
            QListWidget {
                background: #ffffff;
                border: 1px solid #b0b8c8;
                border-radius: 4px;
                color: #222222;
                font-size: 11px;
                outline: none;
            }
            QListWidget::item {
                padding: 2px 10px;
                border-bottom: 1px solid #dde4ef;
            }
            QListWidget::item:last-child {
                border-bottom: none;
            }
            QListWidget::item:selected {
                background: #0078d4;
                color: #ffffff;
            }
            QListWidget::item:hover {
                background: #ddeeff;
                color: #222222;
            }
        """)
        self.suggestion_list.hide()

        # ── Command input ────────────────────────────────────────────────
        self.command = QLineEdit()
        self.command.setMinimumWidth(200)
        self.command.setPlaceholderText('command here...')

        # History filters (installed first → run last, LIFO order)
        self.key_filter1 = KeyPressFilter('up',    self.commandRepeatUp)
        self.key_filter2 = KeyPressFilter('down',  self.commandRepeatDown)
        self.key_filter3 = KeyPressFilter('space', self.commandRepeatPrevCommand)
        self.command.installEventFilter(self.key_filter1)
        self.command.installEventFilter(self.key_filter2)
        self.command.installEventFilter(self.key_filter3)
        # Installed last → runs first.  Intercepts Up/Down/Tab/Escape for
        # the suggestion list before the history filters get to see them.
        self.command.installEventFilter(self)

        self.command.textChanged.connect(self._update_suggestions)
        self.command.returnPressed.connect(self.suggestion_list.hide)
        self.suggestion_list.itemClicked.connect(self._accept_suggestion)

        # Catch mouse presses on the output display and the container background
        # so that clicking anywhere in the terminal triggers map tool reactivation.
        container.installEventFilter(self)
        self.commandDisplay.installEventFilter(self)

        main_layout.addWidget(self.command)

    # ────────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────────

    def set_commands(self, commands):
        """Supply the full list of valid commands for suggestion matching."""
        self._commands = list(commands)

    def request_input(self, prompt: str, callback):
        """Ask the user to type a value; callback(text) fires on the next Enter."""
        self.active_input_handler = callback
        self.suggestion_list.hide()
        self.command.setPlaceholderText(prompt)
        self.command.setFocus()

    def clear_input_handler(self):
        self.active_input_handler = None
        self.command.setPlaceholderText('command here...')

    # ────────────────────────────────────────────────────────────────────
    # Suggestion logic
    # ────────────────────────────────────────────────────────────────────

    def _update_suggestions(self, text):
        """Rebuild and reposition the floating suggestion list."""
        text = text.strip()
        # Suppress suggestions when a tool is waiting for a value (e.g. radius)
        if not text or self.active_input_handler:
            self.suggestion_list.hide()
            return

        text_lower = text.lower()
        matches = [
            cmd for cmd in self._commands
            if cmd.lower().startswith(text_lower) and cmd.lower() != text_lower
        ]

        if not matches:
            self.suggestion_list.hide()
            return

        self.suggestion_list.clear()
        self.suggestion_list.setCurrentRow(-1)   # clear previous selection
        for cmd in matches[:8]:
            self.suggestion_list.addItem(QListWidgetItem(cmd))

        self._reposition_list()

    def _reposition_list(self):
        """Place the suggestion list as a floating overlay above the command input."""
        count = self.suggestion_list.count()
        if count == 0:
            return

        cmd_w = self.command.width()
        if cmd_w <= 0:
            return

        row_h  = 20                      # matches stylesheet item padding + font
        list_h = min(count * row_h + 2, 5 * row_h + 2)
        list_w = min(200, cmd_w)         # compact — never wider than the input

        # Top-left of command input in container coordinates
        cmd_pos = self.command.mapTo(self._container, QPoint(0, 0))
        x = cmd_pos.x()
        y = cmd_pos.y() - list_h        # float just above the input

        self.suggestion_list.setGeometry(x, y, list_w, list_h)
        self.suggestion_list.raise_()
        self.suggestion_list.show()

    def _complete_with_suggestion(self):
        """Fill the command input with the highlighted (or first) suggestion."""
        row = self.suggestion_list.currentRow()
        item = (
            self.suggestion_list.item(row)
            if row >= 0
            else self.suggestion_list.item(0)
        )
        if item:
            self.suggestion_list.hide()
            self.command.setText(item.text())
            self.command.setFocus()

    def _accept_suggestion(self, item=None):
        """Fill command input with the suggestion and submit it immediately."""
        if item is None:
            row = self.suggestion_list.currentRow()
            item = self.suggestion_list.item(row) if row >= 0 else None
        if item:
            self.suggestion_list.hide()
            self.command.setText(item.text())
            self.command.setFocus()
            self.command.returnPressed.emit()

    # ────────────────────────────────────────────────────────────────────
    # Event filter — suggestion navigation from the command input
    # ────────────────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        # Any click inside the terminal restores the plugin map tool so that
        # canvas interaction after typing a command behaves correctly.
        if event.type() == QEvent.MouseButtonPress and self.on_activate_maptool:
            self.on_activate_maptool()

        # Delete / Backspace on an empty input → forward to the canvas so the
        # vertex selector can delete the selected feature or gripped vertex.
        if (event.type() == QEvent.KeyPress and obj is self.command
                and event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                and not self.command.text()
                and self.on_canvas_key):
            self.on_canvas_key(event)
            return True

        if event.type() != QEvent.KeyPress or obj is not self.command:
            return super().eventFilter(obj, event)

        if not self.suggestion_list.isVisible():
            return super().eventFilter(obj, event)

        key   = event.key()
        count = self.suggestion_list.count()
        row   = self.suggestion_list.currentRow()

        if key == Qt.Key_Down:
            # Move selection down (wraps to first item)
            self.suggestion_list.setCurrentRow((row + 1) % count)
            return True   # consume — prevents history navigation

        if key == Qt.Key_Up:
            # Move selection up; at the top, clear selection entirely
            if row <= 0:
                self.suggestion_list.setCurrentRow(-1)
            else:
                self.suggestion_list.setCurrentRow(row - 1)
            return True   # consume

        if key == Qt.Key_Tab:
            self._complete_with_suggestion()
            return True

        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            # Always accept a suggestion when the list is visible —
            # fall back to the first item if nothing is highlighted yet.
            if count > 0:
                if row < 0:
                    self.suggestion_list.setCurrentRow(0)
                self._accept_suggestion()
                return True
            return False

        if key == Qt.Key_Escape:
            self.suggestion_list.hide()
            return False   # let ESC bubble up so active tools still cancel

        return super().eventFilter(obj, event)

    # ────────────────────────────────────────────────────────────────────
    # Dock resize — keep the overlay aligned
    # ────────────────────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.suggestion_list.isVisible():
            self._reposition_list()

    # ────────────────────────────────────────────────────────────────────
    # History helpers
    # ────────────────────────────────────────────────────────────────────

    def previousCommand(self):
        if self.commandHistory:
            return self.commandHistory[self.historyIndex]
        return ''

    def scrollUpcommandDisplay(self):
        self.commandDisplay.moveCursor(QTextCursor.End)
        self.commandDisplay.ensureCursorVisible()

    def commandRepeatPrevCommand(self):
        if self.command.text() == '':
            self.historyIndex = len(self.commandHistory) - 1
            if self.historyIndex > 0:
                self.historyIndex -= 1
                self.command.setText(self.previousCommand())
                self.command.returnPressed.emit()
        else:
            self.command.returnPressed.emit()

    def commandRepeatUp(self):
        if self.historyIndex > 0:
            self.historyIndex -= 1
            self.command.setText(self.previousCommand())

    def commandRepeatDown(self):
        if self.historyIndex < len(self.commandHistory) - 1:
            self.historyIndex += 1
            self.command.setText(self.previousCommand())
