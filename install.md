# Installing dcc-mcp-freecad

This is the canonical standalone adapter runbook. Agents should read the
[raw file](https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-freecad/main/install.md)
before changing an installation.

## Requirements

- Python 3.7 or newer for the adapter service. This is the **wrapper** side
  only; FreeCAD runs its own, newer interpreter — see
  [Host Python interpreter](#host-python-interpreter).
- `dcc-mcp-core>=0.20.36` in the same Python environment.
- A FreeCAD version listed in the compatibility matrix below, with a working
  `FreeCADCmd`/`freecadcmd` executable.
- Existing directories for every entry in `DCC_MCP_FREECAD_ALLOWED_ROOTS`.

FreeCAD is an external OS-managed application. The adapter invokes only its
packaged typed driver; it does not accept arbitrary Python, macros, module
paths, or additional FreeCAD command-line flags.

## Supported versions

Host support is declared in the machine-readable matrix at
[`src/dcc_mcp_freecad/compat_matrix.json`](src/dcc_mcp_freecad/compat_matrix.json),
which the service and the in-FreeCAD driver both read:

| FreeCAD | Status | Real-machine evidence |
| --- | --- | --- |
| 1.0.x | supported | CI end-to-end run on 1.0.2 |
| 1.1.x | supported | CI end-to-end run on 1.1.4 |
| anything else | rejected | n/a |

Both pinned CI artifacts are Linux AppImages built against Python 3.11, so
3.11 is the host interpreter the matrix is currently verified against.

| Platform | FreeCAD installation | Discovery |
| --- | --- | --- |
| Windows | Official 64-bit installer or `winget install --id FreeCAD.FreeCAD --exact` | `PATH`, `Program Files`, or `--dcc-path` |
| macOS | Official application bundle or `brew install --cask freecad` | `PATH`, application bundle, or `--dcc-path` |
| Linux | Distribution package or a version-pinned official package managed by the operator | `PATH` or `--dcc-path` |

Distribution repositories can carry an older FreeCAD. The doctor launches the
discovered executable through the packaged status driver and reports the actual
version plus a `host_matrix` verdict instead of trusting the package name or
install path. A version outside the matrix fails the preflight
(`failure_stage: host_version`) with an `error_code`:

| `error_code` | Meaning |
| --- | --- |
| `freecad_host_version_unsupported` | Below the covered ranges; upgrade FreeCAD |
| `freecad_host_version_unverified` | Newer than every verified range |
| `freecad_host_version_unlisted` | Inside the covered span but not in any declared range |
| `freecad_host_version_unparsable` | The executable did not report a usable version |

An undeclared version is never treated as "good enough". Add and verify a new
range in `compat_matrix.json` before running on it.

### Why an unverified host is refused

FreeCAD moved host API between 1.0 and 1.1 (for example the Sketcher
`Symmetric` → `Midplane` rename, the removal of `ExternalGeometryCount`, and
`MeshPart` tessellation deflection changes). Those moves surface as tools that
report success while the geometry silently stays wrong, so the adapter refuses
to start unverified rather than guessing. Declared breaks are listed per version
in `checks.runtime.host_matrix.breaking_changes`, and every run against a real
host records `api_probe` evidence next to them.

## Host Python interpreter

`dcc-mcp-freecad` runs in two separate Python interpreters:

| Side | Runs | Python version |
| --- | --- | --- |
| Wrapper | the `dcc-mcp-freecad` CLI, server, and `doctor`, plus the process that launches `FreeCADCmd` | 3.7 or newer, from the environment the wheel was installed into |
| Host | `freecad_driver.py` and every skill script, inside FreeCAD | the interpreter FreeCAD ships: 3.10 or newer (3.11 in the verified CI runs) |

They do not share `sys.path` or site-packages. Installing the wheel into the
wrapper environment does not make `import FreeCAD` work there, and installing it
into FreeCAD's interpreter is neither required nor supported. The wrapper reaches
the host only by launching `FreeCADCmd` with the packaged driver, so the
wrapper's Python version never constrains the host's.

### Confirm the host interpreter version

The authoritative value is what FreeCAD itself reports. `doctor` reads it from
inside FreeCAD, so one call gives you both host versions:

```text
dcc-mcp-freecad doctor --json
```

Read `checks.runtime.python_version` for the host interpreter and
`checks.runtime.freecad_version` for the host application.

To ask FreeCAD directly, run the executable in console mode with a one-line
Python argument. `FreeCADCmd` executes a positional argument that is not an
existing file as Python code, so this prints the interpreter the driver will
actually run on:

```bash
FreeCADCmd -c "import sys; print(sys.version)"
```

```powershell
& "C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe" -c "import sys; print(sys.version)"
```

For the wrapper side, run `python -V` in the environment that owns the
`dcc-mcp-freecad` command. Confirm the host interpreter whenever
`DCC_MCP_FREECAD_EXECUTABLE` or `--dcc-path` points at a non-default install, or
when several FreeCAD builds are installed side by side.

### Version mismatch symptoms

The two interpreters do not negotiate a version — the bridge launches
`FreeCADCmd` and never imports FreeCAD into the wrapper. Every symptom that
looks like a wrapper/host version mismatch is therefore one of these:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'FreeCAD'` in the wrapper environment | importing FreeCAD from the wrapper, which never has it | drive FreeCAD through the adapter instead of importing it |
| the driver fails on a package that is installed in the wrapper | the host interpreter cannot see the wrapper's site-packages | the driver uses only FreeCAD's own modules; remove that dependency from the driver path |
| `checks.runtime.python_version` is older than expected | discovery selected a different FreeCAD build than intended | look for an earlier executable ahead on `PATH`, then pin with `DCC_MCP_FREECAD_EXECUTABLE` or `--dcc-path` and re-run `doctor --json` |
| syntax or stdlib errors raised inside the driver | the host interpreter is older than the driver assumes | upgrade FreeCAD to a version declared in `compat_matrix.json` |

`failure_stage` remains the routing key: an interpreter-level problem surfaces
as `runtime` (exit `40`) with the bounded reason FreeCAD reported, while a
version outside the matrix surfaces as `host_version` (exit `10`) with one of
the `freecad_host_version_*` codes listed above.

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

- `failure_stage: host`: no `FreeCADCmd`/`freecadcmd` was found. Install a
  FreeCAD version covered by `compat_matrix.json` (1.0.x or 1.1.x) or pass the
  exact executable with `--dcc-path`.
- `failure_stage: host_version`: the discovered FreeCAD is outside the
  compatibility matrix. Read `error_code` and
  `checks.runtime.host_matrix.supported_ranges`, then install or pin a covered
  version and check for a stale earlier executable on `PATH`.
- `failure_stage: core`: upgrade `dcc-mcp-core` in the same environment that
  provides the `dcc-mcp-freecad` command.
- `failure_stage: configuration`: ensure allowed roots exist and the document
  size/timeout environment variables contain positive numbers.
- `failure_stage: runtime`: run the emitted retry command and inspect the
  bounded reason. FreeCAD licensing/startup errors and a broken Python runtime
  must be repaired in the owning FreeCAD installation.
- Host interpreter looks wrong: compare `checks.runtime.python_version` from
  `dcc-mcp-freecad doctor --json` with the `FreeCADCmd -c "import sys; ..."`
  one-liner in [Host Python interpreter](#host-python-interpreter). If they
  disagree, discovery is picking a different build than you tested — pin it with
  `DCC_MCP_FREECAD_EXECUTABLE` or `--dcc-path`.
- Documents rejected as outside the workspace: add only the required existing
  root to `DCC_MCP_FREECAD_ALLOWED_ROOTS`; do not broaden it to an entire drive.
- Timeouts: raise `DCC_MCP_FREECAD_MAX_TIMEOUT_SECS` deliberately, then pass a
  bounded tool timeout. Cancellation terminates the adapter-owned process.
