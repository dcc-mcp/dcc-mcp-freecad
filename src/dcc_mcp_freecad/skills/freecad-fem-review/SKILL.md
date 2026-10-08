---
name: freecad-fem-review
description: >-
  Turn a FreeCAD FEM solve into an engineering verdict: safety factor, where the
  part is actually critical, and what to change next. Use after freecad-analysis
  has produced a solve; use freecad-analysis itself when all you need are the
  raw stress and displacement numbers.
license: MIT
compatibility: "Python 3.7+; FreeCAD 1.0.x or 1.1.x (see compat_matrix.json); dcc-mcp-core 0.20.36+; orchestration only, no additional host binaries beyond those freecad-analysis needs"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: freecad
    layer: domain
    version: "0.1.0"  # x-release-please-version
    tags: [freecad, fem, review, safety-factor, verdict, assumptions, interpretation]
    depends: [freecad-analysis]
    search-hint: >-
      FreeCAD FEM result review interpret stress safety factor verdict where is
      it critical assumption cantilever recommend thicken material
    recipes: RECIPES.yaml
    examplePrompts:
      - "This bracket was solved at 100 N — is it strong enough, or does it need
        to be thicker?"
      - "Where exactly is this beam most likely to fail, and what should I change
        about it?"
      - "Interpret the last FEM run on this part against a safety factor of 3 and
        tell me what you assumed."
      - "Compare this solve against aluminium instead of steel and say whether
        the material swap is safe."
    recovery:
      - next: freecad-analysis.run_fem_analysis
        note: >-
          A review with no solve to read has nothing to interpret. Run
          freecad-analysis first and pass its result payload in as fem_result.
      - next: freecad-analysis.list_faces
        note: >-
          A hot spot that cannot be named as an object plus a face or edge is an
          unresolved sub-element reference. Re-list the faces the host actually
          resolves and re-anchor the location before reporting it.
      - next: freecad-analysis.run_fem_analysis
        note: >-
          A verdict resting on an un-converged mesh is not a verdict. Re-solve at
          a finer mesh_size, confirm the extrema moved less than the convergence
          tolerance, then review again.
    undo: none
---

# FreeCAD FEM Review

`freecad-analysis` answers "what are the stresses". It does not answer "is this
part OK", and it is the second question a user actually asks. This skill is the
layer between the two: it reads a completed solve and returns **a verdict, a
location, a next action, and the assumptions the verdict is resting on**.

It adds no new typed tool. The whole skill is one orchestration recipe
(`fem-review`) over tools that already exist, which is the right shape for a
layer whose output is judgement rather than a deterministic number.

## The output is the contract

A review is not a summary of the solve; it is a new document with four required
parts. A review missing any of them is not a short review, it is a **failed**
review.

### 1. Verdict and safety factor

Compare `max_von_mises` against the allowable stress and report one of
`satisfied` / `marginal` / `violated`. The safety factor is
`allowable / max_von_mises` — never the reciprocal, and never rounded into a
different verdict band than the number supports.

The allowable stress comes from the target safety factor and a yield strength.
Yield strength is **user-supplied first**; when it is not supplied it comes from
the built-in table below, and the source is then part of the output.

### 2. Where, named

"Max von Mises is 240 MPa" is not a location. A review names the **object plus
the face or edge** the hot spot sits on, using the sub-element references
`list_faces` reported as resolvable. Two categories, both required:

- **Geometric hot spots** — re-entrant corners, the root of a cantilever, a
  hole edge, a section change. These are real and they survive mesh refinement.
- **Boundary-condition artefacts** — a fully restrained face, a point load, a
  sharp restraint corner. Nodal stress at a fully fixed face is extrapolated
  from element integration points and is the **least trustworthy number the
  solver produces**. It must be reported as an artefact, not as a design
  finding.

A review that reports only a global maximum has failed: the user cannot act on
a number with no address.

### 3. Next actions

At least one, and it must be executable — a dimension to change, a material to
switch to, or a stiffener position. "Consider optimising the design" is not an
action. Two is the usual count; more than three means the verdict was not
settled.

### 4. Assumptions, explicitly

This is the part that separates a review from a plausible story, and it is why
this skill exists rather than leaving the interpretation to each user's prompt.
A FEM result is the easiest thing in engineering to over-trust: it converges, it
looks precise, and it is quietly wrong when its premises are. **Every review
states its assumptions.** A review without an assumption block is rejected as
unusable, not returned as-is.

