---
name: freecad-feature
description: >-
  Turn a constrained FreeCAD PartDesign sketch into a parametric solid: pad,
  pocket, revolution, groove, loft, sweep, and hole. Every tool reads the
  body's volume back and refuses to report success unless the geometry actually
  changed in the required direction. Use after freecad-sketch has produced a
  fully constrained profile.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, parametric-modeling, partdesign, pad, pocket, revolution, groove, loft, sweep, hole]
    depends: [freecad-sketch]
    search-hint: >-
      FreeCAD PartDesign feature pad extrude pocket cut revolution revolve groove
      loft sweep hole counterbore countersink parametric solid volume readback
    tools: tools.yaml
---

# FreeCAD Sketch Features

These tools turn a constrained sketch into a parametric solid. Unlike a boolean
cut, a PartDesign feature stays editable: change a dimension and the feature
recomputes.

## Nothing here trusts the return value

Every tool measures the body's volume **before** and **after** the feature and
asserts three things:

* **direction** — a pocket must reduce the volume, a pad must increase it;
* **presence** — a delta of zero is an error, never a success with a small number;
* **magnitude** — the change must match what the requested profile and extent
  require, so a clipped or silently reinterpreted feature cannot pass.

That is the direct answer to upstream FreeCAD issue #99, where `pocket_sketch`
reports success and a Valid state while removing no material. A tool that
removed nothing returns an error carrying `volume.expected_delta` and
`volume.delta` side by side, so the caller sees the gap instead of inferring it.

The response carries the whole comparison under `volume`, and the names of the
checks that ran under `verified`.

## Sketches must be fully constrained

Every tool refuses an under-constrained sketch. A profile with free degrees of
freedom is not a shape you chose — it is whatever the solver settled on, and it
moves on the next host version. Run `get_sketch_info` first and check
`feature_state.feature_ready`; the tools re-check it and refuse anyway.

Degenerate profiles are refused the same way: a sketch that encloses no area is
open, self-intersecting, or zero-thickness, and would otherwise become an empty
shell that still reports success.

## `side_type` is probed, never assumed

`side_type` is a request-side concept (`one_side` / `two_sides` / `symmetric`),
not a host property name. The adapter resolves it by capability, trying
`SideType`, then `Midplane`, then `Symmetric`, and refuses a host that exposes
none of them. Which property took the write is reported as
`feature.side_property`, because that is how a host that moved the property is
recognised after the fact.

**`revolution_feature` and `groove_feature` deliberately accept no `side_type`.**
They carry no reversal property at all — there is no second side to grow
towards, only an angle to sweep. Writing one there would be a silent no-op.

## Failure leaves the document untouched

Every mutation runs on a staging copy and replaces the document only after the
write has been read back, so a feature that fails — or one that changed nothing
— leaves the original bytes exactly as they were.

## Error codes

| Code | Meaning |
|---|---|
| `E_SKETCH_UNDERCONSTRAINED` | The sketch still has free degrees of freedom. |
| `E_SKETCH_OVERCONSTRAINED` | Conflicting constraints; the solver did not commit a solution. |
| `E_SKETCH_DOF_UNAVAILABLE` | The host would not report degrees of freedom, so readiness is unproven. |
| `E_SKETCH_SOLVER_FAILED` | The solver did not converge. |
| `E_PROFILE_DEGENERATE` | The sketch encloses no area; it is open, self-intersecting, or empty. |
| `E_EXTENT_DEGENERATE` | The requested extent yields zero volume change. |
| `E_FEATURE_EMPTY` | The feature produced no solid. |
| `E_FEATURE_INVALID` | The feature produced a shape that is not valid. |
| `E_SIDE_TYPE_INVALID` | `side_type` is not one of the three supported values. |
| `E_ANGLE_INVALID` | `angle_degrees` is outside `(0, 360]`. |
| `E_SKETCH_NOT_IN_BODY` | The sketch has no owning `PartDesign::Body`. |
| `E_FEATURE_UNSUPPORTED` | This host build does not expose the feature's properties. |
| `E_HOLE_TYPE_INVALID` | `hole_type` is not one of the three supported values. |
| `E_SECTIONS_REQUIRED` | A loft needs at least two profile sketches. |
