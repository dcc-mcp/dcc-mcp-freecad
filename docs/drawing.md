# 2D drawing pages

FreeCAD's TechDraw workbench turns 3D geometry into a dimensioned 2D drawing.
This adapter exposes that as two bounded tools in the `freecad-drawing` skill:
`create_drawing_page` writes a page into an existing FCStd document, and
`export_drawing` renders that page to PDF or SVG.

Neither tool accepts arbitrary Python, macros, or workbench commands.

## The two calls

```json
{"tool": "create_drawing_page", "arguments": {
  "document_path": "projects/enclosure.FCStd",
  "object_names": ["BodyWithPort"],
  "views": ["isometric", "front", "top"],
  "page_name": "Sheet"
}}
```

```json
{"tool": "export_drawing", "arguments": {
  "document_path": "projects/enclosure.FCStd",
  "page_name": "Sheet",
  "output_path": "build/enclosure.pdf"
}}
```

`create_drawing_page` is a document mutation. It runs on a sibling staging copy
and replaces the original only after a successful, non-empty save, exactly like
every other mutation in this adapter. An existing `page_name` is refused rather
than replaced, because replacing it would silently invalidate every drawing
already exported from it.

`export_drawing` shares `export_geometry`'s output contract: the destination must
end in `.pdf` or `.svg`, must sit under `DCC_MCP_FREECAD_ALLOWED_ROOTS`, and an
existing file requires explicit `overwrite=true`.

## Scale

Omit `scale` and the page is auto-fitted: the requested views are laid out on a
grid inside the page margins, the sources' projected extents are measured, and
the largest standard drawing scale at which every view still fits is applied.
The scale actually used is always returned, together with `scale_auto`, so a
caller can tell a fitted value from one it asked for.

Pass `scale` to override the fit. An explicit zero is refused rather than being
silently replaced by a default, which is the same swallowed-parameter rule the
rest of the adapter follows.

## Templates

`template` is either a built-in name or a path to an SVG template file under the
allowed roots. The built-ins ship with the adapter, next to the packaged driver,
so the page size is identical on every supported host instead of depending on
which template files a given FreeCAD installation happens to carry.

A missing template, or one whose root `svg` element declares no usable
`width`/`height`, is an error. Nothing silently falls back to a default: the page
size drives every fitted scale, so a fallback would change the drawing without
saying so.

## No GUI session

Creating a page needs only the App-side `TechDraw` module and runs in a plain
console process. Rendering a page needs the page's native view provider and
`PagePrinter`, both of which live in `TechDrawGui`, so `export_drawing` starts
the FreeCAD GUI inside the isolated process the bridge already owns, with
`QT_QPA_PLATFORM=offscreen`, a throwaway user config and the configured
deadline.

No GUI state outlives a call, and nothing is ever screenshotted. That keeps the
drawing path orthogonal to the GUI-screenshot crash class reported against
TechDraw elsewhere: this adapter never captures the FreeCAD window.

## What is proven before a call returns

Both tools follow the [post-write read-back contract](write-contract.md) and
return the list of checks they ran in `verified`.

`create_drawing_page` saves the document first, then reads the page back off it:
the page exists with the right type and is wired to its template, the page lists
exactly the views that were added, and every view has the requested type,
sources, projection direction, scale and page position. Each view must also have
projected something, which is the check that separates a real drawing from a
page of empty frames.

`export_drawing` reads the artefact back off disk. Non-empty is not enough: a
PDF must parse to exactly one page and an SVG must parse with an `svg` root
element, so a blank or truncated render fails instead of reaching the caller as
a success.

## Host requirements

A host that cannot do 2D drawing says so before the first call.
`get_capabilities` reports a `drawing` block:

```json
{"status": "host_limited", "reason": "TechDraw is unavailable on this host: ...",
 "views": ["front", "isometric", "right", "top"],
 "templates": ["A4_Landscape", "A4_Portrait"],
 "extensions": [".pdf", ".svg"]}
```

`status` is `available` only when the host exposes `TechDraw`, imports the
FreeCAD GUI library, and the adapter's built-in templates are installed next to
the driver. Otherwise it is `host_limited` with a reason a caller can gate on.
The probe is guarded, so a host that cannot answer still reports everything else.

## Bounds

Sources, views, page-name length, formats and deadlines are all bounded; see
`tools.yaml` in the `freecad-drawing` skill for the exact limits, and
[the write contract](write-contract.md) for what each tool must prove before it
returns.
