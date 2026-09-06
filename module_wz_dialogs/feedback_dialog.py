# -*- coding: utf-8 -*-
import platform

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QObject
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QTextEdit, QComboBox,
    QPushButton, QFrame,
)
from qgis.PyQt.QtGui import QFont

try:
    from qgis.core import Qgis
    _QGIS_VER = Qgis.QGIS_VERSION
except Exception:
    _QGIS_VER = "unknown"

_PLUGIN_VER = "0.4"
_OS_INFO = f"{platform.system()} {platform.release()}"


class _Sender(QObject):
    done = pyqtSignal(object)  # None = success, str = error message

    def __init__(self, payload):
        super().__init__()
        self._payload = payload

    def run(self):
        from ..core.supabase_client import insert_feedback
        err = insert_feedback(self._payload)
        self.done.emit(err)


class FeedbackDialog(QDialog):
    """Simple one-shot form to send feedback / bug reports to Supabase."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle("Send Feedback / Report Bug")
        self.setMinimumWidth(480)
        self._thread = None
        self._sender = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ── type + title ──────────────────────────────────────────────────
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._type_cb = QComboBox()
        self._type_cb.addItems(["Bug Report", "Feedback", "Suggestion"])
        form.addRow("Type:", self._type_cb)

        self._title_le = QLineEdit()
        self._title_le.setPlaceholderText("Short summary (optional)")
        form.addRow("Title:", self._title_le)

        root.addLayout(form)

        # ── description ───────────────────────────────────────────────────
        root.addWidget(QLabel("Description:"))
        self._desc_te = QTextEdit()
        self._desc_te.setPlaceholderText(
            "Describe the issue or feedback in as much detail as possible …"
        )
        self._desc_te.setMinimumHeight(140)
        root.addWidget(self._desc_te)

        # ── auto-collected info ───────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(sep)

        info_lbl = QLabel(
            f"<small><i>Auto-attached: QGIS {_QGIS_VER} &nbsp;|&nbsp; "
            f"Plugin {_PLUGIN_VER} &nbsp;|&nbsp; {_OS_INFO}</i></small>"
        )
        info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(info_lbl)

        # ── status label ──────────────────────────────────────────────────
        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        # ── buttons ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)

        self._submit_btn = QPushButton("Submit")
        self._submit_btn.setDefault(True)
        self._submit_btn.clicked.connect(self._on_submit)
        btn_row.addWidget(self._submit_btn)

        root.addLayout(btn_row)

    # ── submission ────────────────────────────────────────────────────────

    def _on_submit(self):
        desc = self._desc_te.toPlainText().strip()
        if not desc:
            self._set_status("Please enter a description.", "#ff6666")
            return

        payload = {
            "type":        self._type_cb.currentText(),
            "title":       self._title_le.text().strip() or None,
            "description": desc,
            "qgis_ver":    _QGIS_VER,
            "plugin_ver":  _PLUGIN_VER,
            "os_info":     _OS_INFO,
        }

        self._submit_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._set_status("Sending …", "#aaaaaa")

        self._sender = _Sender(payload)
        self._thread = QThread()
        self._sender.moveToThread(self._thread)
        self._thread.started.connect(self._sender.run)
        self._sender.done.connect(self._on_done)
        self._thread.start()

    def _on_done(self, err):
        self._thread.quit()
        self._thread.wait()
        if err is None:
            self._set_status("Thank you! Your report was sent successfully.", "#44cc88")
            self._submit_btn.setText("Sent")
            self._cancel_btn.setText("Close")
            self._cancel_btn.setEnabled(True)
        else:
            self._set_status(f"Failed to send: {err}", "#ff6666")
            self._submit_btn.setEnabled(True)
            self._cancel_btn.setEnabled(True)

    def _set_status(self, msg: str, color: str = "#cccccc"):
        self._status_lbl.setText(
            f'<span style="color:{color};">{msg}</span>'
        )
