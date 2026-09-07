# -*- coding: utf-8 -*-
"""
TextTool — draws a text box and places a label inside it.

Flow:
  Create:  click first corner → rubber-band preview → click second corner → text dialog.
  Edit:    click inside existing text box (not near a vertex) → properties dialog.
  Resize:  click a corner vertex → rubber-band preview → click new corner position.

Resize algorithm:
  1st click: _find_vertex_near detects grabbed corner + diagonally opposite corner (fixed).
  Hover    : rectangle = QgsRectangle(cursor, fixed_opposite). Rubber band updates live.
  2nd click: commit QgsGeometry.fromRect(QgsRectangle(cursor, fixed_opposite)).
  Axis-aligned rectangle guaranteed by construction — 90° always preserved.
  Enter/Esc cancels and returns to idle.
"""

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QLabel,
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

_MSG_IDLE      = "TEXT — click first corner of text box, or click existing text to edit"
_VERTEX_TOL_PX = 14


# ── Properties dialog ─────────────────────────────────────────────────────────

class _TextBoxDialog(QDialog):
    def __init__(self, text="", cx=0.0, cy=0.0, width=100.0, height=30.0,
                 edit_mode=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Text Box Properties")
        self.setMinimumWidth(340)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Label text  (Enter = new line,  Ctrl+Enter = confirm):"))
        self._te = QTextEdit()
        self._te.setPlainText(text)
        self._te.selectAll()
        self._te.setMinimumHeight(70)
        layout.addWidget(self._te)

        if edit_mode:
            grp  = QGroupBox("Position && Size  (layer CRS — metres)")
            form = QFormLayout(grp)

            def _spin(v, lo=-1e9, hi=1e9, dec=3):
                sb = QDoubleSpinBox()
                sb.setRange(lo, hi)
                sb.setDecimals(dec)
                sb.setValue(v)
                sb.setSingleStep(1.0)
                sb.setMinimumWidth(130)
                return sb

            self._cx = _spin(cx)
            self._cy = _spin(cy)
            self._w  = _spin(width,  lo=0.01)
            self._h  = _spin(height, lo=0.01)
            form.addRow("Centre X:", self._cx)
            form.addRow("Centre Y:", self._cy)
            form.addRow("Width:",    self._w)
            form.addRow("Height:",   self._h)
            layout.addWidget(grp)
        else:
            self._cx = self._cy = self._w = self._h = None
            self._init = (cx, cy, width, height)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.accept)

    def values(self):
        t = self._te.toPlainText()
        if self._cx is not None:
            return t, self._cx.value(), self._cy.value(), self._w.value(), self._h.value()
        return (t,) + self._init


# ── Tool ──────────────────────────────────────────────────────────────────────

