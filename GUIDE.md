# QGIS CAD Plugin — Build Specification

## 1. Purpose

Build a QGIS plugin that recreates a 2D CAD drafting environment (AutoCAD-like) inside QGIS, respecting QGIS's topological, layer-based, and non-destructive editing model. All geometry created by drafting tools is stored as real vector features in a project-local GeoPackage, edited through QGIS's native edit-buffer/undo/save mechanism — never through custom serialization, and never auto-saved by any tool.

**Hard constraints — do not violate these anywhere in the implementation:**
- No tool ever calls `layer.commitChanges()` (disk save). Tools only ever write into the edit buffer (`startEditing()`, `changeGeometry()`, `addFeature()`, etc.). Saving to disk is a user-triggered QGIS-native action, entirely outside plugin control.
- No forced geometry validation, auto-clipping, auto-snap-to-avoid-intersections, or silent geometry correction, ever, by default. Any validation logic must be an explicit, opt-in, user-triggered check — never automatic behavior injected into a tool's commit path.
- No snapping is active unless the user has explicitly enabled it. All snap providers default to off.
- Drafting tools are disabled (toolbar/menu greyed out, not just rejected on click) until the QGIS project has been saved at least once, since the geometries GeoPackage must sit next to the project file. See §5.

---

## 2. Core Architecture — Tool State Machine

Every drafting/modify/vertex tool is a `BaseTool` implementing the same lifecycle:

```
IDLE → SELECTING → ACTING → PENDING_CONFIRM → (commit | cancel) → IDLE
```

- **IDLE** — no tool armed, or tool armed but waiting for first input. Esc here just clears stray selection.
- **SELECTING** — collecting an object/vertex selection set (not all tools use this phase — pure creation tools skip straight to ACTING).
- **ACTING** — the tool's live operation: point placement, dragging, live preview via rubber-band. May loop internally (e.g. polyline collecting N points).
- **PENDING_CONFIRM** — an explicit gate before commit, used when the tool needs a final confirmation distinct from the last data point (not all tools need this stage — some commit immediately on the last required click).
- **commit** — finalize into the edit buffer of the appropriate GeoPackage layer, push an undo command, return to IDLE.
- **cancel** — discard the in-progress `PendingAction`, return to IDLE, no trace left in the buffer.

**Interruption model — two distinct kinds, do not conflate them:**
- **Modal interrupt** (pan, zoom, osnap override) — suspend the current tool's state, run the interrupting action, resume exactly where it left off. Nothing is lost.
- **Command interrupt** (user starts a different drafting command mid-operation) — abandon the current `PendingAction` entirely (no silent resume), then activate the new tool. This matches AutoCAD's actual default behavior — do not build resumable multi-level tool stacking beyond the modal case, it adds complexity with no real payoff.

`ToolManager` owns: the active tool, a small suspend stack (modal interrupts only), and dispatches activation/deactivation.

---

## 3. Core Objects (`core/`)

| Object | Responsibility |
|---|---|
| `ToolManager` | Owns active tool; handles modal-suspend/resume and command-interrupt/abandon; single entry point for tool activation from toolbar, command line, or keyboard shortcut. |
| `BaseTool` (abstract) | Defines `activate()`, `deactivate()`, `suspend()`, `resume()`, and the internal IDLE→...→IDLE state machine. All tools in §7 subclass this. |
| `SelectionModel` | Selection is independent of any tool — persists across tool switches where appropriate. Emits `selectionChanged`. Also the enforcement point for CAD-layer locking (see §6). |
| `PendingAction` | Represents one in-progress, uncommitted operation. Holds preview geometry, parameters, pre-action snapshot. Exposes only `commit()` / `cancel()`. |
| `ToolContext` | Shared read state passed into tools: active CAD-layer, active geometry-target layer, snapping/constraint settings, current sketch. |
| `InputTranslator` | Converts raw canvas mouse/key events plus dynamic-input entry into semantic events (`PointPicked`, `SnapHit`, `CoordinateEntered`, `Confirm`, `Cancel`) that tools consume — tools never parse raw Qt events directly. |
| `StorageManager` | Resolves/creates the geometries GeoPackage, gates drafting tools until the project is saved, exposes the three geometry tables + `cad_layers` table to the rest of the plugin. |
| `EntityFactory` | Given a `PendingAction`'s finished geometry, routes it to the correct GeoPackage table (points/lines/polygons) and stamps the active `cad_layer` value on commit. |
| `SnapEngine` | Queries all active `SnapProvider`s within tolerance on every mouse move, resolves candidates to the point actually used. |
| `CommandDispatcher` | Single entry point for typed command-line input, toolbar clicks, and keyboard shortcuts — all three route through here to `ToolManager.activateTool()`, never directly. |

