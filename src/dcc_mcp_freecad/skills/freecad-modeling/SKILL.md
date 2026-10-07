---
name: freecad-modeling
description: >-
  Build parametric FreeCAD primitives, update dimensions and placements,
  perform booleans, and import or export CAD/mesh geometry. Use after
  freecad-session has created or inspected a durable FCStd document.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.7.0"  # x-release-please-version
    tags: [freecad, cad, parametric-modeling, pipeline]
    depends: [freecad-session]
    search-hint: >-
      FreeCAD add update transform primitive box cylinder sphere cone torus
      boolean union cut intersection import export STEP IGES BREP STL OBJ
    tools: tools.yaml
---

# FreeCAD Modeling

Use these typed tools after creating or inspecting an `.FCStd` document with
`freecad-session`. Mutations are atomic and preserve the original document on
failure. Boolean results remain parametric and retain their operand links.

Geometry import/export supports STEP, IGES, BREP, STL, and OBJ. Mesh export
uses explicit bounded tessellation settings. No inline Python is accepted here —
the typed tools accept arguments, never source text. The one escape hatch is
`freecad-session`'s `run_script`, which takes a **path to a script file** and
runs it in a disposable FreeCAD process; it is not a sandbox, and it does not
load user workbenches, plugins, or macros.
