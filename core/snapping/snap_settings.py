# -*- coding: utf-8 -*-
"""SnapSettings — per-provider enable/disable and global tolerance."""


from .. import style as _style


class SnapSettings:
    """Mutable settings bag shared by SnapEngine and the SnapSettingsDock UI."""

    DEFAULT_TOLERANCE_PX = _style._SNAP_PX

    def __init__(self):
        self._enabled: dict[str, bool] = {}   # provider_key → bool
        self.tolerance_px: int = self.DEFAULT_TOLERANCE_PX

    def set_enabled(self, key: str, state: bool):
        self._enabled[key] = state

    def is_enabled(self, key: str) -> bool:
        return self._enabled.get(key, False)   # all off by default (§9)

    def any_enabled(self) -> bool:
        return any(self._enabled.values())

    def all_keys(self) -> list[str]:
        return list(self._enabled.keys())
