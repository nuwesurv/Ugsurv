# -*- coding: utf-8 -*-
"""
DataCheckDock — compares Excel / CSV files in a folder against a QGIS layer,
reporting which files contain PINs that have not been loaded into the layer.

File reading runs synchronously in the main thread with processEvents() between
files to avoid the libxml2 thread-safety crash that occurs when openpyxl is used
inside a QThread alongside QGIS/Qt's own XML processing.
"""
import os
import csv

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog,
    QTreeWidget, QTreeWidgetItem, QComboBox, QCheckBox, QApplication,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel


_MAX_PIN_ROWS = 200   # max individual PIN rows shown per file before truncating


# ── Dock widget ───────────────────────────────────────────────────────────────

class DataCheckDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Data Load Checker")
        self.setMaximumWidth(420)
        self._results  = []
        self._running  = False

        root = QWidget()
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(6, 6, 6, 6)
        vbox.setSpacing(4)

        H = 26

        # Folder picker
        folder_row = QHBoxLayout()
        folder_lbl = QLabel("Folder:")
        folder_lbl.setFixedWidth(100)
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("Folder containing Excel / CSV files…")
        self._folder_edit.setFixedHeight(H)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(30)
        browse_btn.setFixedHeight(H)
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(folder_lbl)
        folder_row.addWidget(self._folder_edit)
        folder_row.addWidget(browse_btn)
        vbox.addLayout(folder_row)

        # Layer picker
        layer_row = QHBoxLayout()
        layer_lbl = QLabel("Layer:")
        layer_lbl.setFixedWidth(100)
        self._layer_combo = QgsMapLayerComboBox()
        self._layer_combo.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        self._layer_combo.setFixedHeight(H)
        self._layer_combo.layerChanged.connect(self._on_layer_changed)
        layer_row.addWidget(layer_lbl)
        layer_row.addWidget(self._layer_combo)
        vbox.addLayout(layer_row)

        # Layer PIN field
        lfield_row = QHBoxLayout()
        lfield_lbl = QLabel("Layer PIN field:")
        lfield_lbl.setFixedWidth(100)
        self._field_combo = QComboBox()
        self._field_combo.setFixedHeight(H)
        lfield_row.addWidget(lfield_lbl)
        lfield_row.addWidget(self._field_combo)
        vbox.addLayout(lfield_row)

        # File PIN column
        col_row = QHBoxLayout()
        col_lbl = QLabel("File PIN column:")
        col_lbl.setFixedWidth(100)
        self._col_edit = QLineEdit()
        self._col_edit.setPlaceholderText("Column header in Excel/CSV (e.g. PIN)")
        self._col_edit.setFixedHeight(H)
        col_row.addWidget(col_lbl)
        col_row.addWidget(self._col_edit)
        vbox.addLayout(col_row)

        # Status
        self._status = QLabel("Ready.")
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self._status.setCursor(Qt.IBeamCursor)
        vbox.addWidget(self._status)

        # Run button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._run_btn = QPushButton("Run Check")
        self._run_btn.setFixedHeight(H + 4)
        self._run_btn.clicked.connect(self._run)
        btn_row.addWidget(self._run_btn)
        vbox.addLayout(btn_row)

        # Results tree
        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["File", "Status", "Actions"])
        self._tree.setColumnWidth(0, 240)
        self._tree.setColumnWidth(2, 130)
        self._tree.header().setStretchLastSection(False)
        self._tree.setAlternatingRowColors(True)
        vbox.addWidget(self._tree, 1)

        # Bottom row: copy button only
        bottom_row = QHBoxLayout()
        self._copy_btn = QPushButton("Copy unloaded PINs")
        self._copy_btn.setEnabled(False)
        self._copy_btn.clicked.connect(self._copy_unloaded)
        bottom_row.addWidget(self._copy_btn)
        bottom_row.addStretch()
        vbox.addLayout(bottom_row)

        self.setWidget(root)
        self._on_layer_changed(self._layer_combo.currentLayer())

    # ── Layer field population ────────────────────────────────────────────

    def _on_layer_changed(self, layer):
        self._field_combo.clear()
        if layer is None:
            return
        for field in layer.fields():
            self._field_combo.addItem(field.name())

    # ── Folder browse ─────────────────────────────────────────────────────

    def _browse_folder(self):
        start = self._folder_edit.text() or os.path.expanduser("~\\Desktop")
        folder = QFileDialog.getExistingDirectory(
            self, "Select folder with Excel / CSV files", start
        )
        if folder:
            self._folder_edit.setText(folder)

    # ── Status helper ─────────────────────────────────────────────────────

    def _set_status(self, msg, color="green"):
        if sip.isdeleted(self) or sip.isdeleted(self._status):
            return
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(msg)

    # ── Run (synchronous — avoids libxml2 thread-safety crash) ───────────

    def _run(self):
        if self._running:
            return

        folder     = self._folder_edit.text().strip()
        layer      = self._layer_combo.currentLayer()
        field_name = self._field_combo.currentText()
        pin_col    = self._col_edit.text().strip()

        if not folder or not os.path.isdir(folder):
            self._set_status("Select a valid folder first.", "red"); return
        if layer is None:
            self._set_status("Select a layer first.", "red"); return
        if not field_name:
            self._set_status("Select the PIN field in the layer.", "red"); return
        if not pin_col:
            self._set_status("Enter the PIN column name used in the files.", "red"); return

        self._running = True
        self._run_btn.setEnabled(False)
        self._tree.clear()
        self._results = []
        self._copy_btn.setEnabled(False)

        self._set_status("Reading layer PINs…", "orange")
        QApplication.processEvents()
        layer_pins = self._collect_layer_pins(layer, field_name)

        files = self._collect_files(folder)
        self._set_status(f"Found {len(files)} file(s). Scanning…", "orange")
        QApplication.processEvents()

        results = []
        for i, fpath in enumerate(files):
            if sip.isdeleted(self):
                return
            rel = os.path.relpath(fpath, folder)
            ext = os.path.splitext(fpath)[1].lower()
            self._set_status(
                f"Reading {os.path.basename(fpath)}  ({i+1}/{len(files)})…", "orange"
            )
            QApplication.processEvents()
            try:
                file_pins  = self._read_pins(fpath, ext, pin_col)
                not_loaded = [p for p in file_pins if p not in layer_pins]
                results.append({
                    'file':       rel,
                    'path':       fpath,
                    'total':      len(file_pins),
                    'not_loaded': not_loaded,
                    'error':      None,
                })
            except Exception as exc:
                results.append({
                    'file':       rel,
                    'path':       fpath,
                    'total':      0,
                    'not_loaded': [],
                    'error':      str(exc),
                })

        self._running = False
        self._run_btn.setEnabled(True)
        self._results = results

        n_files    = len(results)
        n_issues   = sum(1 for r in results if r['not_loaded'] or r['error'])
        n_unloaded = sum(len(r['not_loaded']) for r in results)
        self._set_status(
            f"Done. {n_files} file(s) scanned — "
            f"{n_issues} file(s) with unloaded PINs "
            f"({n_unloaded} total unloaded PIN(s)).",
            "green" if n_issues == 0 else "darkorange",
        )
        self._refresh_tree()
        self._copy_btn.setEnabled(n_unloaded > 0)

    # ── File collection ───────────────────────────────────────────────────

    @staticmethod
    def _collect_files(folder):
        exts  = {'.xlsx', '.xls', '.csv'}
        files = []
        for root, _, fnames in os.walk(folder):
            for fn in fnames:
                if os.path.splitext(fn)[1].lower() in exts:
                    files.append(os.path.join(root, fn))
        files.sort()
        return files

    # ── Layer PIN collection ──────────────────────────────────────────────

    @staticmethod
    def _collect_layer_pins(layer, field_name):
        pins = set()
        idx  = layer.fields().indexOf(field_name)
        if idx < 0:
            return pins
        for feat in layer.getFeatures():
            val = feat[idx]
            s   = str(val).strip() if val is not None else ''
            if s and s.upper() != 'NULL':
                pins.add(s)
        return pins

    # ── PIN reading ───────────────────────────────────────────────────────

    @staticmethod
    def _read_pins(fpath, ext, col):
        col = col.strip()
        if ext == '.csv':
            pins = DataCheckDock._read_csv(fpath, col)
        elif ext == '.xlsx':
            pins = DataCheckDock._read_xlsx(fpath, col)
        elif ext == '.xls':
            pins = DataCheckDock._read_xls(fpath, col)
        else:
            pins = []
        return [p for p in pins if p]

    @staticmethod
    def _read_csv(fpath, col):
        for enc in ('utf-8-sig', 'latin-1', 'cp1252'):
            try:
                with open(fpath, newline='', encoding=enc) as f:
                    reader = csv.DictReader(f)
                    if not reader.fieldnames:
                        return []
                    header_map = {h.strip().lower(): h for h in reader.fieldnames}
                    real_col   = header_map.get(col.lower())
                    if real_col is None:
                        return []
                    return [str(row.get(real_col, '') or '').strip() for row in reader]
            except UnicodeDecodeError:
                continue
        return []

    @staticmethod
    def _read_xlsx(fpath, col):
        return DataCheckDock._read_via_ogr(fpath, col)

    @staticmethod
    def _read_xls(fpath, col):
        return DataCheckDock._read_via_ogr(fpath, col)

    @staticmethod
    def _read_via_ogr(fpath, col):
        """Read a column from xlsx/xls using GDAL OGR — avoids openpyxl / libxml2 crash."""
        from qgis.core import QgsVectorLayer
        layer = QgsVectorLayer(fpath, "_dc_tmp", "ogr")
        if not layer.isValid():
            raise IOError(f"OGR could not open file: {os.path.basename(fpath)}")
        col_lower = col.lower()
        idx = next(
            (layer.fields().indexOf(f.name())
             for f in layer.fields() if f.name().lower() == col_lower),
            -1,
        )
        if idx < 0:
            return []
        pins = []
        for feat in layer.getFeatures():
            val = feat[idx]
            s   = str(val).strip() if val is not None else ''
            if s and s.upper() != 'NULL':
                pins.append(s)
        return pins

    # ── Tree display ──────────────────────────────────────────────────────

    def _refresh_tree(self):
        self._tree.clear()

        for r in self._results:
            if not r['not_loaded'] and not r['error']:
                continue

            if r['error']:
                item = QTreeWidgetItem(self._tree, [r['file'], f"Error: {r['error'][:80]}", ""])
                item.setForeground(0, Qt.red)
                item.setForeground(1, Qt.red)

            elif r['not_loaded']:
                n      = len(r['not_loaded'])
                loaded = r['total'] - n
                status = f"{n} not loaded  ({loaded}/{r['total']} loaded)"
                item   = QTreeWidgetItem(self._tree, [r['file'], status, ""])
                item.setForeground(1, Qt.darkYellow)

                for pin in r['not_loaded'][:_MAX_PIN_ROWS]:
                    child = QTreeWidgetItem(item, [pin, "not loaded", ""])
                    child.setForeground(1, Qt.red)
                if len(r['not_loaded']) > _MAX_PIN_ROWS:
                    extra = len(r['not_loaded']) - _MAX_PIN_ROWS
                    QTreeWidgetItem(item, [f"… and {extra} more", "", ""])

            else:
                status = f"All loaded  ({r['total']} PIN(s))"
                item   = QTreeWidgetItem(self._tree, [r['file'], status, ""])
                item.setForeground(1, Qt.darkGreen)

            self._tree.setItemWidget(item, 2, self._make_file_buttons(r['path']))

        # Summary row
        if self._results:
            n_unloaded = sum(len(r['not_loaded']) for r in self._results)
            n_issues   = sum(1 for r in self._results if r['not_loaded'] or r['error'])
            summary    = QTreeWidgetItem(
                self._tree,
                ["─── Summary ───",
                 f"{len(self._results)} files  |  {n_issues} with issues  |  {n_unloaded} unloaded PINs",
                 ""],
            )
            font = summary.font(0)
            font.setBold(True)
            summary.setFont(0, font)
            summary.setFont(1, font)

        self._tree.resizeColumnToContents(0)

    def _make_file_buttons(self, fpath):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 1, 2, 1)
        h.setSpacing(3)
        open_btn = QPushButton("Open")
        open_btn.setFixedHeight(22)
        open_btn.setToolTip("Open file in default application")
        open_btn.clicked.connect(lambda: self._open_file(fpath))
        load_btn = QPushButton("Load")
        load_btn.setFixedHeight(22)
        load_btn.setToolTip("Plot coordinates as a QGIS point layer")
        load_btn.clicked.connect(lambda: self._load_file(fpath))
        h.addWidget(open_btn)
        h.addWidget(load_btn)
        return w

    def _open_file(self, fpath):
        try:
            os.startfile(fpath)
        except Exception as exc:
            self._set_status(f"Could not open file: {exc}", "red")

    def _load_file(self, fpath):
        from .feature_navigator import _XYPickerDialog
        from qgis.PyQt.QtWidgets import QDialog, QMessageBox
        headers = self._read_file_headers(fpath)
        if not headers:
            QMessageBox.warning(self, "Load as layer",
                                "Could not read column headers from file.")
            return
        dlg = _XYPickerDialog(os.path.basename(fpath), headers, self)
        if dlg.exec() != QDialog.Accepted:
            return
        x_col, y_col, crs_str = dlg.result_values()
        self._do_load_layer(fpath, x_col, y_col, crs_str)

    @staticmethod
    def _read_file_headers(fpath):
        try:
            if fpath.lower().endswith('.csv'):
                for enc in ('utf-8-sig', 'latin-1', 'cp1252'):
                    try:
                        with open(fpath, newline='', encoding=enc) as f:
                            return next(csv.reader(f), [])
                    except UnicodeDecodeError:
                        continue
                return []
            # xlsx / xls — use OGR to avoid openpyxl/libxml2 conflict
            from qgis.core import QgsVectorLayer
            layer = QgsVectorLayer(fpath, "_dc_hdr", "ogr")
            if not layer.isValid():
                return []
            return [f.name() for f in layer.fields()]
        except Exception:
            return []

    def _do_load_layer(self, fpath, x_col, y_col, crs_str):
        import urllib.parse
        import tempfile
        from qgis.core import QgsVectorLayer, QgsProject
        from qgis.PyQt.QtWidgets import QMessageBox
        layer_name = os.path.splitext(os.path.basename(fpath))[0]
        try:
            if fpath.lower().endswith('.csv'):
                csv_path = fpath
            else:
                import openpyxl
                wb   = openpyxl.load_workbook(fpath, read_only=True, data_only=True)
                rows = list(wb.active.iter_rows(values_only=True))
                wb.close()
                if not rows:
                    QMessageBox.warning(self, "Load as layer", "File is empty.")
                    return
                import csv as _csv
                hdrs = [str(c) for c in rows[0]]
                tmp  = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.csv', delete=False,
                    encoding='utf-8-sig', newline='')
                writer = _csv.writer(tmp)
                writer.writerow(hdrs)
                for r in rows[1:]:
                    writer.writerow(['' if c is None else str(c) for c in r])
                tmp.close()
                csv_path = tmp.name

            x_enc  = urllib.parse.quote(x_col)
            y_enc  = urllib.parse.quote(y_col)
            fwd    = csv_path.replace(os.sep, '/')
            uri    = (f'file:///{fwd}?delimiter=,&'
                      f'xField={x_enc}&yField={y_enc}&crs={crs_str}')
            vlayer = QgsVectorLayer(uri, layer_name, 'delimitedtext')
            if not vlayer.isValid():
                QMessageBox.warning(
                    self, "Load as layer",
                    "Layer is not valid.\n"
                    "Check that the selected X/Y columns contain numeric coordinates.")
                return
            QgsProject.instance().addMapLayer(vlayer)
            self._set_status(f"Loaded: {layer_name}", "green")
        except Exception as exc:
            self._set_status(f"Load error: {exc}", "red")

    # ── Copy unloaded PINs to clipboard ───────────────────────────────────

    def _copy_unloaded(self):
        all_pins = sorted({
            pin
            for r in self._results
            for pin in r['not_loaded']
        })
        if all_pins:
            QApplication.clipboard().setText('\n'.join(all_pins))
            self._set_status(
                f"Copied {len(all_pins)} unique unloaded PIN(s) to clipboard.", "green"
            )
