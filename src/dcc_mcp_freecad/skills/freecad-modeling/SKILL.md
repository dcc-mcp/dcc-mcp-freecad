---
name: freecad-modeling
description: >-
  Build parametric FreeCAD primitives, update dimensions and placements, scale
  copy and mirror objects, perform booleans, and import or export CAD/mesh
  geometry. Use after freecad-session has created or inspected a durable FCStd
  document.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, parametric-modeling, pipeline]
    depends: [freecad-session]
    search-hint: >-
      FreeCAD add update transform scale copy mirror primitive box cylinder
      sphere cone torus wedge helix boolean union cut intersection import export
      STEP IGES BREP STL OBJ 3MF
    tools: tools.yaml
---

# FreeCAD Modeling

Use these typed tools after creating or inspecting an `.FCStd` document with
`freecad-session`. Mutations are atomic and preserve the original document on
failure. Boolean results remain parametric and retain their operand links.

Scaling, copying and mirroring all write to a **new** object named by the
caller, because FreeCAD silently ignores a `Shape` assignment on a parametric
primitive - an in-place variant would report success while the geometry stayed
the same. A copy of a primitive keeps its parametric type; a scaled or mirrored
result becomes a plain `Part::Feature`, except that a mirror which keeps its
source stays a live `Part::Mirroring` linked to it. A copy's `translation` and
`rotation_degrees` are absolute for every source type, so copying without them
puts the copy on top of its source rather than at the document origin.

Geometry import/export supports STEP, IGES, BREP, STL, OBJ, and 3MF. Mesh export
uses explicit bounded tessellation settings. No inline Python is accepted here —
the typed tools accept arguments, never source text. The one escape hatch is
`freecad-session`'s `run_script`, which takes a **path to a script file** and
runs it in a disposable FreeCAD process; it is not a sandbox, and it does not
load user workbenches, plugins, or macros. Every 3MF export is asserted to
declare the millimetre unit, because a consumer scales the model by that
declaration.
