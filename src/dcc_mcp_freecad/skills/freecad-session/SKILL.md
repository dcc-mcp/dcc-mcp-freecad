---
name: freecad-session
description: >-
  Create, inspect, validate, copy, and safely edit durable FreeCAD documents
  through an isolated FreeCADCmd process. Use for FCStd document lifecycle and
  diagnostics; load freecad-modeling for geometry operations.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0+; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.2.0"  # x-release-please-version
    tags: [freecad, cad, parametric-modeling, pipeline]
    search-hint: >-
      FreeCAD status capabilities create inspect validate copy FCStd document
      remove object dependency-aware atomic save
    tools: tools.yaml
---

# FreeCAD Session

Start with `get_status`, then create or inspect a durable `.FCStd` document.
Every mutation runs on a sibling staging copy and replaces the original only
after FreeCADCmd reports success and a non-empty document exists.

Source and output paths must stay under `DCC_MCP_FREECAD_ALLOWED_ROOTS`.
Existing destinations require explicit `overwrite=true`. `remove_object`
refuses referenced objects unless `cascade=true` is explicit.
