# -*- coding: utf-8 -*-
"""
AutoCAD-style CHAMFER tool.

Workflow
────────
Type d1 (or d1,d2 as coordinate) in command line + Enter → sets distances.
Click first line  near the corner end  → highlighted cyan.
Click second line near the corner end  → chamfer applied immediately:
  • Both lines trimmed by their respective distances from the corner.
  • A straight chamfer segment connects the two new endpoints.
RMB after selecting line1  → reset to line1 picking.
Type new distances at any time to update them.
Esc → exit.

d1=d2=0 → sharp corner (trim both lines to intersection).
"""

import contextlib
import math

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.events import EventType
from ...core import style as _style
from ..layer_utils import polyline_attrs

_C_LINE1  = _style.RB_SOURCE
_C_HOVER  = _style.RB_HOVER

_HIT_PX = 10

_HINT_STYLE = (
    "QLabel{background:rgba(20,20,20,210);color:#f0f0f0;"
    "border:1px solid rgba(255,255,255,80);border-radius:4px;"
    "padding:3px 8px;font-size:9pt;}"
)

_ST_LINE1 = 0
_ST_LINE2 = 1

_HINT = {
    _ST_LINE1: "Click first line — near the corner end  (type d1,d2 to set distances)",
    _ST_LINE2: "Click second line — near the corner end",
}


