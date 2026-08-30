# UgSurv Plugin — Development Guide

## What this plugin is

A CAD-style editing plugin for QGIS. It replaces the default QGIS editing tools with a faster, more direct workflow for survey/drafting work.

---

## Layers

All plugin-managed layers are prefixed with `_`:

| Layer | Type | Purpose |
|-------|------|---------|
| `_polylines` | LineString | Main drafting lines |
| `_circles` | CircularString | Circles (stored as 5-point arc) |
| `_points` | Point | Survey points |
| `_dimensions` | LineString | Dimension annotations |

Working data lives at `C:\Users\Nuwe Surv\Desktop\Ugsurv_features`.

---

## The editing tool (`vertex_selector.py`)

This is the permanent default map tool. It handles everything: feature selection, vertex gripping, moving, deleting, extending lines.

**State machine:**

```
IDLE → [click edge] → FEATURE → [click vertex] → GRIPPED → [click grip] → MOVING
                                                     ↑                        |
                                                     └──── [click to commit] ─┘
```

**How all editing operations work:**

Every operation follows the same pattern:

1. **Load into memory** — on feature/vertex selection, copy the geometry into a Python variable and store the link `(layer, fid)` to the real feature. No scratch layers, no temp files.
2. **Manipulate the variable** — all edits (move, trim, offset, etc.) modify only the in-memory geometry.
3. **Display with rubber band** — rubber bands show the current state of the in-memory geometry. They are temporary Qt scene items, not data.
4. **Commit to layer** — on commit, call `layer.changeGeometry(fid, final_geom)`. Only this step touches the actual data.

**Commit triggers** (in any operation that has a live preview):

| Action | Effect |
|--------|--------|
| Right-click | Commit — apply geometry to feature |
| Enter | Commit |
| Space | Commit |
| Escape | Cancel — discard in-memory geometry, restore rubber band |

**The golden rule for mouse-move handlers:**
> Only update the rubber band. Nothing else.

- Cache geometry when entering a state — never call `getFeature()` inside a move handler
- Use `rubberBand.movePoint(vidx, raw_pt)` — O(1), moves one point
- Use `raw_pt` (raw cursor) during preview — snap runs once on commit only
- `_moving_geom` holds the cached geometry for the entire move session

**Snap** (`snap_utils.py`):
- Priority: endpoint → circle center → midpoint → intersection → nearest
- Full snap on commit only — never in the mouse-move hot path

---

## Key files

| File | Role |
|------|------|
| `Ugsurv.py` | Plugin entry point, toolbar, command dispatch |
| `modules/vertex_selector.py` | Default map tool — feature select + vertex edit |
| `modules/snap_utils.py` | All snap logic |
| `modules/maptool.py` | Tool switcher — handles draw tools evicting the default tool |
| `modules/terminal.py` | Command terminal dock |
| `modules/topology_solver.py` | Fixes node mismatches between features |
| `module_wz_dialogs/` | Dialog-backed commands (topology, file import, etc.) |

---

## Performance rules

1. **No `getFeature()` in move handlers.** Cache on state entry.
2. **No snap in move handlers.** Snap on commit only.
3. **Use `QgsPointLocator`** for vertex lookup — it's an R-tree, not a full scan.
4. **Spatial filter** every `getFeatures()` call with a `QgsRectangle` — never iterate the whole layer.
5. **`movePoint()`** not `setToGeometry()` during drag — one point update vs. full geometry rebuild.

---

## Adding a new tool

1. Create `modules/my_tool.py` — subclass `QgsMapTool`
2. Register it in `Ugsurv.py` via `UgsurvMaptool.set_tool()`
3. When done, call `self._maptool.clear_tool()` to return to the default tool
4. If it needs snap, import `snap_utils` and call `snap_utils.snap_point(canvas, raw_pt, active_layer)`

---

## Adding a new command

1. Add the command keyword to the terminal dispatch in `modules/terminal.py`
2. If it needs a dialog, add it under `module_wz_dialogs/`
3. Keep commands stateless — they read current layer state, do the operation, done
