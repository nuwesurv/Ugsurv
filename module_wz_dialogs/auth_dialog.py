# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import Qt, QThread, QObject, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFrame,
)

_REQUIRED_ROLE = "admin"


class _CheckWorker(QObject):
    done = pyqtSignal(object)  # dict | None

    def __init__(self, username: str):
        super().__init__()
        self._username = username

    def run(self):
        from ..core.supabase_client import get_user
        self.done.emit(get_user(self._username))


class AuthDialog(QDialog):
    """Sign-in dialog: verifies username against the Supabase users table."""

    def __init__(self, auth_manager, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self._mgr    = auth_manager
        self._thread = None
        self._worker = None
        self.setWindowTitle("Ugsurv — Sign In Required")
        self.setMinimumWidth(380)
        self.setMaximumWidth(480)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        heading = QLabel("Sign In to Continue")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = heading.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        heading.setFont(f)
        root.addWidget(heading)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(sep)

        body = QLabel(
            "Enter your username.\n"
            "You must be registered in the system to access advanced tools."
        )
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        root.addWidget(body)

        name_row = QHBoxLayout()
        name_lbl = QLabel("Username:")
        name_lbl.setFixedWidth(80)
        name_row.addWidget(name_lbl)

        self._name_le = QLineEdit()
        self._name_le.setPlaceholderText("e.g. surveyor_x")
        saved = self._mgr.username()
        if saved:
            self._name_le.setText(saved)
            self._name_le.selectAll()
        self._name_le.returnPressed.connect(self._on_continue)
        name_row.addWidget(self._name_le)
        root.addLayout(name_row)

        if saved:
            hint = QLabel(
                f'<small>Last signed in as <b>{saved}</b>. '
                '<a href="clear">Use a different name?</a></small>'
            )
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setOpenExternalLinks(False)
            hint.linkActivated.connect(lambda _: (
                self._name_le.clear(), self._name_le.setFocus()
            ))
            root.addWidget(hint)

        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)

        self._ok_btn = QPushButton("Continue")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._on_continue)
        btn_row.addWidget(self._ok_btn)

        root.addLayout(btn_row)
        self._name_le.setFocus()

    # ── sign-in flow ──────────────────────────────────────────────────────

    def _on_continue(self):
        name = self._name_le.text().strip()
        if not name:
            self._set_status("Please enter your username.", "#ff6666")
            return

        self._set_status("Checking …", "#aaaaaa")
        self._ok_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)

        self._worker = _CheckWorker(name)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_check_done)
        self._thread.start()

    def _on_check_done(self, result):
        self._thread.quit()
        self._thread.wait()

        name = self._name_le.text().strip()

        if result is None:
            self._set_status(
                "Username not recognised. Contact the administrator.", "#ff6666"
            )
            self._reset_buttons()
            return

        if "_error" in result:
            self._set_status(f"Connection error: {result['_error']}", "#ff6666")
            self._reset_buttons()
            return

        if result.get("role") != _REQUIRED_ROLE:
            self._set_status(
                f"Access denied — '{name}' does not have the required role.", "#ff6666"
            )
            self._reset_buttons()
            return

        self._mgr.save_username(name)
        self.accept()

    def _reset_buttons(self):
        self._ok_btn.setEnabled(True)
        self._cancel_btn.setEnabled(True)

    def _set_status(self, msg: str, color: str = "#cccccc"):
        self._status_lbl.setText(f'<span style="color:{color};">{msg}</span>')
