# Document snapshots and restore

Every modelling call in this adapter is a **process-level commit**: a
`FreeCADCmd` process starts, mutates a document, saves it, and exits. There is
no in-process undo stack to rewind, so a mistaken `boolean_operation` or
`remove_object` would otherwise be permanent.

Snapshots give that loop an explicit recovery path. They are **not** an undo
stack: nothing is captured automatically, and restoring does not replay or
invert operations. A snapshot is a byte copy of an `.FCStd` at a moment the
caller chose, and a restore puts those bytes back.

## Why these tools never spawn FreeCAD

An `.FCStd` is a self-contained archive, so a snapshot is a byte copy and a
restore is a byte replacement. No host call is involved, which buys three
things:

- snapshots keep working when FreeCAD is missing, broken, or mid-upgrade —
  exactly when recovery matters most;
- creating one is I/O-bound rather than host-bound, so it is cheap enough to
  take before every risky call;
- the integrity check is a SHA-256 over the bytes, which is strictly stronger
  than the object-level read-back a host round-trip could provide.

Because the document is never opened, `create_snapshot` also works on documents
the current host version could not open.

## Tools

| Tool | Input | Output |
| --- | --- | --- |
| `create_snapshot` | `document_path`, `label?` | `snapshot_id`, `document_sha256`, `bytes`, `created_at` |
| `list_snapshots` | `document_path?` | `snapshots`, `count`, `total_bytes`, limits, `orphans` |
| `restore_snapshot` | `document_path`, `snapshot_id`, `expected_sha256?` | `restored_sha256`, `replaced_document_sha256`, `undo_snapshot_id` |
| `delete_snapshot` | `snapshot_id` | remaining `snapshot_count` / `total_bytes` |

`label` is free text recorded in the sidecar; it is not an identity. Two
snapshots of identical content taken at different times get distinct ids.

## Store layout

The store is one directory of **pairs**, with no index file:

```text
<store>/20261007T021530.123456Z-a1b2c3d4e5f6.FCStd
<store>/20261007T021530.123456Z-a1b2c3d4e5f6.json
```

The filesystem is the only registry. An index would be one more thing able to
disagree with the bytes, and the failure class this module exists to prevent is
a report that disagrees with reality. **Both halves must exist for an entry to
be listed**, so an interrupted write leaves a visible orphan instead of a
snapshot that restores nothing.

The store lives inside `DCC_MCP_FREECAD_ALLOWED_ROOTS`. It holds verbatim
copies of the user's documents, so it is gated by the same `_within` check every
other path uses — a snapshot is a readable copy of the work, and writing it
somewhere the operator did not sanction would be an exfiltration path. The
default is `<first allowed root>/.dcc-mcp-freecad/snapshots`; override with
`DCC_MCP_FREECAD_SNAPSHOT_DIR`, which must also resolve inside allowed roots.

## Optimistic concurrency

`restore_snapshot` accepts `expected_sha256`. When given, the restore is
**refused** unless the document still hashes to that value:

```text
restore_snapshot(document_path, snapshot_id, expected_sha256=<sha the caller read>)
  -> snapshot_conflict, expected_sha256 and document_sha256 both reported
```

The check runs twice — once before the pre-restore copy and once after it.
There is no cross-process file lock here, so the second check is what turns
"we refuse stale restores" from a claim into a check: a writer that lands during
the copy is detected rather than silently overwritten. A writer that lands in
the final instant before `os.replace` is still lost. That residual window is
inherent to a process-per-call adapter with no lock server, and is documented
rather than pretended away.

Omitting `expected_sha256` restores unconditionally.

## A restore is itself undoable

`restore_snapshot` snapshots the state it is about to replace and returns that
id as `undo_snapshot_id`, labelled `Before restoring <id>` with
`origin: pre_restore` and `restored_snapshot_id` set. Restoring that id puts the
discarded work straight back, so:

```text
create_snapshot          -> S1
  (user keeps working)
restore_snapshot(S1)     -> document == S1, undo = U1
restore_snapshot(U1)     -> document == the working state again
restore_snapshot(S1)     -> document == S1 again
```

Restore therefore needs capacity for **one more snapshot**, and refuses when
the store cannot provide it. A restore the caller could not undo is worse than
no restore.

Restoring a snapshot taken from a different document is allowed — "make B look
like A" is a real workflow — and reported as `cross_document: true` with
`snapshot_document_path` set. It is never silent.

## Limits

| Variable | Default | Refusal |
| --- | --- | --- |
| `DCC_MCP_FREECAD_MAX_SNAPSHOTS` | 50 | `snapshot_limit_exceeded` |
| `DCC_MCP_FREECAD_MAX_SNAPSHOT_BYTES` | 1 GiB | `snapshot_limit_exceeded` |

A full store is **refused, never evicted**. Auto-deleting the oldest snapshot
would be the convenient choice and the wrong one: the oldest snapshot is often
the only known-good state, and dropping it to make room is the same class of
silent data loss snapshots exist to prevent. Every refusal carries
`snapshot_count` / `max_snapshots` (or the byte equivalents) and remediation
naming `list_snapshots` and `delete_snapshot`.

Orphaned files count against the byte limit but not the snapshot count. They
have no complete metadata, so `delete_snapshot` cannot remove them by id;
`list_snapshots` reports them under `orphans` and the remediation names the
directory.

## Error codes

| Code | Meaning |
| --- | --- |
| `snapshot_limit_exceeded` | Store is at a configured limit; nothing was evicted |
| `snapshot_conflict` | `expected_sha256` was stale, or the document changed mid-copy |
| `snapshot_not_found` | Unknown or malformed id (including traversal attempts) |
| `snapshot_content_mismatch` | Stored bytes no longer match the recorded SHA-256 |
| `snapshot_copy_timeout` | The copy exceeded the call deadline |
| `snapshot_store_outside_allowed_roots` | `DCC_MCP_FREECAD_SNAPSHOT_DIR` escapes allowed roots |

No refusal of these kinds modifies the document. Both conflict paths leave the
document byte-for-byte unchanged and discard the untrustworthy pre-restore copy.

## Verification

Snapshots honour the adapter's post-write read-back contract at the byte level:

- `create_snapshot` proves the entry exists at the size the copy produced. The
  content hash is not recomputed — the copy already hashed the exact bytes it
  wrote, and re-reading would double the I/O of the one operation a caller
  might run before every mutation.
- `restore_snapshot` re-reads the document after replacement and requires it to
  hash to the snapshot's recorded digest, raising a structured
  `WriteVerificationError` with expected and actual otherwise.

These tools are deliberately absent from
`write_contract.MUTATING_TOOLS`: that list classifies the native driver's method
table, and nothing here reaches the driver. The read-back obligation still
applies and is still enforced — it is just satisfied over bytes instead of
through `_METHODS`.