class ChamferTool(QgsMapTool):
    """AutoCAD-style CHAMFER — bevel two lines with a straight segment."""

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas)
        self._canvas     = canvas
        self._ctx        = ctx
        self._translator = translator

        self._state  = _ST_LINE1
        self._dist1  = 0.0
        self._dist2  = 0.0

        self._line1_layer = None
        self._line1_feat  = None
        self._line1_click = None

        self._hover_lyr_id = None
        self._hover_fid    = None
        self._hover_seg    = None

        self._line1_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.LineGeometry)
        self._line1_band.setColor(_C_LINE1)
        self._line1_band.setWidth(_style.RB_WIDTH)
        self._line1_band.setLineStyle(_style.RB_LINE_STYLE)
        self._line1_band.setVisible(False)

        self._hover_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.LineGeometry)
        self._hover_band.setColor(_C_HOVER)
        self._hover_band.setWidth(_style.RB_WIDTH)
        self._hover_band.setLineStyle(_style.RB_LINE_STYLE)
        self._hover_band.setVisible(False)

        self._hint = QLabel(canvas)
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _hit_tol(self):
        return _HIT_PX * self._canvas.mapUnitsPerPixel()

    def _rm(self, item):
        if item is not None:
            with contextlib.suppress(Exception):
                self._canvas.scene().removeItem(item)

    def _go_home(self):
        go = getattr(self._ctx, 'go_home', None)
        if go and callable(go):
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, go)

    def _show_hint(self, screen_pos):
        text = _HINT.get(self._state, "")
        if not text:
            self._hint.hide()
            return
        self._hint.setText(text)
        self._hint.adjustSize()
        pos = screen_pos + QPoint(10, 14)
        if pos.x() + self._hint.width() > self._canvas.width():
            pos.setX(screen_pos.x() - self._hint.width() - 4)
        if pos.y() + self._hint.height() > self._canvas.height():
            pos.setY(screen_pos.y() - self._hint.height() - 4)
        self._hint.move(pos)
        self._hint.show()
        self._hint.raise_()

    def _line_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.isSpatial()
            and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.GeometryType.LineGeometry
        ]

    def _find_line_near(self, map_pt):
        tol  = self._hit_tol()
        rect = QgsRectangle(map_pt.x()-tol, map_pt.y()-tol,
                            map_pt.x()+tol, map_pt.y()+tol)
        cg   = QgsGeometry.fromPointXY(map_pt)
        best_layer, best_feat, best_d = None, None, float('inf')
        for lyr in self._line_layers():
            for feat in lyr.getFeatures(rect):
                if feat.geometry().isEmpty():
                    continue
                d = feat.geometry().distance(cg)
                if d < best_d:
                    best_d, best_layer, best_feat = d, lyr, feat
        return (best_layer, best_feat) if best_d <= tol else (None, None)

    def _nearest_segment(self, pts, click_pt):
        best_i, best_d = 0, float('inf')
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
            seg_sq = dx * dx + dy * dy
            if seg_sq < 1e-14:
                cx, cy = p1.x(), p1.y()
            else:
                t  = max(0.0, min(1.0, ((click_pt.x()-p1.x())*dx + (click_pt.y()-p1.y())*dy) / seg_sq))
                cx = p1.x() + t * dx
                cy = p1.y() + t * dy
            d = math.hypot(click_pt.x() - cx, click_pt.y() - cy)
            if d < best_d:
                best_i, best_d = i, d
        return best_i

    def _nearest_segment_geom(self, feat, map_pt):
        pts = feat.geometry().asPolyline()
        if len(pts) < 2:
            return feat.geometry()
        i = self._nearest_segment(pts, map_pt)
        return QgsGeometry.fromPolylineXY([pts[i], pts[i + 1]])

    def _sub_line(self, geom, d_from, d_to):
        if d_to - d_from < 1e-10:
            return None
        pts = []
        s = geom.interpolate(d_from)
        if not s.isEmpty():
            p = s.asPoint()
            pts.append(QgsPointXY(p.x(), p.y()))
        verts = geom.asPolyline()
        cum = 0.0
        for i, v in enumerate(verts):
            if i > 0:
                cum += verts[i-1].distance(v)
            if d_from < cum < d_to:
                pts.append(v)
        e = geom.interpolate(d_to)
        if not e.isEmpty():
            p = e.asPoint()
            pts.append(QgsPointXY(p.x(), p.y()))
        if len(pts) >= 2:
            return QgsGeometry.fromPolylineXY(pts)
        return None

    def _split_at_chamfer(self, geom, click_pt, cut_dist):
        pts   = geom.asPolyline()
        total = geom.length()
        start = pts[0]
        end   = pts[-1]
        d_s   = math.hypot(start.x() - click_pt.x(), start.y() - click_pt.y())
        d_e   = math.hypot(end.x()   - click_pt.x(), end.y()   - click_pt.y())
        if d_s <= d_e:
            keep_from = cut_dist
            keep_to   = total
            chamfer_d = cut_dist
        else:
            keep_from = 0.0
            keep_to   = total - cut_dist
            chamfer_d = total - cut_dist
        keep_from = max(0.0, keep_from)
        keep_to   = min(total, keep_to)
        if keep_to - keep_from < 1e-10:
            return None, None
        cp = geom.interpolate(chamfer_d)
        if cp.isEmpty():
            return None, None
        p = cp.asPoint()
        chamfer_pt = QgsPointXY(p.x(), p.y())
        kept = self._sub_line(geom, keep_from, keep_to)
        return kept, chamfer_pt

    def _update_polyline_attrs(self, lyr, fid, geom):
        attrs = polyline_attrs(geom)
        for fname, val in attrs.items():
            idx = lyr.fields().indexOf(fname)
            if idx >= 0:
                lyr.changeAttributeValue(fid, idx, val)

    def _apply_chamfer_same_line(self, lyr, feat, click1, click2):
        geom = feat.geometry()
        pts  = geom.asPolyline()
        n    = len(pts)
        if n < 3:
            self._log("  Need at least 3 vertices to chamfer a corner on the same polyline", "#ffaaaa")
            self._reset_to_line1()
            return
        is_closed = (n >= 4
                     and abs(pts[0].x() - pts[-1].x()) < 1e-9
                     and abs(pts[0].y() - pts[-1].y()) < 1e-9)
        seg1 = self._nearest_segment(pts, click1)
        seg2 = self._nearest_segment(pts, click2)

        def _corner_end(seg_i, click):
            d0 = math.hypot(pts[seg_i].x()   - click.x(), pts[seg_i].y()   - click.y())
            d1 = math.hypot(pts[seg_i+1].x() - click.x(), pts[seg_i+1].y() - click.y())
            return seg_i if d0 <= d1 else seg_i + 1

        c1 = _corner_end(seg1, click1)
        c2 = _corner_end(seg2, click2)
        if c1 == c2:
            corner_idx = c1
        elif abs(seg1 - seg2) == 1:
            corner_idx = max(seg1, seg2)
        elif is_closed and {seg1, seg2} == {0, n - 2}:
            corner_idx = 0
        else:
            self._log("  Segments are not adjacent — click segments that share the corner", "#ffaaaa")
            self._reset_to_line1()
            return
        if is_closed and corner_idx == n - 1:
            corner_idx = 0
        if not is_closed and corner_idx in (0, n - 1):
            self._log("  Cannot chamfer the endpoint of an open polyline", "#ffaaaa")
            self._reset_to_line1()
            return

        vcorner = pts[corner_idx]
        if is_closed:
            unique_n = n - 1
            vprev = pts[(corner_idx - 1) % unique_n]
            vnext = pts[(corner_idx + 1) % unique_n]
        else:
            vprev = pts[corner_idx - 1]
            vnext = pts[corner_idx + 1]

        seg_out = corner_idx
        if seg1 == seg_out:
            dist_in, dist_out = self._dist2, self._dist1
        else:
            dist_in, dist_out = self._dist1, self._dist2

        len_in  = math.hypot(vcorner.x() - vprev.x(), vcorner.y() - vprev.y())
        len_out = math.hypot(vnext.x()   - vcorner.x(), vnext.y() - vcorner.y())
        if len_in < 1e-10 or len_out < 1e-10:
            self._log("  Degenerate segment at corner — cannot chamfer", "#ffaaaa")
            self._reset_to_line1()
            return
        if dist_in >= len_in:
            self._log(f"  d1={dist_in:.3f} exceeds incoming segment length {len_in:.3f}", "#ffaaaa")
            self._reset_to_line1()
            return
        if dist_out >= len_out:
            self._log(f"  d2={dist_out:.3f} exceeds outgoing segment length {len_out:.3f}", "#ffaaaa")
            self._reset_to_line1()
            return

        t_in  = 1.0 - dist_in  / len_in
        t_out = dist_out / len_out
        p1 = QgsPointXY(vprev.x()   + t_in  * (vcorner.x() - vprev.x()),
                        vprev.y()   + t_in  * (vcorner.y() - vprev.y()))
        p2 = QgsPointXY(vcorner.x() + t_out * (vnext.x()   - vcorner.x()),
                        vcorner.y() + t_out * (vnext.y()   - vcorner.y()))

        new_pts = list(pts)
        if is_closed and corner_idx == 0:
            inner   = new_pts[1:-1]
            new_pts = [p1, p2] + inner + [p1]
        else:
            new_pts[corner_idx:corner_idx + 1] = [p1, p2]

        new_geom = QgsGeometry.fromPolylineXY(new_pts)
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.changeGeometry(feat.id(), new_geom)
        self._update_polyline_attrs(lyr, feat.id(), new_geom)
        lyr.triggerRepaint()
        self._log(
            f"  Chamfered corner at vertex {corner_idx} on '{lyr.name()}'  "
            f"d1={dist_in:.3f}  d2={dist_out:.3f}", "#88ff88"
        )
        self._reset_to_line1()

    def _apply_chamfer(self, lyr2, feat2, click2):
        lyr1  = self._line1_layer
        feat1 = self._line1_feat
        if lyr1 is lyr2 and feat1.id() == feat2.id():
            self._apply_chamfer_same_line(lyr1, feat1, self._line1_click, click2)
            return

        g1 = feat1.geometry()
        g2 = feat2.geometry()
        if g1.isMultipart() or g2.isMultipart():
            self._log("  Multipart geometry — chamfer not supported", "#ffaaaa")
            self._reset_to_line1()
            return

        kept1, e1 = self._split_at_chamfer(g1, self._line1_click, self._dist1)
        kept2, e2 = self._split_at_chamfer(g2, click2,             self._dist2)

        if kept1 is None or kept2 is None:
            self._log("  Chamfer distance too large — try a smaller value", "#ffaaaa")
            self._reset_to_line1()
            return

        if not lyr1.isEditable():
            lyr1.startEditing()
        if not lyr2.isEditable():
            lyr2.startEditing()

        lyr1.changeGeometry(feat1.id(), kept1)
        lyr2.changeGeometry(feat2.id(), kept2)
        self._update_polyline_attrs(lyr1, feat1.id(), kept1)
        self._update_polyline_attrs(lyr2, feat2.id(), kept2)

        if self._dist1 > 1e-10 or self._dist2 > 1e-10:
            chamfer_geom = QgsGeometry.fromPolylineXY([e1, e2])
            nf = QgsFeature(lyr1.fields())
            nf.setGeometry(chamfer_geom)
            nf.setAttributes(feat1.attributes())
            ch_attrs = polyline_attrs(chamfer_geom)
            for fname, val in ch_attrs.items():
                idx = lyr1.fields().indexOf(fname)
                if idx >= 0:
                    nf.setAttribute(idx, val)
            lyr1.addFeature(nf)

        lyr1.triggerRepaint()
        lyr2.triggerRepaint()
        self._log(
            f"  Chamfered  d1={self._dist1:.3f}  d2={self._dist2:.3f}"
            f"  '{lyr1.name()}' + '{lyr2.name()}'", "#88ff88"
        )
        self._reset_to_line1()

    def _reset_to_line1(self):
        self._line1_layer = None
        self._line1_feat  = None
        self._line1_click = None
        self._line1_band.setVisible(False)
        self._state = _ST_LINE1
        self._log(f"  Click first line  (d1={self._dist1:.3f}  d2={self._dist2:.3f})")

    def _dispatch(self, sem):
        if sem.type == EventType.VALUE_ENTERED and sem.value is not None:
            d = abs(float(sem.value))
            self._dist1 = d
            self._dist2 = d
            self._log(f"  Distances set: d1={d:.3f}  d2={d:.3f}  —  click first line", "#88ccff")
        elif sem.type == EventType.COORDINATE_ENTERED and sem.point is not None:
            d1 = abs(float(sem.point.x()))
            d2 = abs(float(sem.point.y()))
            self._dist1 = d1
            self._dist2 = d2
            self._log(f"  Distances set: d1={d1:.3f}  d2={d2:.3f}  —  click first line", "#88ccff")
        if self._state == _ST_LINE2:
            self._reset_to_line1()

    def activate(self):
        super().activate()
        self._canvas.setFocus()
        self._log(
            "CHAMFER  ──  type d1 or d1,d2, then click two lines at their corner ends"
            "  (d1=d2=0 → sharp corner)", "#aaddff"
        )
        self._log(f"  Current distances: d1={self._dist1:.3f}  d2={self._dist2:.3f}")

    def deactivate(self):
        self._rm(self._line1_band)
        self._rm(self._hover_band)
        self._hint.hide()
        self._state       = _ST_LINE1
        self._line1_layer = None
        self._line1_feat  = None
        self._line1_click = None
        self._hover_lyr_id = None
        self._hover_fid    = None
        self._hover_seg    = None
        super().deactivate()

    def canvasMoveEvent(self, event):
        map_pt = self.toMapCoordinates(event.pos())
        if self._state in (_ST_LINE1, _ST_LINE2):
            lyr, feat = self._find_line_near(map_pt)
            if feat:
                pts   = feat.geometry().asPolyline()
                seg_i = self._nearest_segment(pts, map_pt) if len(pts) >= 2 else 0
                lyr_id = lyr.id() if lyr else None
                if (lyr_id != self._hover_lyr_id
                        or feat.id() != self._hover_fid
                        or seg_i != self._hover_seg):
                    self._hover_lyr_id = lyr_id
                    self._hover_fid    = feat.id()
                    self._hover_seg    = seg_i
                    seg_geom = QgsGeometry.fromPolylineXY([pts[seg_i], pts[seg_i + 1]])
                    self._hover_band.setToGeometry(seg_geom, lyr)
                self._hover_band.setVisible(True)
            else:
                self._hover_lyr_id = None
                self._hover_fid    = None
                self._hover_seg    = None
                self._hover_band.setVisible(False)
        self._show_hint(event.pos())

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            if self._state == _ST_LINE2:
                self._reset_to_line1()
            else:
                self._go_home()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        map_pt = self.toMapCoordinates(event.pos())

        if self._state == _ST_LINE1:
            lyr, feat = self._find_line_near(map_pt)
            if feat is None:
                self._log("  No line found near click")
                return
            if feat.geometry().isMultipart():
                self._log("  Multipart geometry — chamfer not supported", "#ffaaaa")
                return
            self._line1_layer = lyr
            self._line1_feat  = feat
            self._line1_click = map_pt
            self._line1_band.setToGeometry(self._nearest_segment_geom(feat, map_pt), lyr)
            self._line1_band.setVisible(True)
            self._state = _ST_LINE2
            self._log(f"  First line: '{lyr.name()}' fid {feat.id()}  →  click second line")
        elif self._state == _ST_LINE2:
            lyr, feat = self._find_line_near(map_pt)
            if feat is None:
                self._log("  No line found near click")
                return
            self._apply_chamfer(lyr, feat, map_pt)

        self._canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._go_home()
