# dcc-mcp-freecad

<p align="center">
  <img src="docs/assets/dcc-mcp-freecad.svg" alt="DCC-MCP · FREECAD" width="600">
</p>

Production FreeCAD adapter for typed, durable parametric modeling and CAD
exchange through DCC-MCP.

![Parametric bracket moving through FreeCAD topology validation to a game-ready mesh](docs/images/dcc-mcp-freecad-showcase.webp)

_Illustrative workflow based on the live OpenSCAD → FreeCAD → Blender/Godot acceptance run; generated source is retained in `docs/images/dcc-mcp-freecad-showcase-source.png`._

<!-- dcc-mcp-coverage-pointer:start -->
<!-- Generated from dcc-mcp-catalog.yml by scripts/generate_adapter_pointer.py in dcc-mcp/dcc-mcp-core. Do not edit by hand. -->
## Part of the DCC-MCP host matrix

**dcc-mcp-freecad** — FreeCAD adapter for typed parametric modeling, document
validation, and CAD exchange.

It is one of **38 host adapters** in the DCC-MCP catalog. Every adapter speaks the same
MCP protocol and builds on the same core runtime contract; each one exposes the tools
its own host needs on top of that.

- [All host adapters and install metadata](https://dcc-mcp.github.io/ecosystem)
- [Host matrix on the core README](https://github.com/dcc-mcp/dcc-mcp-core#readme)
- [Showcase](https://dcc-mcp.github.io/showcase)

This block is generated from the catalog entry in
[`dcc-mcp-catalog.yml`](https://github.com/dcc-mcp/dcc-mcp-core/blob/main/dcc-mcp-catalog.yml).
Re-run the generator after changing the catalog.
<!-- dcc-mcp-coverage-pointer:end -->

## Capabilities

- Detect FreeCADCmd and report the actual FreeCAD/Python runtime.
- Create, inspect, recompute, validate, copy, and dependency-safely edit FCStd
  documents.
- Render a document view to a PNG headlessly and prove the frame is not a waste
  image before returning it.
- Add and update boxes, cylinders, spheres, cones, tori, wedges, and helices
  with typed dimensions and placements.
- Scale, copy, and mirror objects into a new object named by the caller.
- Create parametric union, cut, and intersection features.
- Import STEP, IGES, BREP, STL, and OBJ geometry, and export those plus 3MF.
- Fillet and chamfer edges on an existing solid, with a feasibility pre-check
  that refuses a radius or distance the adjacent faces cannot absorb.
- Build linear and polar patterns (up to 1000 instances) and mirror a solid
  across a base plane.
- Return object links, placements, shape/mesh topology counts, volume, area,
  bounding boxes, file size, and SHA-256 provenance.

No inline Python, module paths, or FreeCAD command-line flags are accepted, and
the typed tools dispatch only through the packaged method-dispatch driver.

The single exception is `run_script`, an escape hatch for work no typed tool
covers. It accepts a **path to a `.py` file** — never source text — resolves it,
requires it to lie inside `DCC_MCP_FREECAD_ALLOWED_ROOTS`, and runs it in a
disposable FreeCADCmd child started with `--safe-mode` and a throwaway user
config, so no user workbench, plugin, or macro is loaded. **It is not a
sandbox**: the script runs as the operator's account with that account's full
privileges, and allowed roots constrain only which file may be named. Its own
timeout ceiling (`DCC_MCP_FREECAD_MAX_SCRIPT_TIMEOUT_SECS`, 300s by default) is
far below the 1800s document ceiling, so a script that hangs is killed in
minutes. There is no script management surface.

## Requirements

This adapter spans **two separate Python interpreters**. They have different
versions, different `sys.path`, and different site-packages:

| Side | What runs there | Python version | Who owns that version |
| --- | --- | --- | --- |
| Wrapper (service side) | the `dcc-mcp-freecad` CLI, server, and `doctor`, plus the process that launches `FreeCADCmd` | **3.7+** (`requires-python = ">=3.7"`) | the environment you `pip install dcc-mcp-freecad` into |
| Host (FreeCAD side) | `src/dcc_mcp_freecad/freecad_driver.py` and every skill script | **3.10+** — the interpreter FreeCAD is built against and ships (3.11 in both release lines CI exercises) | your FreeCAD installation |

- `dcc-mcp-core` 0.20.36+ in the **wrapper** environment.
- A FreeCAD version covered by the compatibility matrix below, with either a working
  `FreeCADCmd`/`freecadcmd` or an explicitly configured compatible executable Python
  interpreter and installed native FreeCAD library for the opt-in `python-module`
  backend. See [native-library configuration](docs/python-module-backend.md).

Because the two interpreters are separate:

- **Do not create a Python 3.7 virtualenv and expect `import FreeCAD` to work in
  it.** The wrapper runs fine on 3.7, but FreeCAD's modules exist only inside
  FreeCAD's own Python, which is 3.10 or newer in every supported release.
- Installing the wheel into FreeCAD's interpreter is neither required nor
  supported; the wrapper drives the host by launching `FreeCADCmd` with the
  packaged driver.
- `dcc-mcp-freecad doctor --json` reports the host interpreter as
  `checks.runtime.python_version`, next to `checks.runtime.freecad_version`. See
  [install.md](install.md#host-python-interpreter) for how to read and
  cross-check both.

### FreeCAD compatibility matrix

Supported host versions are declared in
[`compat_matrix.json`](src/dcc_mcp_freecad/compat_matrix.json) — that file is the
contract, this section only points at it:

| FreeCAD | Status |
| --- | --- |
| 1.0.x | supported |
| 1.1.x | supported |
| anything else | rejected with an explicit error code |

Both supported lines are exercised end-to-end on real FreeCAD in CI —
**1.0.2** and **1.1.4**, the two pinned AppImages in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) — and both ship Python
3.11 as the host interpreter. A version outside the matrix is **not** silently
downgraded to "probably fine":
`dcc-mcp-freecad doctor --json` fails with an `error_code`
(`freecad_host_version_unsupported`, `freecad_host_version_unverified`,
`freecad_host_version_unlisted`, or `freecad_host_version_unparsable`), the
supported ranges, and the command that pins a supported one. The same matrix is
re-checked inside FreeCAD before any geometry work, so a skipped preflight does
not turn into a silent modelling bug.

## Install

Install the published wheel, prove the standalone FreeCAD runtime, then start
the service:

```text
python -m pip install --upgrade dcc-mcp-freecad
dcc-mcp-freecad doctor --json
dcc-mcp-freecad verify --json
dcc-mcp-freecad
```

See [install.md](install.md) for Windows, macOS, and Linux discovery, stable
doctor exits, configuration, wheel upgrades, uninstall, and troubleshooting.
FreeCAD remains OS/package-manager owned; the adapter does not download or
cache external binaries.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `DCC_MCP_FREECAD_EXECUTABLE` | Exact FreeCADCmd executable or install directory | `PATH` and standard locations |
| `DCC_MCP_FREECAD_ALLOWED_ROOTS` | `os.pathsep`-separated document/import/export roots | server working directory |
| `DCC_MCP_FREECAD_MAX_DOCUMENT_BYTES` | Maximum FCStd input size | 2 GiB |
| `DCC_MCP_FREECAD_MAX_TIMEOUT_SECS` | Maximum per-call deadline | 1800 seconds |
| `DCC_MCP_FREECAD_SNAPSHOT_DIR` | Snapshot store; must resolve inside allowed roots | `<first allowed root>/.dcc-mcp-freecad/snapshots` |
| `DCC_MCP_FREECAD_MAX_SNAPSHOTS` | Maximum stored snapshots | 50 |
| `DCC_MCP_FREECAD_MAX_SNAPSHOT_BYTES` | Maximum total snapshot store size | 1 GiB |
| `DCC_MCP_FREECAD_PORT` | Fixed adapter port when direct addressing is required | OS-assigned |
| `DCC_MCP_FREECAD_PARTS_LIBRARY` | `os.pathsep`-separated local standard-parts library directories | none — `list_parts` reports `parts_library_unavailable` |

The service is a standalone DCC-MCP instance with no GUI PID. Every operation
launches a clean FreeCADCmd process in safe mode with a temporary user config.

## Recoverability

Every call is a process-level commit, so there is no in-process undo stack to
rewind. Recoverability is explicit instead: `create_snapshot` copies a
document's bytes into the snapshot store, and `restore_snapshot` puts them back.

```text
create_snapshot  {document_path, label?}               -> snapshot_id, document_sha256
list_snapshots   {document_path?}                      -> snapshots, limits, orphan report
restore_snapshot {document_path, snapshot_id, expected_sha256?}
                                                       -> restored_sha256, undo_snapshot_id
delete_snapshot  {snapshot_id}                         -> frees one slot
```

Snapshots are byte copies, so they never spawn FreeCAD and keep working when
the host is unavailable. Passing `expected_sha256` makes a restore refuse
rather than overwrite work done after the caller read the document, and a
restore always snapshots the state it replaces — so a mistaken restore is just
another restore. A deleted document is recreated from a snapshot rather than
refused, with `undo_snapshot_id` null because there was no state to preserve.
The store lives inside `DCC_MCP_FREECAD_ALLOWED_ROOTS` and is capped; when it
is full the call fails with `snapshot_limit_exceeded` and cleanup guidance
instead of evicting anything. See [docs/snapshots.md](docs/snapshots.md).

## Agent workflow

```bash
dcc-mcp-cli list
dcc-mcp-cli search --query "FreeCAD create boolean export STEP"
dcc-mcp-cli load-skill freecad-session --dcc-type freecad --instance-id <instance-short>
dcc-mcp-cli load-skill freecad-sketch --dcc-type freecad --instance-id <instance-short>
dcc-mcp-cli load-skill freecad-modeling --dcc-type freecad --instance-id <instance-short>
dcc-mcp-cli load-skill freecad-modify --dcc-type freecad --instance-id <instance-short>
dcc-mcp-cli load-skill freecad-parts --dcc-type freecad --instance-id <instance-short>
```

Typical sequence: `create_document` → `add_primitive` → `transform_object` →
`boolean_operation` → `fillet_edges` → `linear_pattern` → `validate_document` →
`export_geometry`.

### Standard parts

`freecad-parts` adds `list_parts` and `insert_part`. The library is a set of
local directories configured through `DCC_MCP_FREECAD_PARTS_LIBRARY`:

```bash
dcc-mcp-cli call list_parts --category Fasteners --query M8
dcc-mcp-cli call insert_part --document_path projects/enclosure.FCStd \
  --part_ref fasteners/iso4014-m8x40.step --object_name BoltM8x40 \
  --translation '[10, 20, 0]'
```

The adapter is offline-only: it never downloads, extracts, or caches a library,
and writes nothing outside the directories the operator named. `part_ref` is
the relative path `list_parts` returned and nothing else — references
containing `..`, absolute paths, backslashes, URL schemes, and anything that
resolves outside a library root (including through a symlink) are refused
before a file is read, each with a stable error code. Follow an insert with
`validate_document`.

The parametric path starts with a sketch instead:
`create_sketch` → `add_sketch_geometry` → `add_sketch_constraint` →
`get_sketch_info`. `get_sketch_info` reports `dof` and `feature_ready`, and a
sketch with unconstrained degrees of freedom is never reported as usable for a
feature — the adapter accepts no arbitrary Python, so there is no way to repair
a silently under-constrained profile afterwards.

`scale_object`, `copy_object`, and `mirror_object` always write to a **new**
object named by the caller. FreeCAD accepts a `Shape` assignment on a parametric
primitive and then ignores it, so an in-place variant would report success while
the geometry stayed the same. A copy of a primitive keeps its parametric type; a
scaled result becomes a plain `Part::Feature`; a mirror that keeps its source
stays a live `Part::Mirroring` linked to it, and one that drops the source bakes
the geometry and removes it. Every 3MF export is asserted to declare the
millimetre unit, because a consumer scales the model by that declaration.

## Safety contract

- Requested documents and geometry stay under configured allowed roots.
- Existing outputs require explicit `overwrite=true`.
- Every `save_copy` call with `overwrite=false`, plain or presentation,
  publishes exclusively: a sibling hard link where the output filesystem
  supports one, otherwise an exclusive create (`O_CREAT|O_EXCL`) plus copy on
  volumes without hard-link support (FAT/exFAT, some SMB/FUSE mounts). Both
  refuse a destination created during the native call; only the hard link is a
  single atomic inode commit. Publication fails if neither is available —
  retry with `overwrite=true` or publish to a filesystem with hard-link
  support. Explicit overwrite uses atomic replacement.
- FCStd mutations happen on sibling staging copies and replace the original
  only after a successful, non-empty save.
- Failed mutations leave the original document byte-for-byte unchanged.
- Object names, dimensional fields, placements, formats, tessellation, result
  size, process output, and deadlines are bounded.
- Cancellation and timeouts terminate the owned FreeCADCmd process.
- Cascade removal is explicit and reports every removed dependent.
- Parts-library references are resolved and containment-checked against the
  configured roots before anything is read; a caller never names a file.

FreeCAD Python API reference: <https://www.freecad.org/api/>

## Optional native-library backend

The default is still FreeCADCmd. A separately configured `python-module` backend runs the same typed driver in a fresh compatible interpreter and installed FreeCAD library, with isolated temporary user directories. It is opt-in and has no automatic fallback. See [configuration and exact qualification](docs/python-module-backend.md).

### Native presentation copies

`save_copy` accepts an optional bounded `visible_objects` list and a standard
`view` (`isometric`, `front`, `top`, `right`). With this explicit opt-in it uses
FreeCAD's installed GUI library to save view-provider visibility and an
orthographic camera. Selections must be top-level non-container objects;
Groups, LinkGroups, Parts, Bodies and their members are rejected before changing visibility because a
hidden parent can hide or override a child's local state. Other objects remain
editable but hidden. The original
file stays unchanged; presentation mode refuses replacing the source even when
overwrite is set. GUI-library availability is required; this is not a claim of
offscreen OpenGL rendering.

The isolated native process disables view animation before selecting the camera,
verifies visibility, orthographic type and native quaternion orientation against
the request, then saves and reopens its staging file. It verifies camera values
(single-precision serialization tolerance of 1e-6), object names, links, topology
counts, placement, volumes and analytic bounds (double precision tolerance 1e-10 relative,
1e-9 absolute). Analytic bounds explicitly exclude GUI triangulation caches.
A cancellation check precedes final publication. No FCStd XML or binary data is
rewritten outside native FreeCAD APIs. Native raster thumbnails are disabled in
this isolated process because an offscreen host may lack an OpenGL context.

No-overwrite copy publication uses a sibling hard link to publish the complete
native file without replacing a destination created during the native call;
the output filesystem must support this operation. Where it does not, the same
exclusive publication falls back to an exclusive create plus copy, which keeps
the refusal but not the single atomic inode commit; a failure names hard links
and the `overwrite=true` escape hatch. Explicit overwrite continues to use
atomic replacement. These readbacks cover recorded geometry metrics,
not full BRep or mesh connectivity equivalence. Source qualification and the
supported-host GUI persistence gate are recorded in
[presentation-copy validation](docs/validation/presentation-copy.md).

Optional `appearances` entries contain only `object_name`, three `rgb` numbers,
and `opacity`. Each number must be finite and in `[0, 1]`; names must be unique
members of `visible_objects` with non-null top-level Part features and initially
uniform native face RGB and transparency. FreeCAD's integer-percent transparency rounds
the requested opacity to 1% resolution (transparency ties round upward). The
response reports that applied value. RGB is also normalized to the native
8-bit-per-channel save format using float32 scaling and nearest-integer rounding
with ties upward. The response retains original values in `presentation_request`
and reports applied colors/opacity in `presentation.appearances`. All entries are validated before any
view-provider changes; scalar properties and every native face material are
checked before saving and after reopening. Only named parts change appearance.

Optional `frame_margin` is a finite fraction in `[0, 1]` added at each side of
the fitted orthographic camera height: final height is
`fitted_height * (1 + 2 * frame_margin)`. It does not guarantee pixel padding or
fix the capture aspect ratio. Omitting it preserves the existing fit behavior.
Both options require an explicit `visible_objects` selection. These controls
save native presentation state; they do not render an image or add a material
execution interface. See the [appearance qualification gate](docs/validation/appearance-copy.md)
for the source evidence and native tests still required for this enhancement.

### Headless render snapshots

`render_view` captures a document view to a PNG so an agent can see what it
built instead of reading coordinates blind. It runs in the same isolated
process-per-call architecture as every other tool, so none of the
long-lived-GUI-thread failure modes apply.

**The image is not returned by default.** `include_image` defaults to `false`:
the default response is text — whether the render is usable, the pixel summary,
the view, and what was in frame. Asking for the PNG on every call is how a
visual-feedback feature becomes a context-cost problem.

**The frame is verified before it is returned, and a waste frame is refused
rather than handed back.** Two detectors, because there are two failure modes:

| Failure | Error code | Refused when |
| --- | --- | --- |
| capture never reached the scene (flat fill) | `degenerate_render` | one luminance level, or luminance spread below 1.0, or one colour covering more than 99.9% of the frame |
| background rendered, model did not | `empty_render` | fewer than 0.5% of pixels differ from the empty scene at the same camera |

The second detector exists because **FreeCAD's default 3D-view background is a
linear gradient and it is baked into the saved PNG**, so an entirely empty
render already has plenty of pixel variance and would pass any "is it
monochrome?" check. Every render is therefore captured twice — once as
requested, once with every object hidden at the identical camera — and the pair
is compared. Comparing against the empty scene is also a non-background pixel
share measurement that needs no knowledge of what the background is.

Both failures carry the full pixel summary, both captures' statistics, the
measured fraction, the reasons, and remediation. Nothing is published to
`output_path` unless the verdict passes.

**The render is read-only with respect to the document.** Framing the scene
means moving the camera and changing visibility, so the native camera,
visibility and selection are recorded first and restored afterwards — including
when the capture fails, because the restore runs in a `finally`. That
restoration is itself a byte-exact read-back check
(`render.view_state_restored`): `getCamera()` round-trips byte for byte through
`setCamera()`, so the comparison has no tolerance and any difference is a real
move. Both snapshots are returned as `view_state_before` and
`view_state_after`, so the claim is visible rather than asserted in a comment.
The document is never saved, and the source file stays byte-for-byte unchanged.

Bounds and behaviour:

- `width`/`height` default and cap at 1280×720, minimum 16×16.
- `visible_objects` is optional; omitted selects every top-level non-container
  object, using the same container rejection `save_copy` applies.
- `view` is `isometric`, `front`, `top` or `right`.
- `appearances` and `frame_margin` work here as they do in `save_copy`, change
  only this render, and require an explicit `visible_objects`.
- `output_path` is optional and must end with `.png` inside the allowed roots.
  Omitted means render, measure and discard — no artefact is left behind.
  Publication follows the same rules as `save_copy`, including `overwrite`.
- `include_image` attaches the PNG as base64, capped at 4 MiB; larger renders
  must be published with `output_path` and read as a file.

It requires the installed FreeCAD GUI library, the Qt offscreen platform and a
working software GL path. A host without `FreeCADGui` cannot render, and
`get_capabilities` reports `render_view` as `host_limited` with remediation
instead of letting the first call be the discovery. The adapter sets
`LIBGL_ALWAYS_SOFTWARE=1` on the child process (overridable) and disables
FreeCAD's notification area, which can deadlock under the offscreen platform.

See the [headless render qualification](docs/validation/render-view.md) record
for the host API facts this relies on, the evidence per layer, and what is
explicitly not claimed.
