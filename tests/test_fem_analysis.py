"""Real-hardware FEM verification: one cantilever, two FreeCAD release lines.

A structural tool that has never solved a problem with a known answer has not
been verified, only executed. This lane solves a case Euler-Bernoulli beam
theory answers in closed form and compares against it with the tolerance written
into the test -- not a sanity check, an assertion with a number in it.

Why this case: a 100 x 10 x 10 mm steel cantilever with a 100 N tip load
deflects 0.190476 mm and reaches 60 MPa at the root. Both are large enough to
measure and small enough that linear beam theory is the right comparison, and
both are exactly 1000x wrong if a force is read as kN instead of N. The
tolerances below are chosen to be passed by a genuine second-order tetrahedral
solution and failed by a unit error, a first-order mesh, or a solve that never
ran.

This lane runs in the `freecad-real` CI job on FreeCAD 1.0.2 and 1.1.4. It
requires the FEM workbench plus the CalculiX solver and Gmsh mesher, and it
fails loudly rather than skipping when they are missing: a skipped verification
looks identical to a passing one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcc_mcp_freecad import FemAnalysisError
from dcc_mcp_freecad.bridge import FreecadBridge
from dcc_mcp_freecad.fem_contract import (
    FemError,
    cantilever_root_stress,
    cantilever_second_moment,
    cantilever_tip_deflection,
)

BEAM_LENGTH_MM = 100.0
BEAM_WIDTH_MM = 10.0
BEAM_HEIGHT_MM = 10.0
FORCE_N = 100.0
YOUNGS_MPA = 210000.0
POISSON_RATIO = 0.3
MESH_SIZE_MM = 2.0

# Verification thresholds. A second-order tetrahedral mesh this fine lands
# within a few percent on displacement and within roughly ten percent on the
# peak nodal stress at a restrained root, where the extrapolation is least
# reliable. These are deliberately an order of magnitude tighter than a unit
# error and an order of magnitude looser than mesh noise.
MAX_DISPLACEMENT_REL_ERROR = 0.10
MAX_VON_MISES_REL_ERROR = 0.20


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


def _skip() -> bool:
    return not _real_freecad()


def _cantilever(tmp_path: Path, bridge: FreecadBridge) -> Path:
    document = tmp_path / "cantilever.FCStd"
    bridge.create_document(str(document))
    bridge.add_primitive(
        str(document),
        "box",
        "Beam",
        dimensions={
            "length": BEAM_LENGTH_MM,
            "width": BEAM_WIDTH_MM,
            "height": BEAM_HEIGHT_MM,
        },
    )
    return document


def _face_at_extreme(faces: dict, axis: int, pick_minimum: bool) -> dict:
    """Pick the resolvable face whose centre sits at one end of ``axis``.

    Face numbering moved between FreeCAD release lines, so the reference is
    discovered from geometry rather than hard-coded: a wrong guess would
    restrain the wrong end and still converge.
    """
    usable = [face for face in faces["faces"] if face["resolves"] and face["center"]]
    if not usable:
        raise AssertionError(
            "no face reference on this host resolves; sub-element naming moved and the "
            "reference form must be corrected before any FEM tool can be used"
        )
    centres = [face["center"][axis] for face in usable]
    target = min(centres) if pick_minimum else max(centres)
    return next(face for face in usable if face["center"][axis] == target)


@pytest.mark.freecad_fem
@pytest.mark.skipif(_skip(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_cantilever_matches_the_analytic_solution(tmp_path: Path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _cantilever(tmp_path, bridge)

    faces = bridge.list_faces(str(document), "Beam")
    fixed_face = _face_at_extreme(faces, axis=0, pick_minimum=True)
    loaded_face = _face_at_extreme(faces, axis=0, pick_minimum=False)
    assert fixed_face["reference"] != loaded_face["reference"]

    source_before = document.read_bytes()

    result = bridge.run_fem_analysis(
        str(document),
        target_object="Beam",
        fixed_faces=[fixed_face["reference"]],
        load={
            "force": {"value": FORCE_N, "unit": "N"},
            "direction": [0, 0, -1],
            "faces": [loaded_face["reference"]],
        },
        material={
            "youngs_modulus": {"value": YOUNGS_MPA, "unit": "MPa"},
            "poisson_ratio": POISSON_RATIO,
        },
        mesh_size={"value": MESH_SIZE_MM, "unit": "mm"},
        timeout_secs=1800,
    )

    # The solve ran, and it ran on a copy.
    assert result["solver_exit_code"] == 0, result["solver"]["stderr_tail"]
    assert result["node_count"] > 0
    assert Path(result["solver_workdir"]).is_dir(), result["solver_workdir"]
    assert document.read_bytes() == source_before, "a solve must not touch the source document"

    # Units are identified, not assumed, and every result carries one.
    assert result["unit_schema"]["length"] == "mm"
    assert result["unit_schema"]["force"] == "N"
    assert result["unit_schema"]["stress"] == "MPa"
    assert result["max_displacement"]["unit"] == "mm"
    assert result["max_von_mises"]["unit"] == "MPa"

    # The force the solver was given is the force that was asked for.
    assert result["load"]["force"]["value"] == pytest.approx(FORCE_N)
    assert result["load"]["force"]["unit"] == "N"
    assert "load.total_force" in result["verified"]
    assert "unit_schema.detected" in result["verified"]

    inertia = cantilever_second_moment(BEAM_WIDTH_MM, BEAM_HEIGHT_MM)
    expected_deflection = cantilever_tip_deflection(FORCE_N, BEAM_LENGTH_MM, YOUNGS_MPA, inertia)
    expected_stress = cantilever_root_stress(FORCE_N, BEAM_LENGTH_MM, BEAM_WIDTH_MM, BEAM_HEIGHT_MM)

    observed_deflection = result["max_displacement"]["value"]
    observed_stress = result["max_von_mises"]["value"]
    deflection_error = abs(observed_deflection - expected_deflection) / expected_deflection
    stress_error = abs(observed_stress - expected_stress) / expected_stress

    assert deflection_error <= MAX_DISPLACEMENT_REL_ERROR, (
        "tip deflection %.6f mm is %.2f%% off the analytic %.6f mm (tolerance %.2f%%)"
        % (
            observed_deflection,
            deflection_error * 100.0,
            expected_deflection,
            MAX_DISPLACEMENT_REL_ERROR * 100.0,
        )
    )
    assert stress_error <= MAX_VON_MISES_REL_ERROR, (
        "root von Mises %.3f MPa is %.2f%% off the analytic %.3f MPa (tolerance %.2f%%)"
        % (
            observed_stress,
            stress_error * 100.0,
            expected_stress,
            MAX_VON_MISES_REL_ERROR * 100.0,
        )
    )
    assert result["min_displacement"]["value"] == pytest.approx(0.0, abs=1e-9)

    # The load must have been applied along the requested axis, not against it.
    # Magnitudes alone cannot see this: a cantilever loaded in +Z deflects as far
    # as one loaded in -Z, so a magnitude-only check passes on a reversed load.
    # The tip moves along -Z here, so the component on that axis is negative and
    # its magnitude is the deflection.
    axis_displacement = result["axis_displacement"]["value"]
    assert axis_displacement < 0, (
        "the tip displaced %+.6f mm along the requested -Z load axis; a positive value means "
        "the load was applied against the requested direction" % axis_displacement
    )
    assert abs(axis_displacement) == pytest.approx(expected_deflection, rel=0.10), (
        "the signed tip displacement %+.6f mm does not match the analytic %.6f mm"
        % (axis_displacement, expected_deflection)
    )


@pytest.mark.freecad_fem
@pytest.mark.skipif(_skip(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_fem_refuses_an_analysis_name_that_does_not_exist(tmp_path: Path):
    """Naming an analysis is a promise the document has to keep.

    A missing analysis must not fall back to building one silently: the caller
    asked for a specific setup, and solving something else would answer a
    question that was never asked.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _cantilever(tmp_path, bridge)

    with pytest.raises(FemAnalysisError) as excinfo:
        bridge.run_fem_analysis(str(document), analysis_name="NoSuchAnalysis")

    assert excinfo.value.error_code == FemError.ERROR_NO_ANALYSIS
    assert "NoSuchAnalysis" in str(excinfo.value)


