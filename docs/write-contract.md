# Post-write read-back contract

**A mutating tool returns only after it has read the target back and proven that this call's change is actually there.**

Everything else in this document is detail.

## The failure mode

*Reported success, model unchanged.* The tool returns an affirmative answer, the
caller keeps building on it, and the error surfaces steps later as an unrelated
symptom.

In an agent loop this is the most expensive bug class there is. An error that
raises is cheap: the caller sees it, and the loop can correct or stop. An error
that reports success is not an error the caller can act on, so it propagates
until it collides with something else, by which point the trace has gone cold.

The competitor failure that motivated this contract: a `pocket_sketch` tool that
returns `Valid` while never cutting the material, and a parameter that is read
at the wrong index and quietly yields `None`. Both are the same bug. Both are
what this contract makes impossible.

## The rules

For **every** tool that changes a document or writes a file:

1. **Read back before returning.** Re-read the target after the save and compare
   it against what was asked for. Reading the object you just wrote in memory is
   not a read-back; the value has to come back from the model.
2. **On disagreement, raise.** Never return `None`, never return a
   `valid: true` summary computed from the inputs, never report success when
   part of the change applied.
3. **Report both sides.** An error that says only "failed" makes the caller
   guess. `expected` and `actual` are what make it actionable.
4. **Attach the host version.** A read-back that disagrees is the classic
   signature of host API drift. Without the version the report is
   unreproducible.
5. **Reject what you do not understand.** A parameter that does not apply to the
   target is an error, not a no-op.

## What to assert

| Tool | Read-back |
|---|---|
| `add_primitive` | object exists; `TypeId` is the requested primitive; every requested dimension equals the requested value; placement is the requested transform; shape is non-null and valid |
| `update_primitive` | every requested dimension equals the requested value; label persisted; shape non-null and valid |
| `transform_object` | placement is the requested transform |
| `boolean_operation` | result exists with the right `TypeId`; `Base`/`Tool` are wired to the requested operands; shape non-null and valid; result volume relates to the operands the way the operation requires |
| `remove_object` | every name the call claims to have removed is gone |
| `import_geometry` | source file non-empty; object exists; mesh has points and facets, or shape is non-null, valid and finitely bounded |
| `export_geometry` | artefact exists and is non-empty; **and** can be read back into the geometry it came from (solid count and volume for CAD formats; point/facet count and bounding-box containment for meshes) |
| `save_copy` | copy exists and is non-empty; copy reopens with the same object inventory |
| `create_document` | the document file exists and is non-empty |
| `create_drawing_page` | page exists with the right `TypeId` and is wired to its template; the page lists exactly the views that were added; every view has the requested `TypeId`, sources, projection direction, scale and page position; and each view has actually projected something |
| `export_drawing` | artefact exists and is non-empty; **and** parses back into the page it came from (a PDF declares exactly one page; an SVG parses with an `svg` root element) |

Two of these deserve a note.

**Volume relations, not volume equality.** A boolean that does not overlap its
base legitimately changes nothing, so an assertion that the result *differs* from
its input would fire on correct work. What must always hold is the relation: a
union is never smaller than its largest operand, a cut is never larger than its
base, an intersection is never larger than its smallest operand. A result outside
its relation means the operation did not run.

**Bounding-box containment for meshes.** Tessellation only ever places vertices
on the surface it was given, so an exported mesh cannot leave its source's
bounding box. An export of the wrong object -- or of nothing -- shows up here as
a box that escaped. This catches "wrote a plausible-looking file" without
recomputing the source geometry.

**Parsing the artefact, not just stat-ing it.** A drawing export is the one
family of tools here whose output is a file no later call reads back into the
model, so `artifact.non_empty` alone would let a blank render through. The PDF
page count and the SVG root element are cheap to parse and are the difference
between "a file was written" and "the page was drawn".

## Comparing floats

Compare with a tolerance, but keep the tolerance far tighter than any
modelling-relevant delta. The default here is `rel_tol=1e-6, abs_tol=1e-9`,
which absorbs unit and serialisation noise without excusing a real difference.
CAD round-trips are looser by nature and declare it explicitly: STEP/IGES/BREP
volume comparison uses `1e-3` relative, and STEP still catches the wrong object
by orders of magnitude.

Compare transforms as transforms, not as stored fields. FreeCAD may store an
equivalent axis/angle pair, so comparing `Axis` and `Angle` field by field
rejects rotations that are the same rotation. Prefer the host's own `isSame`,
and fall back to applying the placement to probe vectors: two placements that
move every probe point to the same place are the same placement.

## The error

`dcc_mcp_freecad.write_contract.WriteVerificationError` carries a structured
payload that survives the process boundary between the adapter and the FreeCAD
host, and is re-raised on the caller's side as
`dcc_mcp_freecad.WriteVerificationError` (a `BridgeError`, so existing
`except BridgeError` handlers keep working).

```json
{
  "schema_version": 1,
  "tool": "model.update_primitive",
  "check": "dimension.Length",
  "expected": 84.0,
  "actual": 80.0,
  "host_version": "1.1.4",
  "host_matrix": {"status": "supported", "range_id": "1.1.x", "matrix_version": "2026-09-29"},
  "params": {"object_name": "Body", "dimensions": {"length": 84}},
  "remediation": "FreeCAD accepted the write but stored a different value..."
}
```

`check` is the name of the assertion that disagreed, so a caller can branch on
the kind of failure rather than parsing a sentence. `host_matrix` is the trimmed
compatibility-matrix verdict from the host compatibility work, which is what
tells you whether you are looking at drift or at a genuine modelling error.

Every path in the payload is a path the caller passed in, never an internal one.
An adapter that writes through a temporary file and renames it into place must
rewrite its own staging paths out of the report before raising: the temporary
file is gone by the time the caller reads the error, so leaving it in sends the
caller to inspect a file that no longer exists instead of recovering from the
mismatch it was told about.

The rendered message states the tool, the check, both values, and the host
version, in that order:

```
model.update_primitive did not take effect: the post-write read-back disagreed
on dimension.Length (expected 84.0, read back 80.0); host FreeCAD 1.1.4
(matrix status: supported). FreeCAD accepted the write but stored a different
value, so the geometry is not the geometry that was asked for.
```

## Porting this to another adapter

`src/dcc_mcp_freecad/write_contract.py` is plain stdlib and knows nothing about
FreeCAD. Copy it unchanged and supply two things:

1. **A classification of your method table** into `MUTATING_TOOLS` and
   `READ_ONLY_TOOLS`. Keep a test asserting the two lists partition your method
   table exactly -- adding a method then forces the author to answer "does this
   owe a read-back?" instead of leaving it undecided.
2. **A read-back helper per mutating tool** that calls `numbers_match` /
   `sequences_match` and raises `WriteVerificationError`.

If your adapter runs its tool code inside the host's interpreter and cannot
import its own package, load the contract module by path the way
`freecad_driver.py` does -- one source of truth, no duplicated copy.

## Testing it

Two lanes, and both are needed:

* **A fake host that stores what it is told.** Give it a "silently drop writes"
  mode and assert every mutating tool refuses to return. This is what proves the
  guard fires, it runs on every Python version in the matrix, and it needs no
  host installed.
* **The real host.** Run the full end-to-end path and assert it still succeeds.
  This is what proves the guard does *not* fire on correct work -- the half that
  a fake host can never show you.

A read-back contract that only has fake-host tests is a guard nobody trusts
enough to leave on. One that only has real-host tests has no evidence it would
ever catch anything.