---

## 4. Storage — Geometries GeoPackage

**File:** `<project_basename>_geometries.gpkg`, created in the same directory as the `.qgz`/`.qgs` project file, on first project save (see gating rule in §5).

**Tables:**
- `points` (MultiPoint)
- `lines` (MultiLineString)
- `polygons` (MultiPolygon)
- `cad_layers` (non-spatial) — columns: `name`, `color`, `linetype`, `visible` (bool), `locked` (bool), `sort_order` (int)

Every feature in `points`/`lines`/`polygons` carries a `cad_layer` attribute (foreign-key-by-name into `cad_layers`). This is the AutoCAD-style "layer" concept (color/linetype/visibility/lock group) — it is deliberately **not** a separate GeoPackage table, because a single CAD-layer must be able to contain a mix of points, lines, and polygons simultaneously, cutting across the three geometry tables.

Use Multi* geometry types on all three tables even for single-part entities, to avoid type-mismatch errors later.

**Commit path:** `PendingAction.commit()` → `EntityFactory` picks the target table by geometry type → `layer.startEditing()` if not already active → `changeGeometry()`/`addFeature()` → stamp `cad_layer` → push a `QgsVectorLayerUndoCommand` onto that **layer's own** `undoStack()` (do not build a separate undo stack for committed geometry — piggyback on QGIS's native per-layer undo so Ctrl+Z/Ctrl+Y and the Undo dock work transparently).

---

## 5. Project-Save Gating

- On plugin load and on every `ToolManager.activateTool()` attempt: check `QgsProject.instance().fileName()`. Empty string → project unsaved.
- If unsaved: disable (grey out) the entire drafting toolbar/menu, show a status-bar message: *"Save the project before drafting."* Do not let clicks reach `ToolManager` while disabled.
- Hook `QgsProject.instance().projectSaved`: on first fire after being unsaved, resolve the GeoPackage path via `absolutePath()` + `baseName()`, create it with the schema in §4, re-enable the toolbar.
- Hook `QgsProject.instance().cleared` / `readProject` (switching to a different project mid-session) to re-evaluate gating state.
- **Open question to resolve during build, not before:** behavior on "Save As" to a new location after drafting has already started (relocate the gpkg automatically vs. leave it referencing the old path vs. prompt each time). Implement as a user-facing prompt at Save-As time unless directed otherwise.

---

## 6. Vertex Editing — Topology-Aware

Vertex operations must branch on `layer.geometryType()`:
- **Point**: no separate "vertex" — the grip *is* the feature; dragging moves the feature directly.
- **Line**: standard insert/move/delete vertex; endpoints unconstrained.
- **Polygon**: first/last vertex of each ring must stay coincident; exterior ring and interior rings (holes) are separately editable rings within one feature.
- **Curved geometry** (if/when arcs are stored as true curves rather than densified lines): a "vertex" may carry a role — `linear` / `arc_start` / `arc_mid` / `arc_end` — not just a bare coordinate.

**Topological editing:** if `QgsProject.instance().topologicalEditing()` is on, a vertex-drag `PendingAction` must, before starting the drag, query for coincident vertices on neighboring features and include them in the same commit — this is the only place multi-feature edits happen implicitly, and it is driven entirely by the project's own topological-editing flag, never forced independently of it.

