---
name: freecad-modeling
description: >-
  Build parametric FreeCAD primitives, update dimensions and placements,
  perform booleans, and import or export CAD/mesh geometry. Use after
  freecad-session has created or inspected a durable FCStd document.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0+; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.1.2"  # x-release-please-version
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
uses explicit bounded tessellation settings; no arbitrary Python or macros are
accepted.
