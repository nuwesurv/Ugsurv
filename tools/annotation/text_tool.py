# -*- coding: utf-8 -*-
"""
TextTool — places or edits a text label on the text layer.

Flow:
  1. Click empty space      → multi-line text dialog opens; OK places a new label.
  2. Click existing label   → same dialog, pre-filled; OK updates the label in place.
  3. Right-click or Esc     → exit.

Enter adds a new line inside the dialog.  Ctrl+Enter or the OK button confirms.
"""

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel,
    QShortcut, QTextEdit, QVBoxLayout,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType

_LAYER_EPSG     = 32636
_LAYER_CRS_AUTH = f"EPSG:{_LAYER_EPSG}"


class TextTool(BaseTool):
    """TEXT / T / MTEXT — click to place or edit a text label."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._text_layer = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def activate(self):
        super().activate()
        from .area_label import _get_or_create_text_layer
        self._text_layer = _get_or_create_text_layer(self._ctx)
        self._transition(ToolState.ACTING)
        self._log("TEXT — click to place text, or click existing text to edit")
        self._request_input("xy", "Insertion point:")

    # ── events ────────────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            self._go_home()
            return
        if sem.type not in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            return
        if sem.point is None:
            return

        fid = self._find_text_fid_at(sem.point)
        if fid is not None:
            QTimer.singleShot(0, lambda: self._handle_edit(fid))
        else:
            pt = sem.point
            QTimer.singleShot(0, lambda: self._handle_create(pt))

    def _on_hover(self, sem: SemanticEvent):
        pt = sem.point
        if pt is None:
            return
        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn is not None:
            dyn.set_live_pair(pt.x(), pt.y())

    # ── create / edit ─────────────────────────────────────────────────────

    def _handle_create(self, map_pt: QgsPointXY):
        text = self._ask_text()
        if text is None or not text.strip():
            return
        self._commit_create(map_pt, text)

    def _handle_edit(self, fid: int):
        feat = self._text_layer.getFeature(fid)
        current = feat["label_text"] or ""
        text = self._ask_text(initial=current)
        if text is None:
            return
        self._commit_edit(fid, text)

    # ── dialog ────────────────────────────────────────────────────────────

    def _ask_text(self, initial: str = "") -> "str | None":
        """Blocking multi-line text dialog. Returns text on OK, None on cancel."""
        iface = getattr(self._ctx, 'iface', None)
        parent = iface.mainWindow() if iface else None

        dlg = QDialog(parent)
        dlg.setWindowTitle("Text Label")
        dlg.setMinimumWidth(320)
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Enter text  (Enter = new line,  Ctrl+Enter = confirm):"))

        te = QTextEdit()
        te.setPlainText(initial)
        te.selectAll()
        te.setMinimumHeight(80)
        layout.addWidget(te)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        QShortcut(QKeySequence("Ctrl+Return"), dlg).activated.connect(dlg.accept)

        accepted = dlg.exec_() == QDialog.Accepted
        # Restore focus to the canvas so Esc and key shortcuts work again.
        # activateWindow() is required on Windows before setFocus() takes effect.
        canvas = getattr(self._ctx, 'canvas', None)
        if canvas:
            canvas.window().activateWindow()
            canvas.setFocus()
        if not accepted:
            return None
        return te.toPlainText()

    # ── write helpers ─────────────────────────────────────────────────────

    def _commit_create(self, map_pt: QgsPointXY, text: str):
        if not self._text_layer or not self._text_layer.isValid():
            self._log("Text layer unavailable.", "#ff6666")
            return
        if not self._text_layer.isEditable():
            self._text_layer.startEditing()

        layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        proj_crs  = QgsProject.instance().crs()
        geom = QgsGeometry.fromPointXY(map_pt)
        if proj_crs != layer_crs:
            geom.transform(QgsCoordinateTransform(proj_crs, layer_crs, QgsProject.instance()))

        f = QgsFeature(self._text_layer.fields())
        f.setGeometry(geom)
        f["label_text"] = text
        f["cad_layer"]  = getattr(self._ctx, "active_cad_layer", "0")
        self._text_layer.addFeature(f)
        self._text_layer.triggerRepaint()
        preview = text.replace('\n', ' / ')
        self._log(f'Placed: "{preview}"', "#aaffaa")

    def _commit_edit(self, fid: int, new_text: str):
        if not self._text_layer.isEditable():
            self._text_layer.startEditing()
        idx = self._text_layer.fields().indexOf("label_text")
        self._text_layer.changeAttributeValue(fid, idx, new_text)
        self._text_layer.triggerRepaint()
        preview = new_text.replace('\n', ' / ')
        self._log(f'Updated: "{preview}"', "#aaffaa")

    # ── hit-test ──────────────────────────────────────────────────────────

    def _find_text_fid_at(self, map_pt: QgsPointXY):
        if not self._text_layer or not self._text_layer.isValid():
            return None
        tol  = 12 * self.canvas().mapUnitsPerPixel()
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        pt_geom   = QgsGeometry.fromPointXY(map_pt)
        best_fid  = None
        best_dist = tol
        for feat in self._text_layer.getFeatures(rect):
            g = feat.geometry()
            if g and not g.isNull():
                d = g.distance(pt_geom)
                if d < best_dist:
                    best_dist = d
                    best_fid  = feat.id()
        return best_fid

    # ── cancel ────────────────────────────────────────────────────────────

    def _on_cancel_hook(self):
        pass

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, "cmd_dock", None)
        if dock:
            dock.log(msg, color)