Implement grip-based editing (not separate named vertex tools) as the primary interaction, following AutoCAD's model:
- Selecting an object shows its vertices as grips automatically.
- Clicking-dragging a grip is itself a `PendingAction` (base point = the vertex, preview = adjacent segments reshaping live).
- Right-click on a hot grip opens a context menu for grip-relative Move/Rotate/Scale/Mirror.
- Add-vertex (click a segment to insert), remove-vertex (select grip, delete), Join, and Break are thin specializations of the same grip machinery, not separate `BaseTool` subclasses where avoidable.

---

## 7. Full Tool List (each is a `BaseTool` subclass; file per tool under `tools/`)

### Drawing (`tools/drawing/`)
| Tool | Lifecycle summary |
|---|---|
| Line | ACTING loops: click point → click point → ... → Enter/Esc ends chain. |
| Polyline | Same loop, single entity; mid-ACTING sub-modes (Arc/Width/Undo-last-vertex) via keypress without leaving state; close via "C". |
| Circle | Click center → live radius preview → click/type radius → commit. Support 2-point, 3-point, tangent-tangent-radius variants. |
| Arc | 3-point default (start, end, point-on-arc); center+angle variant collects different points, same shape. |
| Rectangle | Click corner 1 → live preview → click corner 2 → commit. |
| Polygon (regular) | Specify side count → click center → click/type radius → commit. |

### Modify (`tools/modify/`)
| Tool | Lifecycle summary |
|---|---|
| Move | SELECTING (Enter confirms) → click base point → live drag preview → click destination = commit. |
| Copy | Same as Move, original retained; supports multiple-copy loop until Esc/Enter. |
| Rotate | SELECTING → click base point → live angle preview → click/type angle → commit. |
| Scale | SELECTING → click base point → live scale preview → click/type factor → commit. |
| Trim / Extend | SELECTING boundary edges (Enter confirms) → click target segments one at a time, each click commits immediately, loop continues until Enter/Esc. |
| Offset | Type/click distance → click object → click side → commit; loops for repeated offsets until Esc. |
| Fillet / Chamfer | Set radius/distance → click first edge → click second edge → auto-commit on second click. |
| Mirror | SELECTING → click mirror-line point 1 → click point 2 → live flip preview → confirm (with erase-source sub-decision inside PENDING_CONFIRM). |
| Array (rect/polar/path) | SELECTING → parameters via dynamic input/dialog → live preview → commit. |

### Vertex Edit (`tools/vertex_edit/`) — see §6 for topology rules
| Tool | Lifecycle summary |
|---|---|
| Grip Select/Stretch | Object already selected, grips shown automatically → drag grip → live preview → release/click commits, Esc snaps back. |
| Add Vertex | Click existing segment → new vertex inserted, immediately draggable → click/Enter commits position. |
| Remove Vertex | Click vertex grip → Enter/confirm removes it, adjacent segments rejoin; Esc cancels. |
| Join | SELECTING multiple objects sharing endpoints (Enter confirms) → immediate commit, no ACTING phase. |
| Break (point / 2-point) | Click object → click break point(s) → commit implicit on final click. |

### Selection / Utility (`tools/selection/`)
| Tool | Lifecycle summary |
|---|---|
| Select (default) | SELECTING only — click, window-drag (fully enclosed), crossing-drag (touches count), Shift to toggle/remove. Feeds `SelectionModel` for whichever tool follows. |
| Erase/Delete | SELECTING → Enter/Delete key = immediate commit, no ACTING phase at all. |
| Stretch | SELECTING via crossing-window only → base point → destination point → commit; only crossing-caught vertices move. |

### Annotation (`tools/annotation/`)
| Tool | Lifecycle summary |
|---|---|
| Dimension (linear/aligned/angular) | Click point 1 → click point 2 → live placement preview → click to place = commit. |
| Text / Mtext | Click insertion point (Mtext: drag box) → type content → Enter/click-outside commits. |

---

## 8. Keyboard & Mouse Bindings (AutoCAD-derived)

