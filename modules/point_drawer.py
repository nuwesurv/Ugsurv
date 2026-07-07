"""
AutoCAD-style POINT tool.

Workflow
────────
1. Move cursor   → live X, Y shown in DynamicInput near cursor
2. Click         → place a point at the snapped location, tool stays active
   Type X,Y + Enter → place a point at precise coordinates
   Empty Enter / Esc / RMB → finish

Points are added to a '_points' memory layer (created if absent).
"""

from qgis.gui import QgsMapTool, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsMarkerSymbol,
    QgsSingleSymbolRenderer,
)
from PyQt5.QtCore import QVariant
from .dynamic_input import DynamicInput
from . import snap_utils
from .layer_utils import add_to_plugin_group, open_layer_from_gpkg, create_layer_in_gpkg


_LAYER_NAME = "_points"

_HINT_STYLE = (
    "QLabel {"
    "  background-color: rgba(20, 20, 20, 210);"
    "  color: #f0f0f0;"
    "  border: 1px solid rgba(255, 255, 255, 80);"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "  font-size: 9pt;"
    "}"
)


class PointDrawer(QgsMapTool):
    """Place point features — click or type X,Y coordinates. Stays active until Esc/RMB."""

    def __init__(self, canvas, terminal_dock):
        super().__init__(canvas)
        self.canvas        = canvas
        self.terminal_dock = terminal_dock
        self._maptool      = None
        self._snap_marker  = None

        self._layer = self._get_or_create_layer()
        snap_utils.init_snap()

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        self._dinput = DynamicInput(canvas, terminal_dock, [
            {"key": "x", "label": "X"},
            {"key": "y", "label": "Y"},
        ])
        self._dinput.on_cancel = self._on_cancel

    # ------------------------------------------------------------------
    # Layer helpers
    # ------------------------------------------------------------------

    def _ensure_fields(self, lyr):
        existing = {f.name() for f in lyr.fields()}
        to_add = []
        if "x" not in existing: to_add.append(QgsField("x", QVariant.Double))
        if "y" not in existing: to_add.append(QgsField("y", QVariant.Double))
        if to_add:
            lyr.dataProvider().addAttributes(to_add)
            lyr.updateFields()

    def _get_or_create_layer(self):
        existing = QgsProject.instance().mapLayersByName(_LAYER_NAME)
        if existing:
            lyr = existing[0]
            if not lyr.isEditable():
                lyr.startEditing()
            self._ensure_fields(lyr)
            return lyr
        lyr = open_layer_from_gpkg(_LAYER_NAME)
        if lyr:
            self._ensure_fields(lyr)
            self._apply_point_style(lyr)
            add_to_plugin_group(lyr)
            lyr.startEditing()
            return lyr
        return self._create_layer()

    def _apply_point_style(self, lyr):
        symbol = QgsMarkerSymbol.createSimple({
            "color": "0,140,220,255",
            "outline_style": "no",
            "size": "2",
        })
        lyr.setRenderer(QgsSingleSymbolRenderer(symbol))

    def _create_layer(self):
        crs = QgsProject.instance().crs().authid()
        mem = QgsVectorLayer(f"Point?crs={crs}", _LAYER_NAME, "memory")
        mem.dataProvider().addAttributes([
            QgsField("x", QVariant.Double),
            QgsField("y", QVariant.Double),
        ])
        mem.updateFields()
        lyr = create_layer_in_gpkg(mem)
        self._apply_point_style(lyr)
        add_to_plugin_group(lyr)
        lyr.startEditing()
        return lyr

    # ------------------------------------------------------------------
    # Snap helper
    # ------------------------------------------------------------------

    def _snap(self, screen_pos):
        map_pt = self.toMapCoordinates(screen_pos)
        pt, icon = snap_utils.snap_point(self.canvas, map_pt)
        if icon is not None and self._snap_marker:
            self._snap_marker.setCenter(pt)
            self._snap_marker.setIconType(icon)
            self._snap_marker.setVisible(True)
        elif self._snap_marker:
            self._snap_marker.setVisible(False)
        return pt

    # ------------------------------------------------------------------
    # Placing a point
    # ------------------------------------------------------------------

    def _place_point(self, pt: QgsPointXY):
        self._layer = self._get_or_create_layer()
        feat = QgsFeature(self._layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(pt))
        feat.setAttribute("x", round(pt.x(), 4))
        feat.setAttribute("y", round(pt.y(), 4))
        self._layer.addFeature(feat)
        self._layer.triggerRepaint()
        self._log(f"\nPoint: {pt.x():.4f}, {pt.y():.4f}")
        self._request_next()

    def _request_next(self):
        """Re-register terminal handler and keep DynamicInput alive for the next point."""
        center = self.canvas.rect().center()
        self.terminal_dock.request_input("X,Y: ", self._on_terminal_input)
        self._dinput.on_commit = self._on_xy_committed
        self._dinput.show(center.x(), center.y())

    # ------------------------------------------------------------------
    # DynamicInput / terminal callbacks
    # ------------------------------------------------------------------

    def _on_xy_committed(self, values: dict):
        self.terminal_dock.clear_input_handler()
        self._parse_and_place(values.get("x", ""), values.get("y", ""))

    def _on_terminal_input(self, text: str):
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        text = text.strip()
        if not text:
            self._hint.hide()
            self.deactivate()
            return
        parts = text.replace(',', ' ').split()
        if len(parts) >= 2:
            self._parse_and_place(parts[0], parts[1])
        else:
            self._log(f"\nExpected 'X,Y' — got '{text}'")
            self._request_next()

    def _parse_and_place(self, x_text: str, y_text: str):
        try:
            x = float(x_text.strip())
            y = float(y_text.strip())
        except ValueError:
            self._log(f"\nInvalid coordinates — enter X and Y as numbers")
            self._request_next()
            return
        self._place_point(QgsPointXY(x, y))

    def _on_cancel(self):
        self._hint.hide()
        self.deactivate()

    # ------------------------------------------------------------------
    # Terminal helpers
    # ------------------------------------------------------------------

    def _log(self, msg):
        self.terminal_dock.commandOutputText += msg
        self.terminal_dock.commandDisplay.setText(self.terminal_dock.commandOutputText)

    def _show_hint(self, screen_pos, text):
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self.canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self.canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    # ------------------------------------------------------------------
    # QgsMapTool interface
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self.terminal_dock.command.setFocus()
        self._snap_marker = QgsVertexMarker(self.canvas)
        self._snap_marker.setColor(QColor(66, 135, 245))
        self._snap_marker.setIconSize(10)
        self._snap_marker.setPenWidth(2)
        self._snap_marker.setVisible(False)
        self._log(
            "\nPOINT  ──  click to place a point  |  type X,Y + Enter for precision"
            "\n  Empty Enter / Esc / RMB → finish\n"
        )
        self._request_next()

    def deactivate(self):
        self._dinput.destroy()
        self.terminal_dock.clear_input_handler()
        if self._snap_marker:
            self.canvas.scene().removeItem(self._snap_marker)
            self._snap_marker = None
        self._hint.hide()
        if self._maptool:
            self._maptool.clear_tool()
        self._log("\n........\n")
        super().deactivate()

    def canvasMoveEvent(self, event):
        snap_pt = self._snap(event.pos())
        cp = self.canvas.getCoordinateTransform().transform(snap_pt)
        self._dinput.update(cp.x(), cp.y(), {
            "x": f"{snap_pt.x():.4f}",
            "y": f"{snap_pt.y():.4f}",
        })
        self._show_hint(event.pos(), "Click to place point  or  type X,Y")

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._hint.hide()
            self.deactivate()
            return
        if event.button() != Qt.LeftButton:
            return
        snap_pt = self._snap(event.pos())
        self._dinput.hide()
        self.terminal_dock.clear_input_handler()
        self._place_point(snap_pt)
        self.terminal_dock.command.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self._hint.hide()
            self.deactivate()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._hint.hide()
            self.deactivate()

    def mouseDoubleClickEvent(self, event):
        self.terminal_dock.command.setFocus()
