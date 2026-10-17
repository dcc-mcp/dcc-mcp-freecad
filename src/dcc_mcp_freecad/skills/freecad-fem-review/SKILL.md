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
          resolves and re-anchor the location; when none resolve, report the
          entry at object level and declare why as a location_resolution
          assumption rather than inventing a reference.
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

The safety factor is **`yield_strength / max_von_mises`** — how far the peak
stress sits below yield. This is the quantity the term means in every
structural hand calculation, and it is the one the verdict is judged against.

The allowable stress is `yield_strength / target_safety_factor`: the stress the
part is permitted to reach. It is a *result* of the safety factor, not an input
to it. Dividing by the target twice — once to get the allowable, then again to
get a "safety factor" from it — is the error this section exists to prevent: it
silently demands `SF >= target²`, and at a target of 3 it reports a part at
4.17x yield margin as `violated`.

Yield strength is **user-supplied first**; when it is not supplied it comes from
the built-in table below, and the source is then part of the output.

### Verdict bands

Bands are defined against the **safety factor**, so they are independent of how
the target was expressed:

| Band | Rule | Meaning |
| --- | --- | --- |
| `violated` | `safety_factor < target` | Below the required margin. |
| `marginal` | `target <= safety_factor < target * 1.1` | Meets the target with under 10% headroom. |
| `satisfied` | `safety_factor >= target * 1.1` | Meets the target with real headroom. |

The 10% marginal width is deliberately narrow, and deliberately not zero: a
result exactly on the target has no margin for the model error the assumption
block already admits to, so it must not read as a clean pass. `marginal` is
never a rounding artefact — it is a real band with real width, and the number
never gets rounded across it.

When the caller supplies `allowable_stress` directly instead of a yield
strength, there is no yield to divide by, so the safety factor is not
computable. In that case report against `utilisation = max_von_mises /
allowable_stress`, judge `satisfied` at `<= 1/1.1`, `marginal` at
`<= 1.0`, and `violated` above it, and say in the output which basis was used.

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

### When the location cannot be anchored

Naming a face requires the host to resolve it, and the host cannot always do
that: `target_object` is absent when the solve reused an existing analysis
rather than being built from an object, and sub-element naming moved between
FreeCAD release lines. In that case the entry is **object-level** — it names the
object and carries no `reference` — and the review must add a
`location_resolution` assumption saying why no face or edge could be confirmed.

What is not acceptable is splitting a restraint reference such as `Beam:Face3`
into an object name in order to satisfy the contract. That is an unverified
claim wearing an anchor, and it is worse than an honest object-level entry
because it validates. A review that names no location at all still fails.

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

- allowable 125 MPa, safety factor **4.17** against the 250 MPa yield, verdict
  **`satisfied`** — 4.17 is more than `2.0 * 1.1`, so this is a comfortable pass
  and not a marginal one. The part is carrying 60 MPa of a 250 MPa yield;
- the margin is not uniform, though: at a target of 3 the allowable drops to
  83.3 MPa and the safety factor is still 4.17, so the verdict stays
  `satisfied`. A review that instead divided the allowable by the stress again
  would report 1.39 and call this `violated` — the failure mode the previous
  section warns about;
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
