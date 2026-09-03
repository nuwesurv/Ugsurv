# -*- coding: utf-8 -*-
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt import sip

from qgis.gui import QgsMapLayerComboBox, QgsMapToolIdentifyFeature, QgsRubberBand
from ..core import style as _style
from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsCoordinateTransform,
    QgsProject,
    QgsSpatialIndex,
    QgsWkbTypes,
)


class AppendGeometryTool(QgsMapToolIdentifyFeature):

    def __init__(self, canvas, iface, dock):
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self._dock = dock
        self.picked_features = []  # list of (QgsFeature, QgsVectorLayer)

        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band.setColor(_style.RB_IDENTIFY_RED)
        self.rubber_band.setWidth(_style.RB_IDENTIFY_WIDTH)
        self.rubber_band.setLineStyle(_style.RB_LINE_STYLE)
        self.rubber_band.setFillColor(_style.RB_IDENTIFY_RED_FILL)

    def _dock_alive(self):
        return not sip.isdeleted(self._dock)

    def activate(self):
        super().activate()
        self.canvas.setFocus()
        if not self._dock_alive():
            return
        n = len(self.picked_features)
        if n:
            self._dock._set_status(f"{n} feature(s) selected.", "#ffaa00")
        self._dock._select_btn.setText("Selecting...")
        self._dock._select_btn.setEnabled(False)

    def deactivate(self):
        if self._dock_alive():
            self._dock._select_btn.setText("Select Features")
            self._dock._select_btn.setEnabled(True)
        super().deactivate()

    def show_appended(self, n_added, skipped, to_name):
        self.picked_features.clear()
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        if self._dock_alive():
            msg = f"Appended {n_added} feature(s) to '{to_name}'."
            if skipped:
                msg += f"  ({skipped} skipped — already exists or type mismatch)"
            self._dock._set_status(msg, "green")

    def clear(self):
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.picked_features.clear()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self._dock_alive():
                self._dock.close()
            else:
                self.canvas.unsetMapTool(self)

    def canvasMoveEvent(self, event):
        pass

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.clear()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            from_layer = self._dock.from_combo.currentLayer()
            if not from_layer:
                self._dock._set_status("Choose a source layer first.", "red")
                return

            results = self.identify(
                event.x(), event.y(),
                [from_layer],
                QgsMapToolIdentifyFeature.IdentifyMode.TopDownAll,
            )
            if len(results) > 1:
                results = [results[0]]

            if results:
                feature = results[0].mFeature
                feat_layer = results[0].mLayer

                already = any(
                    f.id() == feature.id() and lyr.id() == feat_layer.id()
                    for f, lyr in self.picked_features
                )
                if already:
                    return

                geom = QgsGeometry(feature.geometry())

                project_crs = QgsProject.instance().crs()
                feat_crs = feat_layer.crs()
                if feat_crs != project_crs:
                    transform = QgsCoordinateTransform(feat_crs, project_crs, QgsProject.instance())
                    geom.transform(transform)

                self.picked_features.append((feature, feat_layer))
                self.rubber_band.addGeometry(geom, None)
                self.rubber_band.show()

                n = len(self.picked_features)
                self._dock._set_status(
                    f"Feature {n} selected from '{feat_layer.name()}' "
                    f"[fid={feature.id()}]  —  {n} total selected.",
                    "#ffaa00"
                )
            else:
                self._dock._set_status("No feature found at that point.", "#888888")