| Input | Effect |
|---|---|
| Left-click (empty, IDLE) | Start selection or trigger armed tool |
| Left-click + drag left→right | Window select (must fully enclose) |
| Left-click + drag right→left | Crossing select (touch counts) |
| Shift + left-click | Toggle/remove from selection |
| Ctrl + left-click | Cycle overlapping objects at point |
| Right-click | Confirm (same as Enter in most contexts) / context menu in IDLE |
| Enter / Spacebar | Confirm current step, advance state |
| Esc (1st press) | Cancel current step |
| Esc (2nd press) | Full abort back to IDLE |
| Type value + Enter | Precise coordinate/distance/angle entry (absolute, `@x,y` relative, `@dist<angle` polar) |
| Tab (during ACTING) | Cycle dynamic-input fields |
| F8 | Toggle Ortho constraint |
| F3 | Toggle object snap |
| F9 | Toggle grid snap |
| F10 | Toggle polar tracking |
| F11 | Toggle object snap tracking |
| Shift (held) | Temporary snap-mode override |
| Ctrl+Z (mid-command) | Step back one point within the current command, not a full cancel |
| Scroll wheel / middle-click drag | Zoom / pan — modal interrupt, always resumes the suspended tool afterward |
| Leading `'` before a typed command | Transparent command (e.g. `'ZOOM`) — modal interrupt via command line |

---

## 9. Custom Snap Engine (`core/snapping/`)

Snapping sits as a pipeline stage between raw canvas events and the semantic events tools consume:

```
canvasMoveEvent → SnapEngine.resolve(point, tolerance) → snapped point → InputTranslator emits PointHover(point, snap_type) → tool's ACTING preview
```

**`SnapEngine`**: queries every active `SnapProvider` within tolerance on each move, collects candidates, resolves to the best match (nearest, or by explicit user-set priority — never a hardcoded priority).

**Providers** (`core/snapping/providers/`), each independently toggleable, **all off by default**:
- `VertexProvider` — existing feature vertices
- `MidpointProvider` — segment midpoints
- `CenterProvider` — circle/arc/polygon centroids
- `IntersectionProvider` — computed intersections, including against the in-progress sketch
- `PerpendicularProvider` / `ExtensionProvider` — relative to a reference line set during ACTING
- `GridProvider` — independent grid snap, combinable with others
- `SelfSnapProvider` — snaps against the entity currently being drawn (e.g. polyline closing to its own start)

Candidates are queried against both committed GeoPackage features and the live rubber-band geometry.

**Constraints are a separate later stage**, not part of snapping: `OrthoConstraint` / `PolarConstraint` (`core/constraints/`) adjust the already-snapped point afterward. Keep snap resolution and constraint application as two distinct pipeline stages.

This is entirely independent of QGIS's native Project Snapping Options. Optionally add a `QgisNativeSnapProvider` later as just another candidate source if interop is wanted — not required for v1.

---

## 10. UI Components (`ui/`)

- **Toolbar** — one button per tool in §7, routes through `CommandDispatcher`.
- **Command line widget** — text input, `returnPressed` parses token → `CommandDispatcher`; up/down arrow history; mid-command typed input routed as semantic events via `InputTranslator`, not treated as a new command.
- **Dynamic input widget** — floating frameless widget near cursor, live distance/angle/coordinate fields during ACTING, Tab cycles fields. Reuse `QgsAdvancedDigitizingDockWidget`'s coordinate parsing rather than reimplementing it.
- **Properties panel** — reflects current selection's parameters via a common interface (`get_property`/`set_property`) so it doesn't care whether the backing entity is a plain feature or something else later.
- **CAD Layers dock** — lists `cad_layers` table rows; edits color/linetype/visible/locked/sort_order; drives the rule-based renderer per geometry table (one rule per distinct `cad_layer` value) and drives `SelectionModel`'s lock enforcement.
- **Snap settings dock** — toggles for each `SnapProvider`, all off by default.

---

## 11. Folder Structure

