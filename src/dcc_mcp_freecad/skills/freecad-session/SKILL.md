---
name: freecad-session
description: >-
  Inspect a connected FreeCAD session through the DCC-MCP console/standalone boundary.
  This first slice is read-only and does not execute arbitrary source.
license: MIT
compatibility: "FreeCAD; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.1.0"
    tags: "freecad,mcp,dcc,automation"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# FreeCAD

Experimental first slice. Live host validation, version matrices, catalog
onboarding, and mutation tools are separate follow-up gates.