class GeometryAppenderDock(QDockWidget):
    def __init__(self, parent=None, iface=None, canvas=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = canvas
        self.setWindowTitle('Append Selected Parcels')

        self.tool = AppendGeometryTool(canvas, iface, self)

        inputheight = 25

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # Source layer selector
        from_row = QHBoxLayout()
        from_row.addWidget(QLabel('Copy selected from:'))
        self.from_combo = QgsMapLayerComboBox()
        self.from_combo.setFixedHeight(inputheight)
        from_row.addWidget(self.from_combo)
        layout.addLayout(from_row)

        # Target layer selector
        to_row = QHBoxLayout()
        to_row.addWidget(QLabel('Add selected to:'))
        self.to_combo = QgsMapLayerComboBox()
        self.to_combo.setFixedHeight(inputheight)
        to_row.addWidget(self.to_combo)
        layout.addLayout(to_row)

        # Status label
        self.response = QLabel('')
        self.response.setWordWrap(True)
        layout.addWidget(self.response)

        layout.addStretch()

        # Select Features + Append buttons
        btn_row = QHBoxLayout()
        self._select_btn = QPushButton('Select Features')
        btn_row.addWidget(self._select_btn)
        btn_row.addStretch()
        self.append_btn = QPushButton('Append')
        btn_row.addWidget(self.append_btn)
        layout.addLayout(btn_row)

        self.setWidget(root)

        self._select_btn.clicked.connect(self._activate_tool)
        self.append_btn.clicked.connect(self.append_parcels)
        self.visibilityChanged.connect(self._on_visibility_changed)

    def closeEvent(self, event):
        self.tool.clear()
        if self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
        super().closeEvent(event)

    def _on_visibility_changed(self, visible):
        if visible:
            self._activate_tool()

    def _activate_tool(self):
        self.canvas.setMapTool(self.tool)

    def _set_status(self, message, color="green"):
        self.response.setStyleSheet(f"color: {color};")
        self.response.setText(message)

    def append_parcels(self):
        to_layer = self.to_combo.currentLayer()
        if not to_layer:
            self._set_status("No target layer selected!", "red")
            return

        picked = self.tool.picked_features
        if not picked:
            self._set_status("Nothing selected — click features on the map first.", "red")
            return

        index = QgsSpatialIndex(to_layer.getFeatures())
        existing_geoms = {f.id(): f.geometry() for f in to_layer.getFeatures()}

        try:
            if not to_layer.isEditable():
                to_layer.startEditing()

            to_fields = to_layer.fields()
            new_features = []
            skipped = 0

            for feat, from_layer in picked:
                geom = feat.geometry()

                transform = None
                if from_layer.crs() != to_layer.crs():
                    transform = QgsCoordinateTransform(
                        from_layer.crs(), to_layer.crs(), QgsProject.instance()
                    )
                if transform:
                    geom.transform(transform)

                if not geom.isGeosValid():
                    geom = geom.makeValid()

                dest_base = QgsWkbTypes.geometryType(to_layer.wkbType())
                src_base = QgsWkbTypes.geometryType(geom.wkbType())
                if src_base != dest_base:
                    self._set_status(
                        f"Skipped: cannot convert {QgsWkbTypes.displayString(geom.wkbType())} "
                        f"to {QgsWkbTypes.displayString(to_layer.wkbType())}.", "red"
                    )
                    skipped += 1
                    continue

                dest_is_multi = QgsWkbTypes.isMultiType(to_layer.wkbType())
                if geom.isMultipart() and not dest_is_multi:
                    parts = geom.asGeometryCollection()
                    if len(parts) == 1:
                        geom = parts[0]
                    else:
                        converted = geom.convertToType(dest_base, False)
                        if converted is None or converted.isNull():
                            self._set_status("Skipped: multi-part geometry cannot be reduced to a single part.", "red")
                            skipped += 1
                            continue
                        geom = converted
                elif not geom.isMultipart() and dest_is_multi:
                    geom = geom.convertToType(dest_base, True)

                candidate_ids = index.intersects(geom.boundingBox())
                exists = False
                for cid in candidate_ids:
                    existing_geom = existing_geoms[cid]
                    if geom.equals(existing_geom):
                        exists = True
                        break
                    if geom.intersects(existing_geom):
                        overlap = geom.intersection(existing_geom).area()
                        if overlap / geom.area() > 0.95:
                            exists = True
                            break

                if exists:
                    skipped += 1
                    continue

                new_feat = QgsFeature(to_fields)
                new_feat.setGeometry(geom)

                attrs = []
                for field in to_fields:
                    if field.name().lower() == 'fid':
                        attrs.append(None)
                    elif field.name() in feat.fields().names():
                        attrs.append(feat[field.name()])
                    else:
                        attrs.append(None)
                new_feat.setAttributes(attrs)
                new_features.append(new_feat)

            to_layer.addFeatures(new_features)
            to_layer.triggerRepaint()
            self.tool.show_appended(len(new_features), skipped, to_layer.name())

        except Exception as e:
            to_layer.rollBack()
            self._set_status(f"Error: {e}", "red")