```
plugin_root/
├── __init__.py                 # classFactory only
├── metadata.txt
├── plugin_main.py              # composition root: initGui/unload, wires everything below
│
├── core/
│   ├── tool_manager.py
│   ├── base_tool.py
│   ├── selection_model.py
│   ├── pending_action.py
│   ├── tool_context.py
│   │
│   ├── drawing/
│   │   ├── storage_manager.py
│   │   ├── entity_factory.py
│   │   └── cad_layers.py
│   │
│   ├── snapping/
│   │   ├── snap_engine.py
│   │   ├── snap_settings.py
│   │   └── providers/
│   │       ├── vertex_provider.py
│   │       ├── midpoint_provider.py
│   │       ├── center_provider.py
│   │       ├── intersection_provider.py
│   │       ├── perpendicular_provider.py
│   │       ├── extension_provider.py
│   │       ├── grid_provider.py
│   │       └── self_snap_provider.py
│   │
│   ├── constraints/
│   │   ├── ortho_constraint.py
│   │   └── polar_constraint.py
│   │
│   └── input/
│       ├── input_translator.py
│       ├── command_dispatcher.py
│       └── command_registry.py
│
├── tools/
│   ├── drawing/
│   │   ├── line_tool.py
│   │   ├── polyline_tool.py
│   │   ├── circle_tool.py
│   │   ├── arc_tool.py
│   │   ├── rectangle_tool.py
│   │   └── polygon_tool.py
│   ├── modify/
│   │   ├── move_tool.py
│   │   ├── copy_tool.py
│   │   ├── rotate_tool.py
│   │   ├── scale_tool.py
│   │   ├── trim_tool.py
│   │   ├── extend_tool.py
│   │   ├── offset_tool.py
│   │   ├── fillet_tool.py
│   │   ├── mirror_tool.py
│   │   └── array_tool.py
│   ├── vertex_edit/
│   │   ├── grip_edit_tool.py
│   │   ├── add_vertex_tool.py
│   │   ├── remove_vertex_tool.py
│   │   ├── break_tool.py
│   │   └── join_tool.py
│   ├── selection/
│   │   ├── select_tool.py
│   │   ├── erase_tool.py
│   │   └── stretch_tool.py
│   └── annotation/
│       ├── dimension_tool.py
│       └── text_tool.py
│
├── ui/
│   ├── toolbar.py
│   ├── properties_panel.py
│   ├── dynamic_input_widget.py
│   ├── command_line_widget.py
│   ├── cad_layers_dock.py
│   └── snap_settings_dock.py
│
├── resources/
│   ├── icons/
│   └── resources_rc.py
│
└── tests/
    ├── test_snap_engine.py
    ├── test_tool_manager.py
    └── test_storage_manager.py
```

**Layering rule:** `core/` contains no Qt-canvas dependency where avoidable (must be unit-testable headless). `tools/` is one file per command — never a monolith. `ui/` holds widgets only, talking to `core/` through interfaces, never containing domain logic itself. `plugin_main.py` only instantiates and wires; `__init__.py` never grows beyond `classFactory`.

---

## 12. Build Priority (suggested order for Claude Code)

1. `core/` skeleton: `BaseTool`, `ToolManager`, `SelectionModel`, `PendingAction`, `ToolContext`, `InputTranslator` — wire the state machine with no real tools yet, verify transitions with a dummy tool.
2. `StorageManager` + project-save gating (§5) + GeoPackage schema creation (§4).
3. `Select` tool + `SelectionModel` integration — needed before any modify tool can be tested.
4. Drawing tools: Line → Polyline → Rectangle → Circle → Arc → Polygon, each committing into the correct GeoPackage table via `EntityFactory`.
5. `SnapEngine` + `VertexProvider`/`MidpointProvider`/`GridProvider` (simplest providers first), wired into ACTING previews for the drawing tools already built.
6. Modify tools: Move → Copy → Rotate → Scale → Mirror → Trim/Extend → Offset → Fillet → Array.
7. Grip-based vertex editing (§6), including topological-editing awareness.
8. CAD Layers dock + rule-based renderer + lock enforcement in `SelectionModel`.
9. Command line + dynamic input widgets, routed through `CommandDispatcher`.
10. Remaining constraint providers (`Ortho`/`Polar`), remaining snap providers (`Intersection`/`Perpendicular`/`Extension`/`SelfSnap`).
11. Annotation tools (Dimension, Text).
12. Undo/redo verification against QGIS's native per-layer stack across every tool.