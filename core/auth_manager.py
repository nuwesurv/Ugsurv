# -*- coding: utf-8 -*-
import platform
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QSettings, QThread, QObject, pyqtSignal

try:
    from qgis.core import Qgis as _Qgis
    _QGIS_VER = _Qgis.QGIS_VERSION
except Exception:
    _QGIS_VER = "unknown"

_PLUGIN_VER  = "0.4"
_OS_INFO     = f"{platform.system()} {platform.release()}"
_SETTINGS_KEY = "ugsurv/auth/username"


class _SyncWorker(QObject):
    done = pyqtSignal()

    def __init__(self, username: str, payload: dict):
        super().__init__()
        self._username = username
        self._payload  = payload

    def run(self):
        try:
            from .supabase_client import update_session
            update_session(self._username, self._payload)
        except Exception:
            pass
        self.done.emit()


class AuthManager:
    """Singleton — manages local username and Supabase session sync."""

    _instance: "AuthManager | None" = None

    @classmethod
    def get(cls) -> "AuthManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def __init__(self):
        self._settings       = QSettings()
        self._session_synced = False
        self._thread: QThread | None    = None
        self._worker: _SyncWorker | None = None

    # ── username accessors ────────────────────────────────────────────────

    def username(self) -> str | None:
        val = self._settings.value(_SETTINGS_KEY)
        return val.strip() if val else None

    def is_authenticated(self) -> bool:
        return bool(self.username())

    def save_username(self, name: str) -> None:
        self._settings.setValue(_SETTINGS_KEY, name.strip())
        self._session_synced = False

    def clear_username(self) -> None:
        self._settings.remove(_SETTINGS_KEY)
        self._session_synced = False

    # ── Supabase sync ─────────────────────────────────────────────────────

    def record_session(self) -> None:
        """Upsert user row once per plugin session (fire-and-forget)."""
        if self._session_synced:
            return
        username = self.username()
        if not username:
            return
        self._session_synced = True
        payload = {
            "username":   username,
            "last_seen":  datetime.now(timezone.utc).isoformat(),
            "plugin_ver": _PLUGIN_VER,
            "qgis_ver":   _QGIS_VER,
            "os_info":    _OS_INFO,
        }
        self._worker = _SyncWorker(username, payload)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

