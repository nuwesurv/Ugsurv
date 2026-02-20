from PyQt5.QtCore import Qt, QObject, QEvent

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
            'up': Qt.Key_Up,
            'down': Qt.Key_Down,
            'left': Qt.Key_Left,
            'right': Qt.Key_Right,
            'enter': Qt.Key_Return,
            'esc': Qt.Key_Escape,
            'space': Qt.Key_Space,
        }

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            qt_key = self.key_map.get(self.key_name)
            if qt_key and event.key() == qt_key:
                self.callback()
                return True  # stop event propagation
        return False  # let other events pass through