@pytest.mark.freecad_fem
@pytest.mark.skipif(_skip(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_host_builds_the_fem_objects_the_driver_creates():
    """The driver's type ids and proxies must work on the host that runs them.

    The unit suite stubs the driver, so a wrong type id is invisible there and
    100% fatal here. This asserts through the driver's own construction path
    rather than a parallel list of type names: FEM registers its types when
    ``Fem`` is imported, so a bare interpreter reports them as absent even on a
    healthy host.
    """
    bridge = FreecadBridge(_real_freecad())

    constructible = bridge.fem_capabilities()["objects_constructible"]

    assert constructible["ok"], (
        "this host cannot build the FEM objects a solve needs: %s" % constructible["message"]
    )


@pytest.mark.freecad_fem
@pytest.mark.skipif(_skip(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_probe_agrees_with_a_real_solve(tmp_path: Path):
    """A host the probe calls ready must be able to solve, and vice versa.

    The probe exists to stop the tool being advertised where it cannot run. That
    guarantee only holds if the two agree, so assert it directly rather than
    trusting the probe's own construction check.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _cantilever(tmp_path, bridge)
    faces = bridge.list_faces(str(document), "Beam")

    probe = bridge.fem_capabilities()
    ready = bool(probe.get("available"))

    try:
        bridge.run_fem_analysis(
            str(document),
            target_object="Beam",
            fixed_faces=[_face_at_extreme(faces, 0, True)["reference"]],
            load={
                "faces": [_face_at_extreme(faces, 0, False)["reference"]],
                "force": {"value": FORCE_N, "unit": "N"},
                "direction": [0.0, 0.0, -1.0],
            },
        )
        solved = True
    except FemAnalysisError:
        solved = False

    assert ready == solved, (
        "the probe reports available=%s but a real solve succeeded=%s; the probe must not "
        "advertise the tool where it cannot run" % (ready, solved)
    )


@pytest.mark.parametrize(
    ("name", "attr"),
    [
        ("bridge", "create_document"),
        ("bridge", "add_primitive"),
        ("bridge", "list_faces"),
        ("bridge", "run_fem_analysis"),
        ("bridge", "fem_capabilities"),
    ],
)
def test_real_tests_only_call_methods_the_bridge_has(name, attr):
    """Every attribute a real-hardware test calls must exist on the real class.

    These tests only run on a lane with FreeCAD installed, so a typo in a method
    name is invisible locally and fatal in CI. Checking the names against the
    class costs nothing and runs everywhere.
    """
    assert hasattr(FreecadBridge, attr)
