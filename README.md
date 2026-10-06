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
- Add and update boxes, cylinders, spheres, cones, and tori with typed
  dimensions and placements.
- Create parametric union, cut, and intersection features.
- Import or export STEP, IGES, BREP, STL, and OBJ geometry.
- Fillet and chamfer edges on an existing solid, with a feasibility pre-check
  that refuses a radius or distance the adjacent faces cannot absorb.
- Build linear and polar patterns (up to 1000 instances) and mirror a solid
  across a base plane.
- Return object links, placements, shape/mesh topology counts, volume, area,
  bounding boxes, file size, and SHA-256 provenance.

No arbitrary Python, macros, module paths, or FreeCAD command-line flags are
accepted. The adapter invokes only its packaged method-dispatch driver.

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
another restore. The store lives inside `DCC_MCP_FREECAD_ALLOWED_ROOTS` and is
capped; when it is full the call fails with `snapshot_limit_exceeded` and
cleanup guidance instead of evicting anything. See
[docs/snapshots.md](docs/snapshots.md).

## Agent workflow

```bash
dcc-mcp-cli list
dcc-mcp-cli search --query "FreeCAD create boolean export STEP"
dcc-mcp-cli load-skill freecad-session --dcc-type freecad --instance-id <instance-short>
dcc-mcp-cli load-skill freecad-modeling --dcc-type freecad --instance-id <instance-short>
dcc-mcp-cli load-skill freecad-modify --dcc-type freecad --instance-id <instance-short>
```

Typical sequence: `create_document` → `add_primitive` → `transform_object` →
`boolean_operation` → `fillet_edges` → `linear_pattern` → `validate_document` →
`export_geometry`.

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
