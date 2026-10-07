---
name: freecad-sketch
description: >-
  Build constrained 2D PartDesign sketches: create a sketch on a named plane,
  add typed geometry, add geometric and dimensional constraints, and read back
  degrees of freedom before the sketch is used as a feature profile. Use after
  freecad-session has created or inspected a durable FCStd document.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, parametric-modeling, sketch, constraints]
    depends: [freecad-session]
    search-hint: >-
      FreeCAD PartDesign sketch create geometry rectangle circle line arc point
      constraint coincident horizontal vertical parallel perpendicular tangent
      concentric radius distance angle degrees of freedom DOF fully constrained
    tools: tools.yaml
---

# FreeCAD Sketch

Build a parametric profile the typed way: `create_sketch` attaches a
`Sketcher::SketchObject` to a datum plane inside a `PartDesign::Body`,
`add_sketch_geometry` adds typed elements, `add_sketch_constraint` constrains
them, and `get_sketch_info` reports what the solver thinks of the result.

Constraints are not optional decoration. The adapter accepts no arbitrary
Python, so there is no way to repair a sketch afterwards: an under-constrained
sketch solves differently on the next host version, which is why
`get_sketch_info` reports `dof` and `feature_ready` and why a sketch with
unconstrained degrees of freedom is never reported as usable for a feature.

## Arc direction

An `arc` is specified by `start_angle_degrees` and `end_angle_degrees`, and the
**sign of the sweep decides the direction**: `0 -> 90` is anticlockwise, `90 -> 0`
is clockwise. Both are stored as the sweep they describe — the start point is the
angle you passed as `start`, not whichever end happens to be lower.

Both directions are stored as the arc you asked for, and the read-back compares
the start point, the midpoint and the end point, so a request that came back as
the complementary arc is reported as a mismatch rather than a success. An arc
whose start and end land on the same point is refused — use `circle` for a full
turn.

## Constraints are the contract

* `dof` counts unconstrained degrees of freedom. `feature_ready` is true only
  when `dof` is `0` **and** the sketch contains geometry.
* A constraint that references an element index which does not exist is refused
  with `sketch_element_not_found`. Nothing is silently dropped.
* A constraint applied to geometry that cannot carry it (a radius on a line, a
  horizontal on a circle) is refused rather than reinterpreted.
* Pass `require_fully_constrained=true` to `get_sketch_info` to turn an
  under-constrained sketch into an explicit error carrying the error code
  instead of a report the caller has to interpret.
* A host that will not report degrees of freedom yields `dof: null` and
  `feature_ready: false`, never a zero that was not measured.

Every mutation runs on a staging copy and replaces the document only after the
write has been read back, so a failed sketch edit leaves the original bytes
untouched.