class TextTool(BaseTool):
    """TEXT / T / MTEXT — draw a text box and place a label inside it."""

    CURSOR = Qt.CursorShape.CrossCursor

    def __init__(self, canvas, tool_context, input_translator):
        super().__init__(canvas, tool_context, input_translator)
        self._text_layer    = None
        self._first_corner  = None      # QgsPointXY during create
        self._preview_rb    = None      # rubber band for create preview
        self._drag_fid      = None      # fid being vertex-dragged
        self._drag_opposite = None      # fixed corner (project CRS)
        self._drag_rb       = None      # rubber band for resize preview

    # ── lifecycle ─────────────────────────────────────────────────────────

    def activate(self):
        super().activate()
        from .area_label import _get_or_create_text_layer
        self._text_layer  = _get_or_create_text_layer(self._ctx)
        self._first_corner = None
        self._transition(ToolState.ACTING)
        self._log(_MSG_IDLE)
        self._request_input("xy", "First corner:")

    def deactivate(self):
        self._clear_preview_rb()
        self._clear_drag_rb()
        super().deactivate()

    # ── semantic events ───────────────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.CONFIRM:
            if self._drag_fid is not None or self._first_corner is not None:
                self._drag_fid      = None
                self._drag_opposite = None
                self._clear_drag_rb()
                self._first_corner  = None
                self._clear_preview_rb()
                self._log(_MSG_IDLE)
                self._request_input("xy", "First corner:")
            else:
                self._go_home()
            return

        if sem.type not in (EventType.POINT_PICKED, EventType.COORDINATE_ENTERED):
            return
        if sem.point is None:
            return

        # ── resize second click: commit ───────────────────────────────────
        if self._drag_fid is not None:
            fid, opp = self._drag_fid, self._drag_opposite
            self._drag_fid      = None
            self._drag_opposite = None
            self._clear_drag_rb()
            self._finish_drag(fid, sem.point, opp)
            self._log(_MSG_IDLE)
            self._request_input("xy", "First corner:")
            return

        # ── create second click ───────────────────────────────────────────
        if self._first_corner is not None:
            c1, c2 = self._first_corner, sem.point
            self._first_corner = None
            self._clear_preview_rb()
            QTimer.singleShot(0, lambda: self._handle_create(c1, c2))
            return

        # ── idle: vertex grab → resize, inside box → edit, else → create ─
        hit = self._find_vertex_near(sem.point)
        if hit:
            fid, grabbed, opposite = hit
            self._drag_fid      = fid
            self._drag_opposite = opposite
            self._start_drag_rb(grabbed, opposite)
            self._log("TEXT — click new corner position  (Enter to cancel)")
            self._request_input("xy", "New corner:")
            return

        fid = self._find_text_fid_at(sem.point)
        if fid is not None:
            QTimer.singleShot(0, lambda: self._handle_edit(fid))
            return

        self._first_corner = sem.point
        self._log("TEXT — click second corner")
        self._request_input("xy", "Second corner:")

    def _on_hover(self, sem: SemanticEvent):
        pt = sem.point
        if pt is None:
            return

        # Vertex drag in progress — update resize rubber band
        if self._drag_fid is not None and self._drag_opposite is not None:
            self._update_drag_rb(pt)
            return

        # Create mode — update preview rubber band
        if self._first_corner is not None:
            self._update_preview_rb(self._first_corner, pt)
            return

        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn is not None:
            dyn.set_live_pair(pt.x(), pt.y())

    # ── vertex detection ──────────────────────────────────────────────────

    def _find_vertex_near(self, map_pt: QgsPointXY):
        """Return (fid, grabbed_proj, opposite_proj) or None."""
        if not self._text_layer or not self._text_layer.isValid():
            return None

        layer_crs  = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        proj_crs   = QgsProject.instance().crs()
        need_xform = proj_crs != layer_crs
        to_layer   = (QgsCoordinateTransform(proj_crs, layer_crs, QgsProject.instance())
                      if need_xform else None)
        to_proj    = (QgsCoordinateTransform(layer_crs, proj_crs, QgsProject.instance())
                      if need_xform else None)

        pt_lyr = to_layer.transform(QgsPointXY(map_pt)) if to_layer else QgsPointXY(map_pt)
        tol    = _VERTEX_TOL_PX * self.canvas().mapUnitsPerPixel()
        rect   = QgsRectangle(pt_lyr.x() - tol, pt_lyr.y() - tol,
                              pt_lyr.x() + tol, pt_lyr.y() + tol)

        best_fid  = None
        best_d2   = tol * tol
        best_grab = None
        best_opp  = None

        for feat in self._text_layer.getFeatures(rect):
            ring = feat.geometry().asPolygon()
            if not ring or len(ring[0]) < 5:
                continue
            corners = ring[0][:4]
            for i, vtx in enumerate(corners):
                dx = vtx.x() - pt_lyr.x()
                dy = vtx.y() - pt_lyr.y()
                d2 = dx * dx + dy * dy
                if d2 < best_d2:
                    opp      = corners[(i + 2) % 4]
                    best_d2  = d2
                    best_fid = feat.id()
                    best_grab = (to_proj.transform(QgsPointXY(vtx.x(), vtx.y()))
                                 if to_proj else QgsPointXY(vtx.x(), vtx.y()))
                    best_opp  = (to_proj.transform(QgsPointXY(opp.x(), opp.y()))
                                 if to_proj else QgsPointXY(opp.x(), opp.y()))

        return (best_fid, best_grab, best_opp) if best_fid is not None else None

    # ── resize rubber band ────────────────────────────────────────────────

    def _start_drag_rb(self, grabbed: QgsPointXY, opposite: QgsPointXY):
        self._clear_drag_rb()
        rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
        rb.setColor(_style.RB_DRAW)
        rb.setFillColor(_style.RB_DRAW_FILL)
        rb.setWidth(_style.RB_WIDTH)
        rb.setToGeometry(QgsGeometry.fromRect(QgsRectangle(grabbed, opposite)))
        self._drag_rb = rb

    def _update_drag_rb(self, cursor_proj: QgsPointXY):
        if self._drag_rb is not None:
            self._drag_rb.setToGeometry(
                QgsGeometry.fromRect(QgsRectangle(cursor_proj, self._drag_opposite))
            )

    def _clear_drag_rb(self):
        if self._drag_rb is not None:
            try:
                self.canvas().scene().removeItem(self._drag_rb)
            except Exception:   # nosec B110
                pass
            self._drag_rb = None

    def _finish_drag(self, fid: int,
                     cursor_proj: QgsPointXY, opposite_proj: QgsPointXY):
        layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        proj_crs  = QgsProject.instance().crs()
        if proj_crs != layer_crs:
            xf = QgsCoordinateTransform(proj_crs, layer_crs, QgsProject.instance())
            c1 = xf.transform(QgsPointXY(cursor_proj))
            c2 = xf.transform(QgsPointXY(opposite_proj))
        else:
            c1 = QgsPointXY(cursor_proj)
            c2 = QgsPointXY(opposite_proj)

        rect = QgsRectangle(c1, c2)
        if rect.isEmpty():
            return
        self._text_layer.startEditing()
        self._text_layer.changeGeometry(fid, QgsGeometry.fromRect(rect))
        self._text_layer.commitChanges()
        self._text_layer.triggerRepaint()

    # ── create / edit dialogs ─────────────────────────────────────────────

    def _handle_create(self, c1: QgsPointXY, c2: QgsPointXY):
        layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        proj_crs  = QgsProject.instance().crs()
        geom = QgsGeometry.fromRect(QgsRectangle(c1, c2))
        if proj_crs != layer_crs:
            geom.transform(QgsCoordinateTransform(proj_crs, layer_crs, QgsProject.instance()))
        bb = geom.boundingBox()

        dlg = _TextBoxDialog(cx=bb.center().x(), cy=bb.center().y(),
                             width=bb.width(), height=bb.height(),
                             edit_mode=False, parent=self._parent_win())
        accepted = dlg.exec_() == QDialog.Accepted
        self._restore_canvas()
        if not accepted:
            self._log(_MSG_IDLE)
            self._request_input("xy", "First corner:")
            return
        text, *_ = dlg.values()
        if not text.strip():
            self._log(_MSG_IDLE)
            self._request_input("xy", "First corner:")
            return
        self._commit_create(geom, text)
        self._log(_MSG_IDLE)
        self._request_input("xy", "First corner:")

    def _handle_edit(self, fid: int):
        feat = self._text_layer.getFeature(fid)
        bb   = feat.geometry().boundingBox()
        dlg  = _TextBoxDialog(
            text=feat["label_text"] or "",
            cx=bb.center().x(), cy=bb.center().y(),
            width=bb.width(), height=bb.height(),
            edit_mode=True, parent=self._parent_win(),
        )
        accepted = dlg.exec_() == QDialog.Accepted
        self._restore_canvas()
        if not accepted:
            self._log(_MSG_IDLE)
            self._request_input("xy", "First corner:")
            return
        text, new_cx, new_cy, new_w, new_h = dlg.values()
        self._commit_edit(fid, text, new_cx, new_cy, new_w, new_h)
        self._log(_MSG_IDLE)
        self._request_input("xy", "First corner:")

    # ── write ─────────────────────────────────────────────────────────────

    def _commit_create(self, geom: QgsGeometry, text: str):
        if not self._text_layer or not self._text_layer.isValid():
            self._log("Text layer unavailable.", "#ff6666")
            return
        self._text_layer.startEditing()
        f = QgsFeature(self._text_layer.fields())
        f.setGeometry(geom)
        f["label_text"] = text
        f["cad_layer"]  = getattr(self._ctx, "active_cad_layer", "0")
        self._text_layer.addFeature(f)
        self._text_layer.commitChanges()
        self._text_layer.triggerRepaint()
        self._log(f'Placed: "{text.replace(chr(10), " / ")}"', "#aaffaa")

    def _commit_edit(self, fid: int, text: str,
                     cx: float, cy: float, w: float, h: float):
        hw, hh   = w / 2.0, h / 2.0
        new_geom = QgsGeometry.fromRect(
            QgsRectangle(cx - hw, cy - hh, cx + hw, cy + hh)
        )
        self._text_layer.startEditing()
        self._text_layer.changeGeometry(fid, new_geom)
        idx = self._text_layer.fields().indexOf("label_text")
        self._text_layer.changeAttributeValue(fid, idx, text)
        self._text_layer.commitChanges()
        self._text_layer.triggerRepaint()
        self._log(f'Updated: "{text.replace(chr(10), " / ")}"', "#aaffaa")

    # ── hit-test (click inside → edit dialog) ────────────────────────────

    def _find_text_fid_at(self, map_pt: QgsPointXY):
        if not self._text_layer or not self._text_layer.isValid():
            return None
        layer_crs = QgsCoordinateReferenceSystem(_LAYER_CRS_AUTH)
        proj_crs  = QgsProject.instance().crs()
        pt = (QgsCoordinateTransform(proj_crs, layer_crs, QgsProject.instance())
              .transform(QgsPointXY(map_pt)) if proj_crs != layer_crs else QgsPointXY(map_pt))

        tol  = _VERTEX_TOL_PX * self.canvas().mapUnitsPerPixel()
        rect = QgsRectangle(pt.x() - tol, pt.y() - tol,
                            pt.x() + tol, pt.y() + tol)
        pt_geom  = QgsGeometry.fromPointXY(pt)
        best_fid  = None
        best_area = float('inf')
        for feat in self._text_layer.getFeatures(rect):
            g = feat.geometry()
            if not g or g.isNull():
                continue
            if g.distance(pt_geom) < 1e-6:
                a = g.area()
                if a < best_area:
                    best_area = a
                    best_fid  = feat.id()
        return best_fid

    # ── create rubber band ────────────────────────────────────────────────

    def _update_preview_rb(self, c1: QgsPointXY, c2: QgsPointXY):
        if self._preview_rb is None:
            self._preview_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            self._preview_rb.setColor(_style.RB_DRAW)
            self._preview_rb.setFillColor(_style.RB_DRAW_FILL)
            self._preview_rb.setWidth(_style.RB_WIDTH)
        self._preview_rb.setToGeometry(QgsGeometry.fromRect(QgsRectangle(c1, c2)))

    def _clear_preview_rb(self):
        if self._preview_rb is not None:
            try:
                self.canvas().scene().removeItem(self._preview_rb)
            except Exception:   # nosec B110
                pass
            self._preview_rb = None

    # ── helpers ───────────────────────────────────────────────────────────

    def _parent_win(self):
        iface = getattr(self._ctx, 'iface', None)
        return iface.mainWindow() if iface else None

    def _restore_canvas(self):
        canvas = getattr(self._ctx, 'canvas', None)
        if canvas:
            canvas.window().activateWindow()
            canvas.setFocus()

    def _on_cancel_hook(self):
        self._first_corner = None
        self._clear_preview_rb()
        self._drag_fid      = None
        self._drag_opposite = None
        self._clear_drag_rb()

    def _log(self, msg: str, color: str = "#cccccc"):
        dock = getattr(self._ctx, "cmd_dock", None)
        if dock:
            dock.log(msg, color)