At minimum, declare:

- **Constraint idealisation** — a "fixed" face is perfectly rigid. Real
  mountings flex, which lowers the peak stress but raises deflection.
- **Mesh convergence** — whether the extrema were checked at more than one mesh
  size. Unchecked is a legitimate state and must be declared as unchecked,
  never implied to be converged.
- **Material provenance** — where the yield strength and modulus came from:
  user-supplied, or the built-in table with its basis.
- **Model class** — linear static, small displacement, linear elastic. Any
  yielding, contact, buckling or large-deflection behaviour is **outside** what
  the solve can see.

## Built-in material table

Used only when the user does not supply a material. **Every value below is a
generic textbook minimum, not a procurement specification** — when one is used
the review must say so and must tell the user to confirm against their mill
certificate or datasheet.

| Material | Yield strength | Young's modulus | Density |
| --- | --- | --- | --- |
| `DccMcpStructuralSteel` (default) | 250 MPa | 210000 MPa | 7.85e-09 t/mm^3 |
| `Aluminium6061T6` | 276 MPa | 69000 MPa | 2.70e-09 t/mm^3 |
| `Aluminium7075T6` | 503 MPa | 71700 MPa | 2.81e-09 t/mm^3 |
| `SteelAISI304` | 215 MPa | 193000 MPa | 8.00e-09 t/mm^3 |
| `ABS` | 40 MPa | 2200 MPa | 1.04e-09 t/mm^3 |

The default (`DccMcpStructuralSteel`) is the same default `run_fem_analysis`
solves with when no material is passed, so a review of a default solve
self-consistently reports steel. A user-supplied material always wins, and when
the user supplies one the provenance line says so instead.

## Reading a solve

`fem-review` consumes the payload `run_fem_analysis` returned, not a re-run:

| Field | Used for |
| --- | --- |
| `max_von_mises` | The stress the verdict is computed from |
| `max_displacement` | Serviceability, separate from strength |
| `axis_displacement` | Signed deflection along the load axis — the check that a load applied against the requested axis is not read as one applied along it |
| `material` | Provenance of E and ν; names the default when one was defaulted |
| `fixed_faces` / `load.faces` | Anchoring hot spots and spotting boundary artefacts |
| `mesh.size` | Whether convergence can be claimed |
| `node_count` / `verified` | A degenerate result is refused, not reviewed |

Quantities are `{"value": ..., "unit": ...}`. Read the number from `value` and
convert deliberately — the unit is part of the quantity, and a bare float was
already refused upstream.

## Worked example

100 x 10 x 10 mm steel cantilever, fixed at one end, 100 N in -Z at the other,
2 mm mesh. `run_fem_analysis` returns roughly `max_von_mises` 60 MPa and
`max_displacement` 0.190 mm. Against a target safety factor of 2 on a 250 MPa
yield:

- allowable 125 MPa, safety factor 2.08, verdict **`satisfied`** — but only just
  above the target, so `satisfied` with the margin stated rather than a bare
  pass;
- the hot spot is the **root face at the fixed end** (`Beam:Face3`), which is
  also a fully restrained face, so the peak is reported as **part artefact** —
  the true bending peak sits just outboard of the restraint and the element
  value there, not the nodal value, is the design number;
- next actions: the root is the only place that matters, so add a fillet there
  or thicken the section locally — thickening the free end does nothing;
- assumptions: rigid restraint, mesh **unchecked** for convergence, yield from
  the built-in generic table, linear static only.

The cantilever is the reference case in `docs/validation/fem-cantilever.md`,
whose analytic answers (0.190476 mm, 60.0 MPa) are what the solve is verified
against on both supported FreeCAD release lines.

## Undo

`undo: none` — the recipe is read-only with respect to the document. It reads a
solve payload and returns a report; it does not create, modify or delete
anything in the FreeCAD document, and it does not write files. There is nothing
to roll back, so no rollback steps are given. The one side effect worth knowing:
`run_fem_analysis` keeps its solver work directory on disk, and that is cleaned
up by the analysis skill, not by this one.

## Limits

- **Linear static only.** No plasticity, contact, buckling, fatigue or thermal
  load. A result above yield means the linear model has stopped being valid —
  report it as "the model no longer applies", not as a stress the part survives.
- **One load case.** No load combinations or factor-of-safety on the load side.
- **No design automation.** It recommends; it does not perform the
  modification. `freecad-modify` does that.
