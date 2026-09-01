# -*- coding: utf-8 -*-
import os
import math
import base64
import tempfile
from io import BytesIO

from qgis.PyQt.QtGui import QPixmap, QFont, QIcon, QPainter, QColor
from qgis.PyQt.QtCore import Qt, pyqtSignal, QSize
from qgis.PyQt.QtWidgets import (
    QGroupBox, QDialog, QVBoxLayout, QLabel, QFileDialog, QTableWidget,
    QPushButton, QTableWidgetItem, QHBoxLayout, QSpacerItem, QSizePolicy,
    QComboBox, QGridLayout, QGraphicsView, QGraphicsScene, QTextEdit,
    QLineEdit, QGraphicsProxyWidget, QWidget,
)
from qgis.core import QgsProject, QgsVectorLayer, QgsField, QgsRasterLayer, QgsCoordinateReferenceSystem
from qgis.gui import QgsProjectionSelectionWidget
from osgeo import gdal, osr

try:
    import fitz
except Exception:
    print('Failed to find fitz module')
try:
    from PIL import Image
except Exception:
    print('Failed to find Pillow module')


class ZoomableView(QGraphicsView):
    clicked = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def wheelEvent(self, event):
        zoomFactor = 1.25
        if event.angleDelta().y() > 0:
            self.scale(zoomFactor, zoomFactor)
        else:
            self.scale(1 / zoomFactor, 1 / zoomFactor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.pos())
            item = self.scene().itemAt(scene_pos, self.transform())
            if item is not None and isinstance(item, QGraphicsProxyWidget):
                super().mousePressEvent(event)
                return
            self.clicked.emit(scene_pos.x(), scene_pos.y())
        super().mousePressEvent(event)


