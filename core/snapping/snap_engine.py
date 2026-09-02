# -*- coding: utf-8 -*-
"""
SnapEngine — queries all active SnapProviders within tolerance on each mouse
move, collects candidates, resolves to the best match.

All providers default to ON.  The SnapSettingsDialog drives set_enabled().

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


# Tier 1 — exact discrete points: distance wins within the tier.
# Tier 2 — computed intersections.
# Tier 3 — relational (direction-dependent): perpendicular, extension.
# Tier 4 — sliding: nearest fires only when nothing above matched.
# Tier 5 — background grid: last resort.
_SNAP_TIER: dict[SnapType, int] = {
    SnapType.VERTEX:        1,
    SnapType.POINT:         1,
    SnapType.SELF:          1,
    SnapType.MIDPOINT:      1,
    SnapType.CENTER:        1,
    SnapType.INTERSECTION:  2,
    SnapType.PERPENDICULAR: 3,
    SnapType.EXTENSION:     3,
    SnapType.NEAREST:       4,
    SnapType.GRID:          5,
}


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

        within = [r for r in candidates if r.distance <= tolerance_mu]
        if not within:
            return None

        # Sort by (tier, distance): higher tier loses even if closer to cursor.
        return min(within, key=lambda r: (_SNAP_TIER.get(r.snap_type, 99), r.distance))

    @staticmethod
    def _px_to_map_units(px: int, canvas) -> float:
        try:
            return px * canvas.mapUnitsPerPixel()
        except Exception:
            return px * 0.001
