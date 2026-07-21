from qgis.PyQt.QtCore import Qt, QObject, QEvent

class KeyPressFilter(QObject):
    def __init__(self, key_name, callback):
        """
        key_name: string like 'up', 'down', 'left', 'right'
        callback: function to call when that key is pressed
        """
        super().__init__()
        self.key_name = key_name.lower()
        self.callback = callback

        # map string names to Qt keys
        self.key_map = {
            'up': Qt.Key.Key_Up,
            'down': Qt.Key.Key_Down,
            'left': Qt.Key.Key_Left,
            'right': Qt.Key.Key_Right,
            'enter': Qt.Key.Key_Return,
            'esc': Qt.Key.Key_Escape,
            'space': Qt.Key.Key_Space,
        }

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            qt_key = self.key_map.get(self.key_name)
            if qt_key and event.key() == qt_key:
                self.callback()
                return True  # stop event propagation
        return False  # let other events pass through