---
name: freecad-sketch
description: >-
  Create constrained 2D FreeCAD sketches: attach a sketch to a datum plane, add
  line, rectangle, circle, arc and point geometry, and drive it with geometric
  and dimensional constraints. Use to build the parametric profile that
  freecad-modeling turns into a solid feature.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, sketch, parametric-modeling, constraints, pipeline]
    depends: [freecad-session]
    search-hint: >-
      FreeCAD sketch create_sketch add geometry constraint rectangle circle line
      arc point coincident horizontal vertical distance radius angle DOF degrees
      of freedom fully constrained
    tools: tools.yaml
---

# FreeCAD Sketch

Build a constrained 2D profile before extruding it. A sketch is attached to a
datum plane inside a `PartDesign::Body`; geometry is added as elements, and
constraints drive those elements to a fixed shape.

Constraints are first-class here. Every constraint reference is validated
against the sketch's live element list before it is written, so a reference to
an element that does not exist is an error rather than something the solver
ignores. The remaining degrees of freedom (DOF) are read back from the solver
after every change.

## Reading DOF

`get_sketch_info` reports `dof` and `fully_constrained`. DOF is the number of
independent ways the sketch can still move:

* `dof > 0` — under-constrained; the solver is free to move the geometry.
* `dof == 0` — fully constrained; the profile is fixed.
* `dof < 0` — over-constrained; conflicting constraints are listed under
  `conflicting_constraints`.

A squared and sized rectangle needs four lines, four coincident constraints to
close the loop, horizontal and vertical constraints on opposing pairs, and two
dimensional constraints for its width and height. That pins the rectangle's
*shape* and leaves it two degrees of freedom: its position. Reaching `dof == 0`
additionally needs the sketch anchored to the origin, and this skill's constraint
vocabulary has no anchor constraint, so a rectangle here settles at `dof == 2`.
Treat `fully_constrained` as false for it and check `dof` with `get_sketch_info`
rather than assuming the recipe alone reaches zero.

## Element references

Constraints address elements as `{element: <index>, position: <name>}`, where
`element` is the geometry index returned by `add_sketch_geometry` and `position`
selects a point on it:

| `position` | Meaning |
|---|---|
| `none` | the edge or curve itself |
| `start` | start point of a line or bounded curve |
| `end` | end point |
| `mid` | centre of a circle or arc |

`concentric` is accepted and applied as a coincident constraint between two
centres, which is how FreeCAD expresses it.
