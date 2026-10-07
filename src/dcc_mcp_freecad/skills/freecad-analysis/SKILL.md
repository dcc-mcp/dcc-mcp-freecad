---
name: freecad-analysis
description: >-
  Run a static structural (FEM) solve on a FreeCAD solid and report stresses and
  displacements with explicit, verified units. Use to answer "does this part
  survive this load"; load freecad-session and freecad-modeling first to create
  and repair the geometry.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+; FreeCAD FEM workbench with the CalculiX (ccx) solver and the Gmsh mesher"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.6.0"  # x-release-please-version
    tags: [freecad, fem, structural-analysis, calculix, units, verification]
    search-hint: >-
      FreeCAD FEM structural analysis run_fem_analysis stress displacement
      von Mises cantilever load units CalculiX
    tools: tools.yaml
---

# FreeCAD Analysis

Answer "will this part break under this load" without leaving the FreeCAD
document. `run_fem_analysis` builds or reuses a `Fem::FemAnalysis`, meshes it
with Gmsh, solves it with CalculiX, and returns the stress and displacement
extrema **with units attached and verified**.

Every solve is a read of your document: the document is staged into a fresh
solver work directory and only that copy is opened. The work directory is kept
and reported so a number can be audited afterwards.

## Units are part of the answer

A solver is unit-agnostic. It converges just as happily on a load expressed in
kN when you meant N, and the stress field still looks plausible. This skill
refuses that class of error:

- forces, moduli, densities and mesh sizes are passed as `{value, unit}`; a bare
  float is rejected, not assumed;
- the unit schema FreeCAD actually wrote is identified from the node
  coordinates of the generated solver input, never from a preference that may
  not match;
- the summed `*CLOAD` block of that input proves the force the solver was given
  is the force that was asked for;
- every returned quantity is `{"value": ..., "unit": ...}` in a canonical unit
  (`N`, `mm`, `MPa`).

If the schema cannot be identified, or the applied force does not match the
request, the call fails with a structured `error_code` instead of returning
numbers.

## Prerequisites

`run_fem_analysis` needs the FEM workbench plus two binaries FreeCAD does not
always ship: the CalculiX solver (`ccx`) and the Gmsh mesher (`gmsh`). Call
`get_capabilities` first: when they are missing the tool is reported as
`host_limited` with the exact install command, and a solve fails with
`fem_host_limited` rather than reporting success without having solved anything.

## Typical sequence

1. `list_faces` on the target solid — pick the faces to restrain and to load.
   Use a face whose `resolves` is `true`; sub-element naming moved between
   FreeCAD release lines and an unresolvable name is refused.
2. `run_fem_analysis` with `target_object`, `fixed_faces` and `load`.
3. Read `max_von_mises` and `max_displacement`, then compare against the
   material's allowable stress. `solver_workdir` holds the input, logs and
   results.

Pass `analysis_name` to re-solve an analysis the document already contains
instead of building one.
