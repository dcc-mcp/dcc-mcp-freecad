---
name: freecad-drawing
description: >-
  Turn FreeCAD geometry into 2D engineering drawings: create a bounded TechDraw
  page with isometric, front, top and right views, then export it to PDF or SVG.
  Use after freecad-modeling has produced the geometry to document.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, techdraw, drawing, pdf, svg, pipeline]
    depends: [freecad-session]
    search-hint: >-
      FreeCAD TechDraw drawing page 2D engineering drawing projection views
      isometric front top right scale A4 template export PDF SVG
    tools: tools.yaml
---

# FreeCAD Drawing

Two calls, in order: `create_drawing_page` writes a TechDraw page into an
existing `.FCStd` document, and `export_drawing` renders that page to PDF or
SVG.

`create_drawing_page` uses only the App-side `TechDraw` module and needs no GUI.
`export_drawing` renders through the page's native view provider, so it starts
the FreeCAD GUI **inside the isolated process the adapter already uses** — with
`QT_QPA_PLATFORM=offscreen`, a throwaway user config and the configured
deadline. No GUI session outlives a call, and nothing is ever screenshotted.

Page creation is a document mutation: it runs on a sibling staging copy and
replaces the original only after a successful save. An existing `page_name` is
refused rather than replaced.

## Scale

Omit `scale` to auto-fit: every requested view is laid out on a grid, the
projected extents are measured, and the largest standard drawing scale that
still fits is applied. The scale actually used is always returned in `scale`,
together with `scale_auto` so the caller can tell a fitted value from one it
asked for.

## Templates

`template` is a built-in page template name (`A4_Landscape`, default, or
`A4_Portrait`) or a path to an `.svg` template file under the allowed roots. The
built-ins ship with the adapter so the page size is identical on every supported
host. A missing template — or one with no usable `width`/`height` — is an error,
never a silent fallback to the default.

## Capability

A host without `TechDraw`, without the FreeCAD GUI library, or without the
shipped templates reports `drawing.status: host_limited` from `get_capabilities`,
with the reason. Callers should gate on that rather than discovering it on the
first export.
