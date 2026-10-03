# dcc-mcp-freecad

<p align="center">
  <img src="docs/assets/dcc-mcp-freecad.svg" alt="DCC-MCP · FREECAD" width="600">
</p>

Production FreeCAD adapter for typed, durable parametric modeling and CAD
exchange through DCC-MCP.

![Parametric bracket moving through FreeCAD topology validation to a game-ready mesh](docs/images/dcc-mcp-freecad-showcase.webp)

_Illustrative workflow based on the live OpenSCAD → FreeCAD → Blender/Godot acceptance run; generated source is retained in `docs/images/dcc-mcp-freecad-showcase-source.png`._

## Capabilities

- Detect FreeCADCmd and report the actual FreeCAD/Python runtime.
- Create, inspect, recompute, validate, copy, and dependency-safely edit FCStd
  documents.
- Add and update boxes, cylinders, spheres, cones, and tori with typed
  dimensions and placements.
- Create parametric union, cut, and intersection features.
- Import or export STEP, IGES, BREP, STL, and OBJ geometry.
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
- A FreeCAD version covered by the compatibility matrix below, with a working
  `FreeCADCmd`/`freecadcmd`.

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
| `DCC_MCP_FREECAD_PORT` | Fixed adapter port when direct addressing is required | OS-assigned |

The service is a standalone DCC-MCP instance with no GUI PID. Every operation
launches a clean FreeCADCmd process in safe mode with a temporary user config.

## Agent workflow

```bash
dcc-mcp-cli list
dcc-mcp-cli search --query "FreeCAD create boolean export STEP"
dcc-mcp-cli load-skill freecad-session --dcc-type freecad --instance-id <instance-short>
dcc-mcp-cli load-skill freecad-modeling --dcc-type freecad --instance-id <instance-short>
```

Typical sequence: `create_document` → `add_primitive` → `transform_object` →
`boolean_operation` → `validate_document` → `export_geometry`.

## Safety contract

- Requested documents and geometry stay under configured allowed roots.
- Existing outputs require explicit `overwrite=true`.
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
