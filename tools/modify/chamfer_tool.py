# -*- coding: utf-8 -*-
"""
AutoCAD-style CHAMFER tool.

Workflow
────────
Activate → type d1 or d1,d2 + Enter to set distances (defaults 2, 2).
Click first segment near its corner end → highlighted green.
Click second segment (touching the first at that corner) → chamfer applied:
  • Same-feature: replaces the shared corner vertex with two new points.
  • Two features: trims both lines and adds a straight chamfer segment.
RMB after first segment → reset.  RMB with nothing selected → exit.
Esc → exit.

d1=d2=0 → sharp corner (trim both lines to intersection point).
"""

import math

from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ...core.base_tool import BaseTool, ToolState
from ...core.events import SemanticEvent, EventType
from ...core import style as _style
from ..layer_utils import polyline_attrs
from ...core.circle_utils import is_circle

_HIT_PX = 10       # pixel hit tolerance for segment picking
_ST_LINE1 = 0      # waiting for first segment
_ST_LINE2 = 1      # first segment selected, waiting for second


class ChamferTool(BaseTool):
    """AutoCAD-style CHAMFER — bevel two touching segments with a straight line."""

    def __init__(self, canvas, ctx, translator):
        super().__init__(canvas, ctx, translator)
        self._dist1 = 2.0
        self._dist2 = 2.0
        self._state_ch = _ST_LINE1

        self._line1_layer = None
        self._line1_feat  = None
        self._line1_click = None
        self._line1_seg   = None   # segment index on feature for duplicate-pick guard

        self._hover_lyr_id = None
        self._hover_fid    = None
        self._hover_seg    = None

        # Created on activate(), nulled on _on_cancel_hook()
        self._line1_band = None
        self._hover_band = None

    # ── helpers ───────────────────────────────────────────────────────────

    def _log(self, msg, color="#cccccc"):
        dock = getattr(self._ctx, 'cmd_dock', None)
        if dock:
            dock.log(msg, color)

    def _hit_tol(self):
        return _HIT_PX * self.canvas().mapUnitsPerPixel()

    def _corner_tol(self):
        return self._hit_tol() * 3

    def _prompt(self):
        if self._state_ch == _ST_LINE1:
            return "Click first segment:"
        return "Click second touching segment:"

    def _sync_live_distances(self):
        """Push current d1/d2 into the widget's live placeholder values."""
        dyn = getattr(self._ctx, 'dyn_widget', None)
        if dyn:
            dyn.set_live_polar(self._dist1, self._dist2)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def activate(self):
        super().activate()
        self._transition(ToolState.ACTING)
        self._state_ch     = _ST_LINE1
        self._line1_layer  = None
        self._line1_feat   = None
        self._line1_click  = None
        self._line1_seg    = None
        self._hover_lyr_id = None
        self._hover_fid    = None
        self._hover_seg    = None
        self._line1_band = self._new_rubber_band(
            QgsWkbTypes.GeometryType.LineGeometry, _style.RB_SOURCE
        )
        self._hover_band = self._new_rubber_band(
            QgsWkbTypes.GeometryType.LineGeometry, _style.RB_HOVER
        )
        self._line1_band.setVisible(False)
        self._hover_band.setVisible(False)
        self._sync_live_distances()   # set live values before rebuild so they show immediately
        self._request_input("d1d2", self._prompt())
        self._log("CHAMFER  ──  enter d1, d2 then click two touching segments", "#aaddff")
        self._log(f"  d1={self._dist1:.3f}  d2={self._dist2:.3f}")

    def _on_cancel_hook(self):
        self._line1_band  = None
        self._hover_band  = None
        self._line1_layer = None
        self._line1_feat  = None
        self._line1_click = None
        self._line1_seg   = None
        self._state_ch    = _ST_LINE1

    def _reset_to_line1(self):
        self._line1_layer = None
        self._line1_feat  = None
        self._line1_click = None
        self._line1_seg   = None
        if self._line1_band is not None:
            self._line1_band.setVisible(False)
        self._state_ch = _ST_LINE1
        self._sync_live_distances()   # set live values before rebuild
        self._request_input("d1d2", self._prompt())

    # ── geometry utilities ────────────────────────────────────────────────

    def _line_layers(self):
        return [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.isSpatial()
            and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.GeometryType.LineGeometry
        ]

    def _find_line_near(self, map_pt):
        tol  = self._hit_tol()
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol,
            map_pt.x() + tol, map_pt.y() + tol,
        )
        cg = QgsGeometry.fromPointXY(map_pt)
        best_layer, best_feat, best_d = None, None, float('inf')
        for lyr in self._line_layers():
            for feat in lyr.getFeatures(rect):
                geom = feat.geometry()
                if geom.isEmpty() or is_circle(geom):
                    continue
                d = geom.distance(cg)
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
                t = max(0.0, min(1.0,
                    ((click_pt.x() - p1.x()) * dx + (click_pt.y() - p1.y()) * dy) / seg_sq
                ))
                cx = p1.x() + t * dx
                cy = p1.y() + t * dy
            d = math.hypot(click_pt.x() - cx, click_pt.y() - cy)
            if d < best_d:
                best_i, best_d = i, d
        return best_i

    def _feature_corner_end(self, geom, click_pt):
        """Return the start or end vertex of geom that is closest to click_pt."""
        pts = geom.asPolyline()
        d_s = math.hypot(pts[0].x()  - click_pt.x(), pts[0].y()  - click_pt.y())
        d_e = math.hypot(pts[-1].x() - click_pt.x(), pts[-1].y() - click_pt.y())
        return pts[0] if d_s <= d_e else pts[-1]

    def _features_touch(self, g1, click1, g2, click2):
        """True if the chamfer-corner ends of two features are coincident."""
        cp1 = self._feature_corner_end(g1, click1)
        cp2 = self._feature_corner_end(g2, click2)
        return math.hypot(cp1.x() - cp2.x(), cp1.y() - cp2.y()) < self._corner_tol()

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
                cum += verts[i - 1].distance(v)
            if d_from < cum < d_to:
                pts.append(v)
        e = geom.interpolate(d_to)
        if not e.isEmpty():
            p = e.asPoint()
            pts.append(QgsPointXY(p.x(), p.y()))
        return QgsGeometry.fromPolylineXY(pts) if len(pts) >= 2 else None

    def _split_at_chamfer(self, geom, click_pt, cut_dist):
        pts   = geom.asPolyline()
        total = geom.length()
        d_s   = math.hypot(pts[0].x()  - click_pt.x(), pts[0].y()  - click_pt.y())
        d_e   = math.hypot(pts[-1].x() - click_pt.x(), pts[-1].y() - click_pt.y())
        if d_s <= d_e:
            keep_from, keep_to, chamfer_d = cut_dist, total, cut_dist
        else:
            keep_from, keep_to, chamfer_d = 0.0, total - cut_dist, total - cut_dist
        keep_from = max(0.0, keep_from)
        keep_to   = min(total, keep_to)
        if keep_to - keep_from < 1e-10:
            return None, None
        cp = geom.interpolate(chamfer_d)
        if cp.isEmpty():
            return None, None
        p = cp.asPoint()
        return self._sub_line(geom, keep_from, keep_to), QgsPointXY(p.x(), p.y())

    def _update_polyline_attrs(self, lyr, fid, geom):
        change = {
            lyr.fields().indexOf(fname): val
            for fname, val in polyline_attrs(geom).items()
            if lyr.fields().indexOf(fname) >= 0
        }
        if change:
            lyr.changeAttributeValues(fid, change)

    # ── chamfer apply ─────────────────────────────────────────────────────

    def _apply_chamfer_same_line(self, lyr, feat, click1, click2):  # noqa: C901
        geom = feat.geometry()
        pts  = geom.asPolyline()
        n    = len(pts)
        if n < 3:
            self._log("  Need ≥3 vertices to chamfer a corner on the same polyline", "#ffaaaa")
            self._reset_to_line1()
            return

        is_closed = (n >= 4
                     and abs(pts[0].x() - pts[-1].x()) < 1e-9
                     and abs(pts[0].y() - pts[-1].y()) < 1e-9)
        seg1 = self._nearest_segment(pts, click1)
        seg2 = self._nearest_segment(pts, click2)

        if seg1 == seg2:
            self._log("  Same segment selected twice — click an adjacent segment", "#ffaaaa")
            self._reset_to_line1()
            return

        def _corner_idx(seg_i, click):
            d0 = math.hypot(pts[seg_i].x()   - click.x(), pts[seg_i].y()   - click.y())
            d1 = math.hypot(pts[seg_i+1].x() - click.x(), pts[seg_i+1].y() - click.y())
            return seg_i if d0 <= d1 else seg_i + 1

        c1 = _corner_idx(seg1, click1)
        c2 = _corner_idx(seg2, click2)

        if c1 == c2:
            corner_idx = c1
        elif abs(seg1 - seg2) == 1:
            corner_idx = max(seg1, seg2)
        elif is_closed and {seg1, seg2} == {0, n - 2}:
            corner_idx = 0
        else:
            self._log("  Segments are not adjacent — click segments that share a corner", "#ffaaaa")
            self._reset_to_line1()
            return

        if is_closed and corner_idx == n - 1:
            corner_idx = 0
        if not is_closed and corner_idx in (0, n - 1):
            self._log("  Cannot chamfer an open endpoint", "#ffaaaa")
            self._reset_to_line1()
            return

        vcorner = pts[corner_idx]
        if is_closed:
            un = n - 1
            vprev = pts[(corner_idx - 1) % un]
            vnext = pts[(corner_idx + 1) % un]
        else:
            vprev = pts[corner_idx - 1]
            vnext = pts[corner_idx + 1]

        dist_in  = self._dist2 if seg1 == corner_idx else self._dist1
        dist_out = self._dist1 if seg1 == corner_idx else self._dist2

        len_in  = math.hypot(vcorner.x() - vprev.x(), vcorner.y() - vprev.y())
        len_out = math.hypot(vnext.x() - vcorner.x(), vnext.y() - vcorner.y())

        if len_in < 1e-10 or len_out < 1e-10:
            self._log("  Degenerate segment — cannot chamfer", "#ffaaaa")
            self._reset_to_line1()
            return
        if dist_in >= len_in:
            self._log(f"  d={dist_in:.3f} exceeds incoming segment {len_in:.3f}", "#ffaaaa")
            self._reset_to_line1()
            return
        if dist_out >= len_out:
            self._log(f"  d={dist_out:.3f} exceeds outgoing segment {len_out:.3f}", "#ffaaaa")
            self._reset_to_line1()
            return

        t_in  = 1.0 - dist_in  / len_in
        t_out = dist_out / len_out
        p1 = QgsPointXY(
            vprev.x() + t_in  * (vcorner.x() - vprev.x()),
            vprev.y() + t_in  * (vcorner.y() - vprev.y()),
        )
        p2 = QgsPointXY(
            vcorner.x() + t_out * (vnext.x() - vcorner.x()),
            vcorner.y() + t_out * (vnext.y() - vcorner.y()),
        )

        new_pts = list(pts)
        if is_closed and corner_idx == 0:
            new_pts = [p1, p2] + new_pts[1:-1] + [p1]
        else:
            new_pts[corner_idx:corner_idx + 1] = [p1, p2]

        new_geom = QgsGeometry.fromPolylineXY(new_pts)
        if not lyr.isEditable():
            lyr.startEditing()
        hist = getattr(self._ctx, 'action_history', None)
        if hist:
            hist.begin_group()
        lyr.changeGeometry(feat.id(), new_geom)
        if hist:
            hist.record_step(lyr.id())
        self._update_polyline_attrs(lyr, feat.id(), new_geom)
        if hist:
            hist.record_step(lyr.id())
        if hist:
            hist.end_group()
        lyr.triggerRepaint()
        self._log(
            f"  Chamfered corner {corner_idx} on '{lyr.name()}'  "
            f"d1={dist_in:.3f} d2={dist_out:.3f}", "#88ff88"
        )
        self._reset_to_line1()

    def _apply_chamfer(self, lyr2, feat2, click2):  # noqa: C901
        lyr1, feat1 = self._line1_layer, self._line1_feat

        if lyr1 is lyr2 and feat1.id() == feat2.id():
            self._apply_chamfer_same_line(lyr1, feat1, self._line1_click, click2)
            return

        g1, g2 = feat1.geometry(), feat2.geometry()

        if g1.isMultipart() or g2.isMultipart():
            self._log("  Multipart geometry — chamfer not supported", "#ffaaaa")
            self._reset_to_line1()
            return

        if not self._features_touch(g1, self._line1_click, g2, click2):
            self._log(
                "  Segments do not share a corner — "
                "click two segments that meet at a common endpoint", "#ffaaaa"
            )
            self._reset_to_line1()
            return

        kept1, e1 = self._split_at_chamfer(g1, self._line1_click, self._dist1)
        kept2, e2 = self._split_at_chamfer(g2, click2,             self._dist2)

        if kept1 is None or kept2 is None:
            self._log("  Chamfer distance too large — use a smaller value", "#ffaaaa")
            self._reset_to_line1()
            return

        if not lyr1.isEditable():
            lyr1.startEditing()
        if not lyr2.isEditable():
            lyr2.startEditing()

        hist = getattr(self._ctx, 'action_history', None)
        if hist:
            hist.begin_group()
        lyr1.changeGeometry(feat1.id(), kept1)
        if hist:
            hist.record_step(lyr1.id())
        lyr2.changeGeometry(feat2.id(), kept2)
        if hist:
            hist.record_step(lyr2.id())
        self._update_polyline_attrs(lyr1, feat1.id(), kept1)
        if hist:
            hist.record_step(lyr1.id())
        self._update_polyline_attrs(lyr2, feat2.id(), kept2)
        if hist:
            hist.record_step(lyr2.id())

        if self._dist1 > 1e-10 or self._dist2 > 1e-10:
            chamfer_geom = QgsGeometry.fromPolylineXY([e1, e2])
            nf = QgsFeature(lyr1.fields())
            nf.setGeometry(chamfer_geom)
            nf.setAttributes(feat1.attributes())
            for fname, val in polyline_attrs(chamfer_geom).items():
                idx = lyr1.fields().indexOf(fname)
                if idx >= 0:
                    nf.setAttribute(idx, val)
            lyr1.addFeature(nf)
            if hist:
                hist.record_step(lyr1.id())

        if hist:
            hist.end_group()
        lyr1.triggerRepaint()
        lyr2.triggerRepaint()
        self._log(
            f"  Chamfered  d1={self._dist1:.3f} d2={self._dist2:.3f}"
            f"  '{lyr1.name()}' + '{lyr2.name()}'", "#88ff88"
        )
        self._reset_to_line1()

    # ── BaseTool event interface ───────────────────────────────────────────

    def _on_event(self, sem: SemanticEvent):
        if sem.type == EventType.VALUE_ENTERED and sem.value is not None:
            try:
                d = abs(float(sem.value))
                self._dist1 = d
                self._dist2 = d
                self._log(f"  d1=d2={d:.3f}", "#88ccff")
            except (TypeError, ValueError):  # nosec B110
                pass
            # Update prompt and placeholders without resetting segment picks
            self._update_prompt(self._prompt())
            self._sync_live_distances()
            return

        if sem.type == EventType.COORDINATE_ENTERED and sem.point is not None:
            # Fired by the d1d2 widget (Tab → Enter) or by typing "d1,d2" in command line
            d1 = abs(float(sem.point.x()))
            d2 = abs(float(sem.point.y()))
            self._dist1, self._dist2 = d1, d2
            self._log(f"  d1={d1:.3f}  d2={d2:.3f}", "#88ccff")
            # Update prompt and placeholders without resetting segment picks
            self._update_prompt(self._prompt())
            self._sync_live_distances()
            return

        if sem.type == EventType.CONFIRM:
            if self._state_ch == _ST_LINE2:
                self._reset_to_line1()
            else:
                self._go_home()
            return

        if sem.type == EventType.POINT_PICKED and sem.point is not None:
            self._handle_click(sem.point)

    def _handle_click(self, map_pt):
        if self._state_ch == _ST_LINE1:
            lyr, feat = self._find_line_near(map_pt)
            if feat is None:
                self._log("  No line near click")
                return
            if feat.geometry().isMultipart():
                self._log("  Multipart geometry — not supported", "#ffaaaa")
                return
            pts   = feat.geometry().asPolyline()
            seg_i = self._nearest_segment(pts, map_pt)
            self._line1_layer = lyr
            self._line1_feat  = feat
            self._line1_click = map_pt
            self._line1_seg   = seg_i
            seg_geom = QgsGeometry.fromPolylineXY([pts[seg_i], pts[seg_i + 1]])
            self._line1_band.setToGeometry(seg_geom, lyr)
            self._line1_band.setVisible(True)
            self._state_ch = _ST_LINE2
            self._sync_live_distances()   # set live values before rebuild
            self._request_input("d1d2", self._prompt())
            self._log(f"  First segment on '{lyr.name()}'  →  click second touching segment")

        elif self._state_ch == _ST_LINE2:
            lyr, feat = self._find_line_near(map_pt)
            if feat is None:
                self._log("  No line near click")
                return
            # Prevent double-selection of the same segment
            if lyr is self._line1_layer and feat.id() == self._line1_feat.id():
                pts   = feat.geometry().asPolyline()
                seg_i = self._nearest_segment(pts, map_pt)
                if seg_i == self._line1_seg:
                    self._log(
                        "  Same segment selected twice — click an adjacent or touching segment",
                        "#ffaaaa"
                    )
                    return
            self._apply_chamfer(lyr, feat, map_pt)

    def _on_hover(self, sem: SemanticEvent):
        if not self._hover_band:
            return
        # Prefer raw mouse position so the segment highlight tracks the actual cursor
        raw = getattr(sem, 'raw', None)
        if raw is not None and hasattr(raw, 'pos'):
            map_pt = self.toMapCoordinates(raw.pos())
        elif sem.point is not None:
            map_pt = sem.point
        else:
            self._hover_band.setVisible(False)
            return

        lyr, feat = self._find_line_near(map_pt)
        if feat:
            pts    = feat.geometry().asPolyline()
            seg_i  = self._nearest_segment(pts, map_pt) if len(pts) >= 2 else 0
            lyr_id = lyr.id() if lyr else None

            # Don't highlight the already-selected first segment
            if (self._state_ch == _ST_LINE2
                    and lyr is self._line1_layer
                    and feat.id() == self._line1_feat.id()
                    and seg_i == self._line1_seg):
                self._hover_band.setVisible(False)
                return

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
