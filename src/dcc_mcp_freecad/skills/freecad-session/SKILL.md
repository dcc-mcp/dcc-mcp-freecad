---
name: freecad-session
description: >-
  Discover, create, inspect, validate, copy, snapshot, restore, and safely edit
  durable FreeCAD documents through an isolated FreeCADCmd process. Use for
  FCStd document lifecycle, discovery, recoverability, and diagnostics; load
  freecad-modeling for geometry operations.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.8.0"  # x-release-please-version
    tags: [freecad, cad, parametric-modeling, pipeline]
    search-hint: >-
      FreeCAD status capabilities list discover create inspect validate copy
      snapshot restore undo recover rollback FCStd document remove object
      dependency-aware atomic save run script python file escape hatch
    tools: tools.yaml
---

# FreeCAD Session

Start with `get_status`. When the caller does not already know the path, use
`list_documents` first: it enumerates the `.FCStd` files under
`DCC_MCP_FREECAD_ALLOWED_ROOTS` and returns each one's `absolute_path`, ready
to hand to `inspect_document`. It is a filesystem listing, so it works even
before a FreeCAD host is configured, and it never opens, recomputes, or rewrites
a document. Paging is bounded: follow `next_offset` while it is non-null, and
narrow `root` or `pattern` only once `next_offset` is null while
`scan_budget_exhausted` is true.

Entries are sorted by path. When the scan budget runs out the walk stops early,
so entries are drawn from whatever the scan reached — but that partial set is
still paged normally: `scan_budget_exhausted: true` with a non-null
`next_offset` means more already-scanned entries remain, and paging on is the
only way to see them. Do not stop at the first `scan_budget_exhausted`. Only
when `next_offset` is null *and* `scan_budget_exhausted` is true has the scanned
portion been fully read; files past the stopping point were never visited and no
`offset` reaches them, so narrow `root` or `pattern` then.

Check the budget counters even when `truncated` is false. The checksum read
budget does not stop a listing: exhausting it turns later digests `unavailable`
while enumeration continues, and `truncated` reflects the scan alone. So
`truncated: false` can coexist with `sha256_scope: "unavailable"` — only
`read_budget_exhausted: true` explains the missing digests, so read it before
treating a null `sha256` as a broken document.

Size caps: at most 200 entries per page, and the serialized `entries` array is
capped at 256 KiB, counted as the per-entry JSON character length summed over
the emitted entries (no comma or bracket separators). The response envelope
(`roots`, `pattern`, and the paging and budget counters) is not counted against
it.

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

## Escape hatch: `run_script`

`run_script` is the one entry point for work no typed tool covers. It executes a
single `.py` file in a disposable FreeCADCmd child process and returns
`exit_code`, `stdout`, `stderr`, `stdout_truncated` / `stderr_truncated`,
`timed_out`, `script_path`, and `script_sha256`.

A **timeout is returned as an error**, not as a successful result carrying
`timed_out: true` — `success` is `false` and `error` is `script_timeout`, with
the pre-hang `stdout` / `stderr` and `timed_out: true` in the context so the
caller can see how far the script got. A timeout must never look like a finished
run. A **non-zero exit code is not an error**: the script ran and decided to
fail, and reading `exit_code` is the caller's job.

**It is not a sandbox.** The script runs as the operator's own account with that
account's full privileges: it can read, write, and import anything that account
can. `DCC_MCP_FREECAD_ALLOWED_ROOTS` constrains **which script may be named**,
not **what the script may do**. Do not treat this tool as a boundary for
untrusted code.

What it does constrain:

- **Path only, never source text.** `script_path` must end in `.py`, must exist,
  is resolved (symlinks followed) and then must lie inside
  `DCC_MCP_FREECAD_ALLOWED_ROOTS` — the same gate the document tools use. A link
  inside a root that points outside it is refused.
- **Vacuum mode.** The child starts with `--safe-mode` and a throwaway user
  config, so no user workbench, plugin, or macro is loaded. State left behind by
  a previous GUI session cannot reach this process.
- **Its own timeout.** `timeout_secs` is capped at
  `DCC_MCP_FREECAD_MAX_SCRIPT_TIMEOUT_SECS` (300s by default), not the 1800s
  document ceiling — a script that blocks on a modal dialog is killed in
  minutes. The child is terminated and `success` comes back `false` with
  `error: script_timeout`; what the script printed before it hung is preserved
  in `stdout`, which is usually the only clue to where it blocked.
- **No management surface.** There is no list, read, create, or delete for
  scripts. Maintain them on the filesystem.

`stdout` is the script's output **plus the host's**: FreeCADCmd writes its own
banner - version, licence, and a safe-mode notice - to the same stream. Match
substrings rather than comparing the whole stream, and prefer a script that
writes its results somewhere you control. `stderr` is less reliable still: the
host may swallow it entirely when a script exits via `SystemExit`, so treat
`exit_code` as the authoritative outcome and stderr as best-effort detail.

The script is responsible for its own persistence. A script that changes a
durable document should be followed by `inspect_document` or
`validate_document`, so the write is verified on the real file rather than
trusted from the exit code.
