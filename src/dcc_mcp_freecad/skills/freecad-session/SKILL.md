---
name: freecad-session
description: >-
  Create, inspect, validate, copy, snapshot, restore, and safely edit durable
  FreeCAD documents through an isolated FreeCADCmd process. Use for FCStd
  document lifecycle, recoverability, and diagnostics; load freecad-modeling
  for geometry operations.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, cad, parametric-modeling, pipeline]
    search-hint: >-
      FreeCAD status capabilities create inspect validate copy snapshot restore
      undo recover rollback FCStd document remove object dependency-aware
      atomic save
    tools: tools.yaml
---

# FreeCAD Session

Start with `get_status`, then create or inspect a durable `.FCStd` document.
Every mutation runs on a sibling staging copy and replaces the original only
after FreeCADCmd reports success and a non-empty document exists.

Source and output paths must stay under `DCC_MCP_FREECAD_ALLOWED_ROOTS`.
Existing destinations require explicit `overwrite=true`. `remove_object`
refuses referenced objects unless `cascade=true` is explicit.

## Recoverability

Every call is a process-level commit, so there is no undo stack to rewind.
`create_snapshot` copies a document's bytes into the snapshot store, and
`restore_snapshot` puts them back. Use `create_snapshot` before any destructive
call you might want to reverse, and pass `expected_sha256` to `restore_snapshot`
so new work is never silently overwritten. A restore snapshots the state it
replaces and returns that id as `undo_snapshot_id`, so a mistaken restore is
just another restore.

The store lives inside `DCC_MCP_FREECAD_ALLOWED_ROOTS` and is capped by
`DCC_MCP_FREECAD_MAX_SNAPSHOTS` and `DCC_MCP_FREECAD_MAX_SNAPSHOT_BYTES`. When
it is full the call is refused with `snapshot_limit_exceeded` — nothing is
evicted behind your back. Use `delete_snapshot` to free capacity.
