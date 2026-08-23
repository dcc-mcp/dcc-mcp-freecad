# Installing dcc-mcp-freecad

This is the canonical standalone adapter runbook. Agents should read the
[raw file](https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-freecad/main/install.md)
before changing an installation.

## Requirements

- Python 3.7 or newer for the adapter service.
- `dcc-mcp-core>=0.19.91` in the same Python environment.
- FreeCAD 1.0 or newer with a working `FreeCADCmd`/`freecadcmd` executable.
- Existing directories for every entry in `DCC_MCP_FREECAD_ALLOWED_ROOTS`.

FreeCAD is an external OS-managed application. The adapter invokes only its
packaged typed driver; it does not accept arbitrary Python, macros, module
paths, or additional FreeCAD command-line flags.

## Supported versions

| Platform | FreeCAD installation | Discovery |
| --- | --- | --- |
| Windows | Official 64-bit installer or `winget install --id FreeCAD.FreeCAD --exact` | `PATH`, `Program Files`, or `--dcc-path` |
| macOS | Official application bundle or `brew install --cask freecad` | `PATH`, application bundle, or `--dcc-path` |
| Linux | Distribution package or a version-pinned official package managed by the operator | `PATH` or `--dcc-path` |

Distribution repositories can carry an older FreeCAD. The doctor launches the
discovered executable through the packaged status driver and rejects any
runtime below 1.0 instead of trusting the package name or install path.

## Agent quick path

Install the published wheel, then run the standalone preflight before starting
the service:

```text
python -m pip install --upgrade dcc-mcp-freecad
dcc-mcp-freecad doctor --json
dcc-mcp-freecad verify --json
dcc-mcp-freecad
```

Use `--dcc-path PATH` when discovery does not select the intended executable,
and `--timeout SECONDS` for a bounded runtime probe. `doctor` and `verify`
return the same safe standalone contract:

- exit `0`: configuration, Core, packaged driver, FreeCAD version, and runtime
  status all passed; `directly_usable` is true;
- exit `10`: executable, FreeCAD version, Core, or configuration preflight
  failed;
- exit `40`: FreeCAD was discovered but its packaged runtime status probe
  failed.

Every failure includes `failure_stage`, `failure_reason`, and a structured
`next_steps` command. Nothing is installed inside the FreeCAD GUI.

## Manual path

1. Install FreeCAD through the operating system, an operator-owned package
   manager, or a version-pinned official artifact whose checksum the operator
   verifies.
2. Install the `dcc-mcp-freecad` wheel into the service environment with pip.
3. Set `DCC_MCP_FREECAD_EXECUTABLE` or pass `--dcc-path` if `FreeCADCmd` is not
   on `PATH`.
4. Set `DCC_MCP_FREECAD_ALLOWED_ROOTS` to an `os.pathsep`-separated list of
   existing document/import/export roots when the working directory default is
   too narrow.
5. Run doctor and verify before starting the service.

The adapter never scrapes a latest-download page and has no adapter-managed binary cache.
FreeCAD upgrades and cleanup remain with the selected OS/package-manager
owner; the wheel contains only the bounded Python bridge and typed driver.

## Verify

```text
dcc-mcp-freecad doctor --json
dcc-mcp-freecad verify --json
```

A successful response reports the resolved executable, actual FreeCAD and
embedded Python versions, Core floor, configuration limits, and
`directly_usable: true`. This is a real `FreeCADCmd` status invocation, not a
path-only health claim. Start the DCC-MCP service only after exit `0`.

For release acceptance, a runner with an operator-provided
`FREECAD_TEST_EXECUTABLE` can additionally run the existing `freecad`-marked
modeling/exchange smoke. Ordinary CI does not claim a real host when that
binary is unavailable.

## Upgrade

Upgrade the adapter wheel and re-run verification:

```text
python -m pip install --upgrade dcc-mcp-freecad
dcc-mcp-freecad doctor --json
```

Upgrade FreeCAD separately with the same OS/package-manager owner used to
install it. Do not replace an externally managed executable from the adapter.
If multiple versions exist, pin discovery with `DCC_MCP_FREECAD_EXECUTABLE` or
`--dcc-path` and verify the reported runtime version.

## Uninstall

Stop the standalone `dcc-mcp-freecad` process, then remove the wheel:

```text
python -m pip uninstall dcc-mcp-freecad
```

There is no plugin, daemon registration, receipt, or adapter cache to remove.
FreeCAD and user documents are independent and are never removed by the
adapter uninstall.

## Troubleshooting

- `failure_stage: host`: install FreeCAD 1.0+ or pass the exact
  `FreeCADCmd` path with `--dcc-path`.
- `failure_stage: host_version`: the discovered executable is too old; upgrade
  it with its OS/package manager and check for a stale earlier executable on
  `PATH`.
- `failure_stage: core`: upgrade `dcc-mcp-core` in the same environment that
  provides the `dcc-mcp-freecad` command.
- `failure_stage: configuration`: ensure allowed roots exist and the document
  size/timeout environment variables contain positive numbers.
- `failure_stage: runtime`: run the emitted retry command and inspect the
  bounded reason. FreeCAD licensing/startup errors and a broken Python runtime
  must be repaired in the owning FreeCAD installation.
- Documents rejected as outside the workspace: add only the required existing
  root to `DCC_MCP_FREECAD_ALLOWED_ROOTS`; do not broaden it to an entire drive.
- Timeouts: raise `DCC_MCP_FREECAD_MAX_TIMEOUT_SECS` deliberately, then pass a
  bounded tool timeout. Cancellation terminates the adapter-owned process.
