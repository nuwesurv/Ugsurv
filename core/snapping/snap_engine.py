# -*- coding: utf-8 -*-
"""
SnapEngine — queries all active SnapProviders within tolerance on each mouse
move, collects candidates, resolves to the best match.

All providers default to OFF (§9).  The SnapSettingsDock drives set_enabled().

Pipeline (§9):
    canvasMoveEvent → SnapEngine.resolve(raw_point, canvas)
                    → SnapResult(point, snap_type)
                    → InputTranslator uses result
"""

from dataclasses import dataclass
from typing import Optional

from qgis.core import QgsPointXY, QgsRectangle
from ..events import SnapType


@dataclass
class SnapResult:
    point:     QgsPointXY
    snap_type: SnapType
    distance:  float        # map units


class SnapEngine:
    """Aggregates all registered SnapProviders and resolves the best match."""

    def __init__(self, settings, storage_manager):
        self._settings  = settings
        self._storage   = storage_manager
        self._providers = {}   # key → provider instance

    def register_provider(self, key: str, provider):
        self._providers[key] = provider

    def resolve(self, raw_point: QgsPointXY, canvas) -> Optional[SnapResult]:
        if not self._settings.any_enabled():
            return None

        tolerance_mu = self._px_to_map_units(
            self._settings.tolerance_px, canvas
        )
        search_rect  = QgsRectangle(
            raw_point.x() - tolerance_mu, raw_point.y() - tolerance_mu,
            raw_point.x() + tolerance_mu, raw_point.y() + tolerance_mu,
        )

        candidates: list[SnapResult] = []
        for key, provider in self._providers.items():
            if not self._settings.is_enabled(key):
                continue
            hits = provider.query(raw_point, search_rect, self._storage)
            candidates.extend(hits)

        if not candidates:
            return None

        # nearest wins; no hardcoded priority
        best = min(candidates, key=lambda r: r.distance)
        return best if best.distance <= tolerance_mu else None

    @staticmethod
    def _px_to_map_units(px: int, canvas) -> float:
        try:
            return px * canvas.mapUnitsPerPixel()
        except Exception:
            return px * 0.001
