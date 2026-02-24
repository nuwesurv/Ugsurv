from qgis.PyQt.QtCore import QObject, Qt
from qgis.PyQt.QtGui import QKeyEvent

class GlobalKeyPressFilter(QObject):
    def __init__(self, terminal_dock):
        super().__init__()
        self.terminal_dock = terminal_dock

    def eventFilter(self, obj, event):
        if event.type() == QKeyEvent.KeyPress:
            key_event = event

            # Convert to string for terminal input
            key_text = key_event.text()
            if key_text:  # Only process printable characters
                # Append to terminal input buffer
                current = self.terminal_dock.command.text()
                self.terminal_dock.command.setText('HEy we reached here')
                # self.terminal_dock.command.setText(current + key_text)
            
            # Handle Enter separately
            if key_event.key() == Qt.Key_Return or key_event.key() == Qt.Key_Enter:
                self.terminal_dock.command.returnPressed.emit()

            # Block this key from going to QGIS
            return True

        return False