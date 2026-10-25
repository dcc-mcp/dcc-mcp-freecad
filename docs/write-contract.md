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
| `import_geometry` | source file non-empty; object exists; mesh has points and facets, or shape is non-null, valid and finitely bounded; **and** the landed object matches the source file it was read from (solid count and volume for CAD formats; point/facet count and bounding-box containment for meshes) |
| `export_geometry` | artefact exists and is non-empty; **and** can be read back into the geometry it came from (solid count and volume for CAD formats; point/facet count and bounding-box containment for meshes) |
| `save_copy` | copy exists and is non-empty; copy reopens with the same object inventory |
| `create_document` | the document file exists and is non-empty |
| `create_sketch` | sketch exists; `TypeId` is `Sketcher::SketchObject`; `AttachmentSupport` names the datum plane the call created for the requested `plane`; the attachment mode is the flat-on-face mode; the sketch normal is the requested plane's normal; the sketch is inside the requested body |
| `add_sketch_geometry` | the geometry count grew by exactly the number of elements the batch expands to; every stored element's kind is the one requested; every element's key points equal the coordinates that were asked for |
| `add_sketch_constraint` | the constraint count grew by exactly the number of constraints in the batch; every stored `Type` is the one requested; a dimensional constraint's driving value equals the requested value (`angle` after the degrees-to-radians conversion) |
| `get_sketch_info` | read-only: reports geometry, constraints, remaining degrees of freedom and the feature-readiness verdict |

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

The same containment holds the other way round: a mesh imported from a file
cannot leave the envelope of the file it was read from, so `import_geometry`
compares the landed mesh against the source's box in exactly the same way.
Containment is a one-directional test, though -- a mesh that lost facets stays
inside the box -- so `import_geometry` also compares point and facet counts,
which are exact for a vertex format. Containment proves the import is not the
wrong or a larger geometry; the counts prove none of it went missing.

## Refusing an under-constrained sketch

A sketch carries a second kind of reported success that the rules above do not
cover: the write lands perfectly and the profile is still wrong, because the
solver is free to move whatever the constraints did not pin down. The sketch
reports as created, and the solid built from it differs on every run.

So degrees of freedom are *measured*, never assumed, and never read off the
solver's own return value. `solve()` is called so a sketch the solver rejects is
reported, but on FreeCAD 1.0.2 and 1.1.4 it returns `0` for any sketch that
solves -- including one measured at four degrees of freedom. Its return value is
read only as an error code (a negative value means the host committed no
solution), and the count itself comes from the sketch's own `DoF` property, with
`getDoF()` as the fallback spelling.

A count the host will not report stays `None`. That is the whole point: an
unmeasured sketch is reported as *not provably constrained* rather than as zero,
because reporting every sketch as fully constrained is the failure this section
exists to prevent.

The verdict is computed in one place, `sketch_rules.feature_state`, and every
sketch tool returns it:

```json
{
  "dof": 3,
  "dof_available": true,
  "geometry_count": 4,
  "fully_constrained": false,
  "feature_ready": false,
  "blocking_error_code": "E_SKETCH_UNDERCONSTRAINED",
  "blocking_reason": "The sketch has 3 unconstrained degree(s) of freedom, so a feature built on it is not reproducible across hosts."
}
```

`dof == 0` is not on its own sufficient: an empty sketch solves with zero freedom
and is still not a profile, so `feature_ready` also requires geometry.

**Reporting is not refusing.** `create_sketch`, `add_sketch_geometry` and
`add_sketch_constraint` report the verdict and never refuse on it. A sketch is
built one element at a time and every intermediate state legitimately has
freedom left, so gating those calls would make sketching impossible. The refusal
lives in the one call whose job is to answer "is this finished?":
`get_sketch_info` with `require_fully_constrained: true`, which routes the
verdict through `sketch_rules.assert_feature_ready` and raises instead of
returning.

The refusal is `dcc_mcp_freecad.sketch_rules.SketchStateError`. It crosses the
process boundary under its own `sketch_state` key -- so a caller can branch on
the `code` rather than parse a sentence that differs between host versions -- and
the bridge re-raises it as `dcc_mcp_freecad.SketchStateError`, a `BridgeError`,
so an existing `except BridgeError` handler keeps working.

```json
{
  "schema_version": 1,
  "code": "E_SKETCH_UNDERCONSTRAINED",
  "details": {
    "sketch_name": "Profile",
    "dof": 3,
    "geometry_count": 4,
    "fully_constrained": false
  }
}
```

Six codes reach a caller as `SketchStateError`. Four of them are the readiness
verdict from `feature_state` and all four mean the same thing: this profile is
not reproducible, do not build a feature on it. The other two are raised while a
constraint is resolved against live geometry, *before* anything is written.

| Code | Refused because | Gate |
|---|---|---|
| `E_SKETCH_UNDERCONSTRAINED` | `dof > 0`, or `dof == 0` on a sketch with no geometry | readiness |
| `E_SKETCH_OVERCONSTRAINED` | `dof < 0`: redundant or conflicting constraints | readiness |
| `E_SKETCH_DOF_UNAVAILABLE` | the host would not report a count at all | readiness |
| `E_SKETCH_SOLVER_FAILED` | the solver raised or did not converge | readiness |
| `E_SKETCH_ELEMENT_NOT_FOUND` | a constraint names an element the sketch does not have | reference |
| `E_SKETCH_SPEC_INVALID` | a constraint is put on geometry that cannot carry it, or its two references do not line up (different kinds for `equal`, a vertex that kind does not have) | spec |

Constraint references are validated against the live geometry list *before* the
first write of a batch, because the host adds a constraint on a missing element
without complaint and the sketch then solves as if it were not there. A
half-applied batch is a sketch that looks finished and is not, so the whole
batch is refused rather than partially applied.

**One sketch type does not survive the boundary.** `sketch_rules` refuses an
uninterpretable spec with `SketchSpecError` -- a rectangle with a negative width,
a circle with no radius, a coordinate that is not a number. That class carries
none of the three attributes the driver forwards across the process boundary
(`code`, `payload`, `state_payload`), so a refusal raised *inside* the FreeCAD
process arrives at the caller as a plain `BridgeError` with the message and no
code.

`dcc_mcp_freecad.SketchSpecError` is therefore exported for the **local** path:
`sketch_rules` is pure Python, so a caller can validate a spec before spending a
host round-trip and catch exactly this type. An `except` written against it does
not fire on a refusal that came back from the host; catch `BridgeError` for that,
or treat the spec as unvalidated.

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
