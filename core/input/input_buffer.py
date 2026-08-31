# -*- coding: utf-8 -*-
"""
InputBuffer — single shared text buffer for all CAD tool typed input.

Both the CommandLineWidget and DynamicInputWidget subscribe to this object.
GlobalKeyFilter writes into it; the widgets display it.
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal


class InputBuffer(QObject):
    textChanged = pyqtSignal(str)   # buffer content changed (empty = cleared)
    submitted   = pyqtSignal(str)   # user committed; buffer already cleared
    cancelled   = pyqtSignal()      # Esc pressed; buffer already cleared

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""

    @property
    def text(self) -> str:
        return self._text

    def append(self, ch: str):
        self._text += ch
        self.textChanged.emit(self._text)

    def backspace(self):
        if self._text:
            self._text = self._text[:-1]
            self.textChanged.emit(self._text)

    def submit(self):
        text = self._text
        if text:
            self._text = ""
            self.textChanged.emit("")
            self.submitted.emit(text)

    def cancel(self):
        if self._text:
            self._text = ""
            self.textChanged.emit("")
        self.cancelled.emit()

    def clear(self):
        if self._text:
            self._text = ""
            self.textChanged.emit("")
