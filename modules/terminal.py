from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLabel, QLineEdit, QSpacerItem, QSizePolicy
)
from PyQt5.QtGui import (QIcon, QTextCursor)
from PyQt5.QtCore import Qt
from qgis.core import QgsPointXY
import os
from .keymap import KeyPressFilter

class TerminalDialog(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Terminal", parent)

        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        self.setWindowIcon(QIcon(icon_path))

        # Allow docking areas
        self.setAllowedAreas(
            Qt.LeftDockWidgetArea |
            Qt.RightDockWidgetArea |
            Qt.BottomDockWidgetArea
        )

        # Create a container QWidget for the dock
        container = QWidget()
        self.setWidget(container)
        self.setMinimumHeight(80)

        # Main layout
        main_layout = QVBoxLayout(container)

        # --- Terminal output ---
        self.commandDisplay = QTextEdit()
        self.commandDisplay.setReadOnly(True)
        self.commandDisplay.setText("Loading plugin 0.01ms...\nPlugin has been loaded 🧪 0.01ms...")
        self.commandDisplay.setStyleSheet("color: #5f5f5f;")
        self.commandDisplay.setMinimumHeight(30)
        self.commandDisplay.textChanged.connect(self.scrollUpcommandDisplay)
        main_layout.addWidget(self.commandDisplay)
        


        # Spacer
        main_layout.addItem(QSpacerItem(0, 10, QSizePolicy.Minimum, QSizePolicy.Expanding))

        # --- Command entry ---
        self.commandOutputText = "Loading plugin 0.01ms...\nPlugin has been loaded 🧪 0.01ms..."
        self.commandHistory = ['']
        self.historyIndex = len(self.commandHistory)-1

        
        self.active_input_handler = None  # set by tools that need typed input

        self.command = QLineEdit()
        self.command.setMinimumWidth(200)
        self.command.setPlaceholderText('command here...')
        # self.command.textChanged.connect(self.commandTyping)
        
        self.key_filter1 = KeyPressFilter( 'up',self.commandRepeatUp)
        self.key_filter2 = KeyPressFilter( 'down',self.commandRepeatDown)
        self.key_filter3 = KeyPressFilter( 'space',self.commandRepeatPrevCommand)
        self.command.installEventFilter(self.key_filter1)
        self.command.installEventFilter(self.key_filter2)
        self.command.installEventFilter(self.key_filter3)

        main_layout.addWidget(self.command)
        
        
    def request_input(self, prompt: str, callback):
        """Ask the user to type something. callback(text) fires on next Enter."""
        self.active_input_handler = callback
        self.command.setPlaceholderText(prompt)
        self.command.setFocus()

    def clear_input_handler(self):
        self.active_input_handler = None
        self.command.setPlaceholderText('command here...')

    def previousCommand(self):
        if len(self.commandHistory) > 0:
            return self.commandHistory[self.historyIndex]
        return ''
    
    # def commandTyping(self):
    #     self.commandDisplay.setText(self.commandOutputText + '\n' + self.command.text())
    #     ...
    def scrollUpcommandDisplay(self):
        # move cursor to the end
        self.commandDisplay.moveCursor(QTextCursor.End)
        self.commandDisplay.ensureCursorVisible()
        
        
    def commandRepeatPrevCommand(self):
        if self.command.text() == '':
            # Always start from the most recent entry so UP-arrow browsing
            # doesn't cause space to jump further back than the last command.
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
        if self.historyIndex < len(self.commandHistory)-1:
            self.historyIndex += 1
            self.command.setText(self.previousCommand())
        ...

