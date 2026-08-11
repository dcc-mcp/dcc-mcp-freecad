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

- Python 3.7+
- `dcc-mcp-core` 0.19.91+
- FreeCAD 1.0 or newer with `FreeCADCmd`/`freecadcmd`

## Install

```bash
python -m pip install dcc-mcp-freecad
dcc-mcp-freecad
```

For development:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

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
