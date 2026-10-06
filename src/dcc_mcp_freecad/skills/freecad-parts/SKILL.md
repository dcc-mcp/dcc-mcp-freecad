---
name: freecad-parts
description: >-
  Browse and insert standard parts (fasteners, bearings, profiles, washers) from
  a configured offline parts library into a durable FCStd document. Use after
  freecad-session has created or inspected the document; load freecad-modeling
  for parametric primitives and booleans.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, standard-parts, library, assembly]
    depends: [freecad-session]
    search-hint: >-
      FreeCAD standard parts library list insert bolt screw nut washer bearing
      fastener profile STEP offline
    tools: tools.yaml
---

# FreeCAD Parts

`list_parts` browses the configured offline parts library and returns a
`part_ref` per entry. `insert_part` takes one of those references, resolves it
against the library roots, and inserts it into a document with the same
placement contract as `add_primitive`.

Start with `list_parts`. Never build a `part_ref` by hand: it is the relative
path `list_parts` returned, and anything else is refused before FreeCAD starts.

## Offline only

The library is one or more local directories configured through
`DCC_MCP_FREECAD_PARTS_LIBRARY` (`os.pathsep`-separated). The adapter never
downloads, extracts, or caches a library, and writes nothing outside the
directories the operator named. A URL-shaped root is refused with
`remote_library_unsupported` rather than silently fetched.

The roots are operator-configured and are deliberately *not* required to sit
inside `DCC_MCP_FREECAD_ALLOWED_ROOTS` — a shared read-only library normally
lives outside a project sandbox. What the caller can never do is name a file:
only a relative path the library listed is accepted.

## Path containment

Every `part_ref` is rejected, with a stable error code and before any file is
read, when it:

- contains an empty, `.` or `..` segment (`invalid_part_ref`);
- is absolute in any spelling — POSIX, drive-letter, UNC or `\\` prefix
  (`invalid_part_ref`);
- uses a backslash instead of `/` (`invalid_part_ref`);
- carries a suffix outside the supported exchange set
  (`unsupported_part_format`);
- resolves outside every configured root, including through a symlink
  (`part_ref_escapes_library`).

This is the check the comparable FreeCAD MCP project was missing in its
`insert_part_from_library` (their issue #121). It stays a gate, not a schema
hint, because a direct script call bypasses the schema.

## Result shape

`insert_part` returns the inserted object in the same shape
`inspect_document` reports it: name, label, type id, placement, and — for BRep
formats — topology counts, volume, area and bounding box. It reads the object
back after the save, so an entry that imports to an empty or invalid shape is
reported as a failure instead of a quiet insert. Follow it with
`validate_document`.
