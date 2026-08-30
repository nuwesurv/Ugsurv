# -*- coding: utf-8 -*-
"""
TextTool — click insertion point → type content → Enter commits.

Text is placed as a QGIS text annotation (QgsTextAnnotation) so it is
rendered on the canvas without needing a separate layer.
"""

from qgis.PyQt.QtCore import Qt, QSizeF, QPointF
from qgis.PyQt.QtGui import QFont, QTextDocument
from qgis.core import (
    QgsPointXY, QgsTextAnnotation, QgsProject,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType


class TextTool(BaseTool):
    CURSOR = Qt.CursorShape.IBeamCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._insert_pt: QgsPointXY | None = None

    def activate(self):
        super().activate()
        self._insert_pt = None
        self._transition(ToolState.ACTING)

    def _on_event(self, sem: SemanticEvent):
        if sem.type in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            if sem.point:
                self._insert_pt = sem.point
                # Open an inline input dialog to collect the text
                from qgis.PyQt.QtWidgets import QInputDialog
                text, ok = QInputDialog.getText(
                    self.canvas(), "Text", "Enter text:"
                )
                if ok and text.strip():
                    self._place_text(self._insert_pt, text.strip())
                self._insert_pt = None

        elif sem.type == EventType.CONFIRM:
            self._insert_pt = None

    def _place_text(self, pt: QgsPointXY, text: str):
        ann = QgsTextAnnotation()
        ann.setMapPosition(pt)
        ann.setMapPositionCrs(QgsProject.instance().crs())
        doc = QTextDocument(text)
        ann.setDocument(doc)
        ann.setFrameSizeMm(QSizeF(40, 10))
        ann.setHasFixedMapPosition(True)
        QgsProject.instance().annotationManager().addAnnotation(ann)
