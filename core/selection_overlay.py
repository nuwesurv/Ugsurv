# -*- coding: utf-8 -*-
"""
SelectionOverlay — rubber-band highlights drawn over selected features.

Connects to SelectionModel.selectionChanged and redraws rubber bands
every time the selection set changes.  One QgsRubberBand per feature,
styled with the amber SELECT_OVERLAY colour from style.py.

Created once in Ugsurv._setup() and destroyed in _teardown().
"""

from qgis.core import QgsProject
from qgis.gui import QgsRubberBand

from . import style as _style


class SelectionOverlay:
    def __init__(self, canvas, selection_model):
        self._canvas = canvas
        self._sel    = selection_model
        self._bands: list[QgsRubberBand] = []
        selection_model.selectionChanged.connect(self._rebuild)

    # ── rebuild on every selection change ────────────────────────────────
    def _rebuild(self):
        self._clear()
        project = QgsProject.instance()
        for lid, fid in self._sel:
            layer = project.mapLayer(lid)
            if not layer:
                continue
            feat = layer.getFeature(fid)
            if not feat.isValid():
                continue
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            rb = QgsRubberBand(self._canvas, geom.type())
            rb.setToGeometry(geom)
            rb.setColor(_style.SELECT_OVERLAY)
            rb.setFillColor(_style.SELECT_OVERLAY_FILL)
            rb.setWidth(_style.RB_WIDTH)
            rb.setLineStyle(_style.RB_LINE_STYLE)
            self._bands.append(rb)

    def _clear(self):
        scene = self._canvas.scene()
        for rb in self._bands:
            try:
                scene.removeItem(rb)
            except Exception:
                try:
                    rb.reset()
                    rb.hide()
                except Exception:  # nosec B110
                    pass
        self._bands.clear()

    def set_editing(self, editing: bool):
        """Hide bands while a vertex is being live-dragged; rebuild after."""
        if editing:
            for rb in self._bands:
                try:
                    rb.hide()
                except Exception:  # nosec B110
                    pass
        else:
            self._rebuild()

    def destroy(self):
        self._clear()
        try:
            self._sel.selectionChanged.disconnect(self._rebuild)
        except Exception:  # nosec B110
            pass
