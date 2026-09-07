# -*- coding: utf-8 -*-
"""
TextTool — draws a text box and places a label inside it.

Flow:
  1. Click first corner of the text box.
  2. Click second corner — rectangle rubber-band shows during mouse move.
  3. Multi-line text dialog opens; OK places the label centred in the box.
  4. Click existing text box → same dialog, pre-filled; OK updates text.
  5. Esc while waiting for second corner → reset to first-corner state.
  6. Right-click or Esc at idle → exit.
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
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style

_LAYER_EPSG     = 32636
_LAYER_CRS_AUTH = f"EPSG:{_LAYER_EPSG}"

_MSG_IDLE = "TEXT — click first corner of text box, or click existing text to edit"


class TextTool(BaseTool):
    """TEXT / T / MTEXT — draw a text box and place a label inside it."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._text_layer   = None
        self._first_corner = None   # QgsPointXY — set after first click
        self._preview_rb   = None   # QgsRubberBand — live rectangle preview

    # ── lifecycle ─────────────────────────────────────────────────────────

    def activate(self):
        super().activate()
        from .area_label import _get_or_create_text_layer
        self._text_layer   = _get_or_create_text_layer(self._ctx)
        self._first_corner = None
        self._transition(ToolState.ACTING)
        self._log(_MSG_IDLE)
        self._request_input("xy", "First corner:")

    def deactivate(self):
        self._clear_preview()
        super().deactivate()

    # ── events ────────────────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            if self._first_corner is not None:
                self._first_corner = None
                self._clear_preview()
                self._log(_MSG_IDLE)
                self._request_input("xy", "First corner:")
            else:
                self._go_home()
            return
        if sem.type not in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            return
        if sem.point is None:
            return

        if self._first_corner is None:
            fid = self._find_text_fid_at(sem.point)
            if fid is not None:
                QTimer.singleShot(0, lambda: self._handle_edit(fid))
            else:
                self._first_corner = sem.point
                self._log("TEXT — click second corner")
                self._request_input("xy", "Second corner:")
        else:
            c1, c2 = self._first_corner, sem.point
            self._first_corner = None
            self._clear_preview()
            QTimer.singleShot(0, lambda: self._handle_create(c1, c2))

    def _on_hover(self, sem: SemanticEvent):
        pt = sem.point
        if pt is None:
            return
        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn is not None:
            dyn.set_live_pair(pt.x(), pt.y())
        if self._first_corner is not None:
            self._update_preview(self._first_corner, pt)

    # ── rubber-band preview ────────────────────────────────────────────────

    def _update_preview(self, c1: QgsPointXY, c2: QgsPointXY):
        if self._preview_rb is None:
            self._preview_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            self._preview_rb.setColor(_style.RB_DRAW)
            self._preview_rb.setFillColor(_style.RB_DRAW_FILL)
            self._preview_rb.setWidth(_style.RB_WIDTH)
        self._preview_rb.setToGeometry(QgsGeometry.fromRect(QgsRectangle(c1, c2)))

    def _clear_preview(self):
        if self._preview_rb is not None:
            try:
                self.canvas().scene().removeItem(self._preview_rb)
            except Exception:  # nosec B110
                pass
            self._preview_rb = None

    # ── create / edit ─────────────────────────────────────────────────────

    def _handle_create(self, c1: QgsPointXY, c2: QgsPointXY):
        text = self._ask_text()
        if text is None or not text.strip():
            self._log(_MSG_IDLE)
            self._request_input("xy", "First corner:")
            return
        self._commit_create(c1, c2, text)
        self._log(_MSG_IDLE)
        self._request_input("xy", "First corner:")

    def _handle_edit(self, fid: int):
        feat = self._text_layer.getFeature(fid)
        current = feat["label_text"] or ""
        text = self._ask_text(initial=current)
        if text is None:
            self._log(_MSG_IDLE)
            self._request_input("xy", "First corner:")
            return
        self._commit_edit(fid, text)
        self._log(_MSG_IDLE)
        self._request_input("xy", "First corner:")

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
        canvas = getattr(self._ctx, 'canvas', None)
        if canvas:
            canvas.window().activateWindow()
            canvas.setFocus()
        if not accepted:
            return None
        return te.toPlainText()

    # ── write helpers ─────────────────────────────────────────────────────

    def _commit_create(self, c1: QgsPointXY, c2: QgsPointXY, text: str):
        if not self._text_layer or not self._text_layer.isValid():
            self._log("Text layer unavailable.", "#ff6666")
            return
        if not self._text_layer.isEditable():
            self._text_layer.startEditing()

        layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        proj_crs  = QgsProject.instance().crs()
        geom = QgsGeometry.fromRect(QgsRectangle(c1, c2))
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
        best_dist = float('inf')
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
        self._first_corner = None
        self._clear_preview()

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, "cmd_dock", None)
        if dock:
            dock.log(msg, color)
