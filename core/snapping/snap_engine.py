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

# Beyond this map-unit radius the snap search rect becomes so large that
# getFeatures() returns the entire dataset and the intersection provider's
# O(n²) loop crashes QGIS.  500 m is already unreachable in normal drawing
# work (EPSG:32636 units are metres).
_MAX_TOLERANCE_MU = 2000.0


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
        if tolerance_mu > _MAX_TOLERANCE_MU:
            return None

        # Providers query layers stored in EPSG:32636.  When the project CRS
        # differs, raw_point is in project CRS so we must transform it before
        # building the search rect and passing it to providers.
        crs_mgr  = getattr(self, '_crs_mgr', None)
        layer_raw = crs_mgr.project_to_layer(raw_point) if crs_mgr else raw_point

        search_rect = QgsRectangle(
            layer_raw.x() - tolerance_mu, layer_raw.y() - tolerance_mu,
            layer_raw.x() + tolerance_mu, layer_raw.y() + tolerance_mu,
        )

        candidates: list[SnapResult] = []
        for key, provider in self._providers.items():
            if not self._settings.is_enabled(key):
                continue
            hits = provider.query(layer_raw, search_rect, self._storage)
            candidates.extend(hits)

        within = [r for r in candidates if r.distance <= tolerance_mu]
        if not within:
            return None

        # Sort by (tier, distance): higher tier loses even if closer to cursor.
        best = min(within, key=lambda r: (_SNAP_TIER.get(r.snap_type, 99), r.distance))

        if crs_mgr:
            # Snap point is in layer CRS — convert back to project CRS for callers.
            best = SnapResult(
                crs_mgr.layer_to_project(best.point),
                best.snap_type,
                best.distance,
            )
            # ExtensionProvider exposes active_endpoint / active_direction so
            # drawing tools can follow the extension line.  Both must be in
            # project CRS after this transform.
            ext_prov = self._providers.get("extension")
            if ext_prov and ext_prov.active_endpoint:
                ep_layer = ext_prov.active_endpoint
                ep_proj  = crs_mgr.layer_to_project(ep_layer)
                if ext_prov.active_direction:
                    ux, uy = ext_prov.active_direction
                    # Step one unit in layer CRS then project to get direction.
                    ep2_proj = crs_mgr.layer_to_project(
                        QgsPointXY(ep_layer.x() + ux, ep_layer.y() + uy)
                    )
                    dx, dy = ep2_proj.x() - ep_proj.x(), ep2_proj.y() - ep_proj.y()
                    ln = (dx * dx + dy * dy) ** 0.5
                    if ln > 1e-10:
                        ext_prov.active_direction = (dx / ln, dy / ln)
                ext_prov.active_endpoint = ep_proj

        return best

    @staticmethod
    def _px_to_map_units(px: int, canvas) -> float:
        try:
            return px * canvas.mapUnitsPerPixel()
        except Exception:
            return px * 0.001
