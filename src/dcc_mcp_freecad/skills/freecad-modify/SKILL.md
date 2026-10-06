---
name: freecad-modify
description: >-
  Finish and repeat FreeCAD solids: fillet and chamfer edges, build linear and
  polar patterns, and mirror a solid across a base plane. Every tool validates
  its inputs against the real geometry before writing and proves the change by
  reading the result back.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, fillet, chamfer, pattern, mirror, pipeline]
    depends: [freecad-session]
    search-hint: >-
      FreeCAD fillet chamfer round bevel edge linear polar pattern array mirror
      bolt circle heatsink symmetric
    tools: tools.yaml
---

# FreeCAD Modify

Dress-up and pattern operations for solids that already exist. Use
`freecad-session` to create or inspect a document, `freecad-modeling` to build
and combine primitives, then this skill to finish the part.

Edges are addressed by **1-based index** into the object's edge list. Call
`inspect_document` first and read `shape.edges` to learn how many edges the
object has; an index outside that range is an error, never a silent skip.

## What each tool guarantees

- **`fillet_edges` / `chamfer_edges`** — the radius or distance is checked
  against the extent of the faces adjacent to each requested edge *before* the
  kernel is asked. An infeasible size is refused with `E_RADIUS_NOT_FEASIBLE`
  or `E_DISTANCE_NOT_FEASIBLE` and the largest size the geometry absorbs, so a
  self-intersecting solid is never returned as a success. Afterwards the result
  is read back to confirm the size was stored on every requested edge, that the
  solid count did not change, and that the volume actually moved.
- **`linear_pattern` / `polar_pattern`** — the result carries one solid group per
  instance, and its volume must equal the source volume times the instance
  count. `overlap_detected` and `min_instance_gap` report whether neighbouring
  instances collide, so a pattern with too small a spacing is visible instead of
  shipping as a fused blob.
- **`mirror_feature`** — a mirror is an isometry, so the read-back asserts the
  volume and the solid, face, edge and vertex counts are all unchanged. Either
  one moving means the host did something other than a mirror.

Patterns produce a **compound of transformed copies**, not a live parametric
array: re-running the tool is how a pattern is changed. To merge a pattern into
a body, pass the result to `boolean_operation`.

## Refusals

Every refusal carries a stable code on the error (`BridgeError.code`), so a
caller can branch on why instead of parsing prose: `E_OBJECT_NOT_FOUND`,
`E_NO_SHAPE`, `E_NOT_A_SOLID`, `E_EDGE_REFS_REQUIRED`, `E_EDGE_REF_INVALID`,
`E_EDGE_REF_DUPLICATE`, `E_EDGE_REF_OUT_OF_RANGE`, `E_EDGE_REF_LIMIT`,
`E_RADIUS_NOT_FEASIBLE`, `E_DISTANCE_REQUIRED`, `E_DISTANCE_CONFLICT`,
`E_DISTANCE_NOT_FEASIBLE`, `E_INSTANCE_COUNT_INVALID`, `E_INSTANCE_LIMIT`,
`E_PATTERN_SPACING`, `E_PATTERN_DEGENERATE`, `E_ZERO_VECTOR`,
`E_ANGLE_REQUIRED`, `E_ANGLE_CONFLICT`, `E_ANGLE_INVALID`, `E_PLANE_INVALID`,
`E_PLANE_OFFSET_INVALID`, `E_RESULT_EXISTS`, `E_SAVE_FAILED`.

Mutations are atomic: the tool writes to a staged copy and publishes it only
after the read-back passes, so a refused call leaves the source document
byte-for-byte unchanged.