class ImportPrintDialog(QDialog):
    def __init__(self, cmd_dock=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Import Print')
        self.setMinimumWidth(1000)
        self.setMinimumHeight(400)
        self._cmd_dock = cmd_dock
        self.align_points = []
        self.image_holder = {}
        self.choosen_pagenumber = 0

        self.templayout = QVBoxLayout()

        self.filepicker = QPushButton('Select print file:')
        self.filepicker.clicked.connect(self.fileselector)
        self.templayout.addWidget(self.filepicker)

        default_crs = QgsCoordinateReferenceSystem("EPSG:32636")
        self.crsWidget = QgsProjectionSelectionWidget()
        self.crsWidget.setCrs(default_crs)
        self.templayout.addWidget(self.crsWidget)

        self.filepath_store = QLabel("")
        self.response = QLabel("No file Path selected currently!")
        self.templayout.addWidget(self.response)

        self.view = ZoomableView()
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.clicked.connect(self.add_align_point)
        self.templayout.addWidget(self.view)

        self.buttongrouper1 = QHBoxLayout()
        self.hspacer1 = QSpacerItem(40, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.hspacer2 = QSpacerItem(40, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.prevbutton = QPushButton('<< Prev')
        self.prevbutton.setFixedWidth(100)
        self.choosen_pagenumber_label = QLabel(f"{self.choosen_pagenumber}")
        self.nextbutton = QPushButton('Next >>')
        self.nextbutton.setFixedWidth(100)
        self.buttongrouper1.addItem(self.hspacer1)
        self.buttongrouper1.addWidget(self.prevbutton)
        self.buttongrouper1.addWidget(self.choosen_pagenumber_label)
        self.buttongrouper1.addWidget(self.nextbutton)
        self.buttongrouper1.addItem(self.hspacer2)
        self.buttongrouper1.setSpacing(10)
        self.templayout.addLayout(self.buttongrouper1)
        self.prevbutton.clicked.connect(self.prevpage)
        self.nextbutton.clicked.connect(self.nextpage)

        self.buttongrouper2 = QHBoxLayout()
        self.hspacer3 = QSpacerItem(40, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.plotbutton = QPushButton('Align print')
        self.plotbutton.setFixedWidth(100)
        self.buttongrouper2.addItem(self.hspacer3)
        self.buttongrouper2.addWidget(self.plotbutton)
        self.buttongrouper2.setSpacing(10)
        self.templayout.addLayout(self.buttongrouper2)

        self.setLayout(self.templayout)
        self.plotbutton.clicked.connect(self.align_raster)

    def nextpage(self):
        if self.choosen_pagenumber < len(self.image_holder) - 1:
            self.choosen_pagenumber += 1
            self.updatePageView()

    def prevpage(self):
        if self.choosen_pagenumber > 0:
            self.choosen_pagenumber -= 1
            self.updatePageView()

    def updatePageView(self):
        self.scene.clear()
        self.align_points.clear()
        self.pixmap = QPixmap(self.image_holder[self.choosen_pagenumber])
        self.pixmap_item = self.scene.addPixmap(self.pixmap)
        self.pixmap_item.setCursor(Qt.CursorShape.CrossCursor)
        total = len(self.image_holder)
        self.choosen_pagenumber_label.setText(f"{self.choosen_pagenumber+1} / {total}")
        self.prevbutton.setEnabled(self.choosen_pagenumber > 0)
        self.nextbutton.setEnabled(self.choosen_pagenumber < total - 1)

    def load_pdf_images(self, pdf_path):
        pdf = fitz.open(pdf_path)
        n_pages = len(pdf)
        self.temp_dir = tempfile.mkdtemp(prefix="Ugsurv_pdf_pages_")
        for page_number in range(n_pages):
            page_obj = pdf[page_number]
            pix = page_obj.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=True)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            image_path = os.path.join(self.temp_dir, f"page_{page_number}.png")
            img.save(image_path, "PNG")
            self.image_holder[page_number] = image_path
        pdf.close()

    def refresh(self):
        self.response.setText('Input been refreshed')
        self.filepath_store.setText('')
        self.scene.clear()
        self.align_points = []

    def fileselector(self):
        self.refresh()
        filepath, _ = QFileDialog.getOpenFileName(self, 'Select files...', '', "Image Files (*.png *.jpg *.jpeg *.pdf)")
        if os.path.exists(filepath):
            self.response.setText(f'File selected: {filepath}')
            self.filepath_store.setText(filepath)
        try:
            if filepath.endswith('.pdf'):
                self.load_pdf_images(filepath)
                self.updatePageView()
            else:
                self.scene.clear()
                self.image_holder = {}
                self.image_holder = {0: filepath}
                self.choosen_pagenumber = 0
                self.updatePageView()
        except Exception as e:
            self.response.setText(f"Error: {str(e)}")

    def align_raster(self):
        filepath = self.filepath_store.text()
        if not os.path.exists(filepath) or filepath == "No file selected":
            self.response.setText("File does not exist")
            return
        if len(self.align_points) != 2:
            self.response.setText(
                f"Caution: Only two points are needed for 2D align but {len(self.align_points)} were selected."
            )
            return
        ds = gdal.Open(self.image_holder[self.choosen_pagenumber])
        if ds is None:
            self.response.setText("Failed to open raster.")
            return
        width = ds.RasterXSize
        height = ds.RasterYSize
        px1, py1 = self.align_points[0]['xy']
        px2, py2 = self.align_points[1]['xy']
        py1 = height - py1
        py2 = height - py2

        def parse_gcp(text):
            text = str(text).strip().replace(" ", "")
            if text.count(",") != 1:
                return None
            e, n = text.split(",")
            return float(e), float(n)

        gcp1 = parse_gcp(self.align_points[0]['text_edit'].text())
        gcp2 = parse_gcp(self.align_points[1]['text_edit'].text())
        if not gcp1 or not gcp2:
            self.response.setText("Incorrect coordinate format. Use 'E,N'")
            return
        x1, y1 = gcp1
        x2, y2 = gcp2
        dx_img = px2 - px1
        dy_img = py2 - py1
        dx_map = x2 - x1
        dy_map = y2 - y1
        img_dist = math.hypot(dx_img, dy_img)
        map_dist = math.hypot(dx_map, dy_map)
        if img_dist == 0:
            self.response.setText("Image points are identical.")
            return
        scale = map_dist / img_dist
        rotation = math.atan2(dy_map, dx_map) - math.atan2(dy_img, dx_img)
        cos_r = math.cos(rotation)
        sin_r = math.sin(rotation)
        a = scale * cos_r
        b = -scale * sin_r
        c = scale * sin_r
        d = -scale * cos_r
        x1_trans = scale * px1
        y1_trans = scale * (height - py1)
        Tx = x1 - x1_trans
        Ty = y1 + y1_trans
        geotransform = [Tx, a, b, Ty, c, d]
        output_raster = os.path.join(
            os.path.dirname(filepath),
            f"{filepath.split('/')[-1].split('.')[0]}.tif"
        )
        ds_out = gdal.Translate(output_raster, ds, format="GTiff")
        ds_out.SetGeoTransform(geotransform)
        crs = self.crsWidget.crs().postgisSrid()
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(crs)
        ds_out.SetProjection(srs.ExportToWkt())
        ds_out = None
        ds = None
        raster_layer = QgsRasterLayer(output_raster, "Aligned Raster (2pt)")
        if not raster_layer.isValid():
            self.response.setText("Failed to create raster layer.")
            return
        QgsProject.instance().addMapLayer(raster_layer)
        self.response.setText("Raster aligned successfully (2-point conformal).")

    def add_align_point(self, x, y):
        if self._cmd_dock:
            self._cmd_dock.log(f'Align point: {[round(x, 2), round(y, 2)]}', '#aaddff')
        self.add_textedit_at(x, y)

    def add_textedit_at(self, x, y):
        container = QWidget()
        container.setFixedHeight(60)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        text_edit = QLineEdit()
        text_edit.setPlaceholderText("E,N")
        text_edit.setFixedSize(270, 60)
        font = QFont()
        font.setPointSize(25)
        font.setBold(True)
        font.setFamily("Arial")
        text_edit.setFont(font)
        text_edit.setStyleSheet("QLineEdit { color: blue; background-color: white; }")

        delete_btn = QPushButton()
        delete_btn.setFixedSize(50, 60)
        delete_btn.setIcon(self.style().standardIcon(QPushButton().style().SP_TrashIcon))
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.setStyleSheet(
            "QPushButton { border: none; } QPushButton:hover { background-color: #ffdddd; }"
        )

        layout.addWidget(text_edit)
        layout.addWidget(delete_btn)

        proxy = QGraphicsProxyWidget()
        proxy.setWidget(container)
        self.scene.addItem(proxy)
        proxy.setPos(x, y)
        proxy.setFlag(proxy.ItemIsMovable, True)

        point_data = {"xy": [x, y], "text_edit": text_edit}
        self.align_points.append(point_data)

        def remove_widget():
            self.scene.removeItem(proxy)
            proxy.deleteLater()
            self.align_points.remove(point_data)

        delete_btn.clicked.connect(remove_widget)
