# Ugsurv Plugin — Claude Instructions

## Terminology

- **cad_layer** — the logical draw-order grouping concept (e.g. "Layer 1", "Layer 2"). This is a field/attribute on every feature, and a row in the non-spatial `cad_layers` GeoPackage table. Controls Z-order / render order of features.
- **layer** — a QGIS vector layer (Points, Lines, Circles, Dimensions). These are what appear in the QGIS layers panel.

Never use "layer" to mean "cad_layer". They are different things.

## QGIS Layers Panel vs Backend Data Model

**Layers panel (visible to user):**
```
Ugsurv
  └── Points        ← single QGIS layer for all points across all cad_layers
  └── Lines         ← single QGIS layer for all lines across all cad_layers
  └── Circles       ← single QGIS layer for all circles across all cad_layers
  └── Dimensions    ← single QGIS layer for all dimensions across all cad_layers
```

**Backend (GeoPackage, invisible to user):**
- Every feature has a `cad_layer` field (string name matching a row in `cad_layers` table)
- The `cad_layers` non-spatial table stores: name, color, linetype, visible, locked, sort_order
- Draw order is controlled by `sort_order` — lower sort_order renders first (bottom), higher renders on top
- Two features in different cad_layers both appear in the same QGIS layer but one draws over the other

**Purpose:** Allows changing display order of individual features without creating separate QGIS layers. The user changes a feature's cad_layer assignment to move it in front of or behind other features.

---

## Feature Selection Standards

All map-tool feature selection must follow these rules:

### 1. No native QGIS selection
Never call `layer.select()`, `layer.selectByIds()`, or `layer.deselect()`. The rubber band is the sole visual indicator of what is picked. Native selection highlights interfere and are not used.

### 2. Rubber band is the only visual feedback
Use `QgsRubberBand` styled with the `_style.RB_IDENTIFY_*` constants from `core/style.py`:
```python
rb.setColor(_style.RB_IDENTIFY_RED)
rb.setWidth(_style.RB_IDENTIFY_WIDTH)   # 2
rb.setLineStyle(_style.RB_LINE_STYLE)   # DashLine
rb.setFillColor(_style.RB_IDENTIFY_RED_FILL)
```
Secondary/adjacent band uses `RB_IDENTIFY_BLUE` / `RB_IDENTIFY_BLUE_FILL`.

### 3. Silent duplicate rejection
Use a `_picked_fids` set keyed by `(layer.id(), feature.id())`. If the key is already present, return immediately — no log message, no status update.
```python
fid_key = (feat_layer.id(), feature.id())
if fid_key in self._picked_fids:
    return
self._picked_fids.add(fid_key)
```

### 4. _picked_fids cleared everywhere state resets
Clear `_picked_fids` in every reset path: `deactivate()`, right-click reset block, and any `_clear_state()` helper. Never leave stale keys.

---

## Layer Naming Convention

Most plugin-managed QGIS layers use an underscore prefix in their names (e.g. `_circles`, `_lines`, `_points`). This is defined as `LAYER_NAME` constants in each drawer module.

**Exception:** The dimensions layer is named `dimensions` (no underscore) — `_DIM_LAYER_NAME = "dimensions"` in `tools/annotation/dimension_tool.py`.

Any code that branches on layer name must use the exact name. Prefer importing the `LAYER_NAME` constant from the relevant drawer module rather than hardcoding the string.

---

## Project Data Folder

Active working folder for all project layers: `C:\Users\Nuwe Surv\Desktop\Ugsurv_features`

Use this as the default/expected location when writing code that opens, saves, or references layer files (e.g. default directory for file dialogs, constructing layer paths).

---

## CRS Rules

All plugin geometry is stored exclusively in **EPSG:32636** (WGS 84 / UTM Zone 36N).

**Layer CRS**: EPSG:32636 — fixed, never changes, enforced in `StorageManager._create_gpkg()`.

**Allowed project/display CRS** (8 systems):
- WGS84: 35N EPSG:32635, 36N EPSG:32636, 35S EPSG:32735, 36S EPSG:32736
- Arc1960: 35N EPSG:21035, 36N EPSG:21036, 35S EPSG:21095, 36S EPSG:21096

**On plugin load**: `CrsManager.enforce_project_crs()` forces project CRS to EPSG:32636.

**On CRS change**: If user picks a CRS outside the 8 allowed, CrsManager reverts it to 32636 and shows a QMessageBox warning.

**Coordinate transformation**: All geometry from drawing tools is in project CRS. `StorageManager.add_line()` / `add_point()` call `CrsManager.transform_geom_to_layer()` before saving. `EntityFactory.commit()` routes through these methods. Move/copy tools transform clicked points via `CrsManager.project_to_layer()` before computing deltas.

**CRS badge**: DynamicInputWidget shows current project CRS short label (e.g. "WGS84 36N") in a teal badge, updated via `CrsManager.crsLabelChanged` signal.

**Key file**: `core/crs_manager.py` — source of truth for LAYER_EPSG, ALLOWED_EPSG, and transformation helpers.

---

## Planned Tools

### RevertGeometryDock
- **Command**: `REVERT` / `RV` in `acceptInput` in Ugsurv.py
- **File**: `module_wz_dialogs/revert_geometry.py` (new file)
- **Class**: `RevertGeometryDock(QDockWidget)`
- **Purpose**: Restores original geometry from `original_geometry` WKT column on selected features
- **Placement**: left dock area, same `_show_dock` pattern as SOLVETOPO
- **UI**: Layer combo (polygon layers), "Revert selected" button, status label
- **Behaviour**: reads `original_geometry` column → `QgsGeometry.fromWkt()` → `layer.changeGeometry(fid, geom)`; reports "Reverted N / M selected features"
