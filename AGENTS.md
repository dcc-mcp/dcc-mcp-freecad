# AGENTS.md — dcc-mcp-freecad

> Navigation map for AI agents, not a reference manual. Detailed API lives in
> `README.md`; install and lifecycle in `install.md`. **This file is the only
> agent contract file at the repository root** — `CLAUDE.md` / `GEMINI.md` /
> `COPILOT.md` / `CODEBUDDY.md` / `CURSOR.md` and `.cursorrules` are deliberately
> absent; agents looking for a vendor-named entry point should read this file.

## Build & test

There is no `justfile` or `vx.toml` in this repo — use the commands CI runs:

```bash
python -m pip install -e ".[test]"          # editable install with the test extra
python -m pytest -m "not freecad"           # host-free suite (default local run)
python -m pytest tests/test_doctor.py -q    # doctor contract smoke
python -m ruff check src tests              # lint
python -m ruff format --check src tests     # format check
python -m build                             # build wheel + sdist
python -m twine check dist/*                # distribution metadata check
```

Use the `dev` extra (`python -m pip install -e ".[dev]"`) when running the lint,
build, and distribution gates — CI uses `test` for the pytest matrix and `dev`
for lint plus packaging.

- **Python:** `>=3.7`. The CI matrix covers 3.7–3.12, so keep syntax and
  dependencies importable on 3.7 unless a job explicitly excludes it.
- **Core pin:** `dcc-mcp-core>=0.20.36,<1.0.0`. Do not widen or lower this
  without running `tests/test_doctor.py`.

### Real-host lanes (gated, not run by default)

FreeCAD tests are behind markers and are skipped unless a real host is present:

```bash
python -m pytest -m "freecad and not freecad_gui" -v --junitxml=freecad-real.xml
python -m pytest -m freecad_gui -v --junitxml=freecad-gui.xml
```

Both lanes install a real FreeCAD first via `bash .github/scripts/install-freecad.sh`,
then assert the run actually measured something with
`python3 .github/scripts/verify-freecad-run.py <junitxml> <expected-count>`.
The expected counts are asserted in `.github/workflows/ci.yml` (37 for the
headless lane, 38 for the GUI lane): a run that collects fewer tests than the
count fails instead of reporting a green skip. Never lower those numbers to make
a lane pass, and when a lane gains tests, raise the count in the same change --
`ci.yml` is the source of truth, so a count here that disagrees with it is
drift.

The GUI lane additionally needs a headless GL stack. `QT_QPA_PLATFORM=offscreen`
is not enough on its own: the 3D view still asks for an OpenGL context, so on a
runner it dies with `QOpenGLWidget: Failed to create context`. CI installs Mesa's
software rasteriser plus Xvfb and runs the lane under `xvfb-run -a`; a local run
on Linux needs the same packages.

## Repo layout

| Path | Role |
|---|---|
| `src/dcc_mcp_freecad/` | Adapter package — `server.py`, `cli.py`, `doctor.py`, `bridge.py`, `freecad_driver.py`, `module_runner.py`, `skill_tools.py`, `write_contract.py`, `__version__.py` |
| `src/dcc_mcp_freecad/skills/` | Shipped skills; shipped as wheel artifacts |
| `src/dcc_mcp_freecad/compat_matrix.json` | Host compatibility matrix (`1.0.x` / `1.1.x`) |
| `tests/` | pytest suite; host-free by default, FreeCAD lanes behind markers |
| `docs/` | Human-readable guides |
| `install.md` | Wheel-first install and lifecycle guide |

## Write-after-read contract

Tools that mutate a FreeCAD document read the state back and compare before
returning. A mismatch is raised as an explicit error rather than a silent
success. When adding a write tool, follow `write_contract.py` instead of
returning the requested value directly.

## Release

- release-please drives versioning from Conventional Commits on `main`.
- `chore:` / `ci:` / `style:` / `refactor:` / `test:` / `build:` are
  `hidden: true`; `docs:` is a visible `Documentation` section. If every commit
  in a batch is hidden, release-please skips the batch entirely — no release PR
  and no version bump.

## Agent control path

AI agent runtimes reach this adapter through the shared gateway and the
`dcc-mcp` skill, not by importing the package directly:

```bash
dcc-mcp-cli search --query "<task>" --dcc-type freecad
dcc-mcp-cli describe <tool-slug>
dcc-mcp-cli call <tool-slug> --json '{"key":"value"}'
```

Adapter-local Python start APIs exist for host bootstrap and tests. Keep an
official CLI current with `dcc-mcp-cli update check` / `dcc-mcp-cli update apply`
(obtain user consent before installing anything).
