# FEM structural verification: the cantilever case

`run_fem_analysis` reports stress and displacement. A tool that has only ever
been executed has not been verified — only run. This is the case that decides
whether the numbers it reports mean anything; it is exercised in CI on both
supported FreeCAD release lines, and both lanes must be green for the
verification below to hold.

## Why this case

A cantilever beam with an end load has a closed-form answer, and the answer is
sensitive to exactly the failure mode the feature exists to prevent.

| Quantity | Value |
| --- | --- |
| Beam | 100 mm (length) x 10 mm x 10 mm, solid |
| Restraint | fully fixed on the face at `x = 0` |
| Load | 100 N in `-Z`, applied to the face at `x = 100` |
| Material | E = 210000 MPa, ν = 0.30 (density defaulted, unused in a static solve) |
| Mesh | Gmsh, characteristic length 2 mm, second-order tetrahedra |

| Result | Analytic | Source |
| --- | --- | --- |
| Max displacement | 0.190476 mm | `δ = P L³ / (3 E I)`, `I = b h³ / 12 = 833.333 mm⁴` |
| Max von Mises at the root | 60.0 MPa | `σ = M c / I` with `M = P L` |

Both are large enough to measure against mesh noise and small enough that
linear Euler-Bernoulli theory is the correct comparison. Both are exactly 1000x
wrong if a force is read as kN instead of N.

## Thresholds

The tolerances are written into the test, not left to a visual check. They are
chosen to be an order of magnitude tighter than any unit error and an order of
magnitude looser than mesh discretisation error.

| Quantity | Threshold | Why that width |
| --- | --- | --- |
| Max displacement | 10% relative | Second-order tetrahedra land within a few percent on tip deflection; beam theory omits shear, and the built-in restraint stiffens the root slightly. |
| Max von Mises | 20% relative | Nodal stress at a fully restrained face is extrapolated from element integration points and is the least reliable number the solver produces. |

A first-order mesh is too stiff in bending to pass the displacement threshold,
which is why the driver refuses to solve without a second-order element order.

## What the case also proves

The analytic comparison is the headline, but the same run asserts the
properties that make the number trustworthy:

- **The unit schema was identified, not assumed.** The driver measures the
  generated solver input's node extents against the beam's real bounding box and
  accepts the schema only when exactly one length unit explains them. In this
  configuration that is `mm`, which implies `N` for force and `MPa` for stress.
- **The applied force is the requested force.** The summed `*CLOAD` block of the
  generated input must agree with 100 N along the requested direction to within
  1e-4 relative. This is the check that catches a kN/N mix-up, and it is the
  reason a bare-float force property is never trusted on its own.
- **The material that reached the solver is the material that was requested.**
  Young's modulus is read back out of the material object through
  `FreeCAD.Units.Quantity` and compared to the request.
- **The source document is untouched.** The document is staged into the solver
  work directory and only that copy is opened; the test compares the source
  bytes before and after, and the bridge refuses the call outright if they differ.
- **The result is non-degenerate.** Node count greater than zero, finite
  non-negative displacement and stress, and `max >= min`.

## How it runs

```bash
python -m pytest -m freecad_fem -v --junitxml=fem.xml
python3 .github/scripts/verify-freecad-run.py fem.xml 4
```

`tests/test_fem_analysis.py` is marked `freecad_fem`, which the existing
`freecad` and `freecad_gui` lanes do not select, so the contract and
presentation counts are unaffected. The `freecad-real` CI job installs
`calculix-ccx` and `gmsh` before running it on FreeCAD 1.0.2 and 1.1.4, and
`verify-freecad-run.py` fails the job unless all four cases ran. A skipped
verification is indistinguishable from a passing one, so the host is required to
actually solve.

The reference for the sub-element references is discovered from geometry at
runtime (`list_faces`, then pick the resolvable face at each end of the beam)
rather than hard-coded: face numbering moved between FreeCAD release lines, and a
stale name would restrain the wrong end and still converge.
