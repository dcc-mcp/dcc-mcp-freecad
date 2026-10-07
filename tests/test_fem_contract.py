"""Typed FEM contract: units, references and the solver-input read-back.

The failure mode under test is the one the whole feature exists to prevent: a
number that looks right and is wrong by a fixed factor. The reference
implementation this adapter is measured against applies ``ConstraintForce.Force``
1000x off (N read as kN), the solver converges, and the stress field looks
plausible. Nothing in a bare float can catch that, so this module pins the three
guards that can: the unit table, the schema detection on the generated solver
input, and the summed ``*CLOAD`` force read-back.

Everything here is stdlib-only and runs without FreeCAD. The real-host lane in
``tests/test_fem_analysis.py`` is what proves the guards do not fire on a real
solve; this file proves they do fire when a unit is wrong.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import fem_contract, freecad_driver, write_contract  # noqa: E402

SKILLS = Path(__file__).parents[1] / "src" / "dcc_mcp_freecad" / "skills"
TESTS = Path(__file__).parent

# A 100 x 10 x 10 mm cantilever, tip-loaded with 100 N downward. Analytic
# deflection 0.190476 mm, root bending stress 60 MPa. Small enough that Euler-
# Bernoulli beam theory is the right comparison, and the numbers are exact
# enough that a factor-of-1000 unit error cannot hide inside any sane tolerance.
BEAM_LENGTH_MM = 100.0
BEAM_WIDTH_MM = 10.0
BEAM_HEIGHT_MM = 10.0
FORCE_N = 100.0
YOUNGS_MPA = 210000.0


# ---------------------------------------------------------------------------
# The unit table
# ---------------------------------------------------------------------------


def test_canonical_units_are_declared_per_dimension():
    assert fem_contract.CANONICAL_UNITS == {
        "force": "N",
        "length": "mm",
        "stress": "MPa",
        "density": "t/mm^3",
    }


@pytest.mark.parametrize(
    "value,unit,dimension,expected",
    [
        (1000.0, "N", "force", 1000.0),
        (1.0, "kN", "force", 1000.0),
        (0.001, "MN", "force", 1000.0),
        (1.0, "kgf", "force", 9.80665),
        (100.0, "lbf", "force", pytest.approx(444.82216152605)),
        (2.0, "m", "length", 2000.0),
        (1.0, "in", "length", 25.4),
        (210000.0, "MPa", "stress", 210000.0),
        (210.0, "GPa", "stress", 210000.0),
        (210000.0, "N/mm^2", "stress", 210000.0),
        (7850.0, "kg/m^3", "density", pytest.approx(7.85e-9)),
        (7.85, "g/cm^3", "density", pytest.approx(7.85e-9)),
    ],
)
def test_conversions_land_on_the_canonical_unit(value, unit, dimension, expected):
    assert fem_contract.to_canonical(value, unit, dimension) == expected


@pytest.mark.parametrize("dimension", ["force", "length", "stress", "density"])
def test_a_bare_float_is_refused_not_assumed(dimension):
    """The whole point: a number with no unit has no defensible interpretation."""
    for value in (1.0, 0, 100):
        with pytest.raises(fem_contract.UnitError):
            fem_contract.to_canonical(value, None, dimension)
    with pytest.raises(fem_contract.UnitError):
        fem_contract.to_canonical(1.0, "", dimension)


def test_an_unknown_unit_is_refused_with_the_accepted_list():
    with pytest.raises(fem_contract.UnitError) as excinfo:
        fem_contract.to_canonical(1.0, "stone", "force")
    assert "N" in str(excinfo.value)


def test_a_unit_of_the_wrong_dimension_is_refused():
    # N is a force; asking for it as a stress would silently produce a value a
    # million times too small.
    with pytest.raises(fem_contract.UnitError):
        fem_contract.to_canonical(1.0, "N", "stress")


def test_non_finite_values_are_refused():
    for value in (float("nan"), float("inf")):
        with pytest.raises(fem_contract.UnitError):
            fem_contract.to_canonical(value, "N", "force")


def test_quantity_payload_keeps_both_the_request_and_the_canonical_value():
    payload = fem_contract.quantity_payload(1.0, "kN", "force")

    assert payload == {"value": 1000.0, "unit": "N", "requested": {"value": 1.0, "unit": "kN"}}


# ---------------------------------------------------------------------------
# Material and load
# ---------------------------------------------------------------------------


def test_default_material_is_structural_steel_and_says_so():
    material = fem_contract.material_payload(None)

    assert material["youngs_modulus"] == {
        "value": 210000.0,
        "unit": "MPa",
        "requested": {"value": 210000.0, "unit": "MPa"},
    }
    assert material["poisson_ratio"] == 0.3
    assert material["defaulted"] == ["density", "name", "poisson_ratio", "youngs_modulus"]


def test_defaulted_material_fields_are_named_in_the_result():
    material = fem_contract.material_payload({"youngs_modulus": {"value": 70.0, "unit": "GPa"}})

    assert material["youngs_modulus"]["value"] == 70000.0
    assert material["defaulted"] == ["density", "name", "poisson_ratio"]


@pytest.mark.parametrize(
    "spec",
    [
        {"youngs_modulus": {"value": 0, "unit": "MPa"}},
        {"youngs_modulus": {"value": -1, "unit": "MPa"}},
        {"density": {"value": 0, "unit": "t/mm^3"}},
        {"poisson_ratio": 0.5},
        {"poisson_ratio": -1.0},
        {"youngs_modulus": 210000.0},
    ],
)
def test_an_unusable_material_is_refused(spec):
    with pytest.raises(fem_contract.UnitError):
        fem_contract.material_payload(spec)


def test_a_force_without_a_unit_is_refused():
    with pytest.raises(fem_contract.UnitError, match="\\{value, unit\\}"):
        fem_contract.load_payload({"force": 100.0, "direction": [0, 0, -1], "faces": ["B:Face1"]})


def test_load_direction_is_normalised_and_reported():
    load = fem_contract.load_payload(
        {"force": {"value": 100.0, "unit": "N"}, "direction": [0, 0, -5], "faces": ["B:Face2"]}
    )

    assert load["force"]["value"] == 100.0
    assert load["unit_direction"] == pytest.approx([0.0, 0.0, -1.0])
    assert load["direction"] == [0.0, 0.0, -5.0]


@pytest.mark.parametrize(
    "load",
    [
        {"force": {"value": 0, "unit": "N"}, "direction": [0, 0, -1], "faces": ["B:Face1"]},
        {"force": {"value": 100, "unit": "N"}, "direction": [0, 0, 0], "faces": ["B:Face1"]},
        {"force": {"value": 100, "unit": "N"}, "direction": [0, 0], "faces": ["B:Face1"]},
        {"force": {"value": 100, "unit": "N"}, "direction": "down", "faces": ["B:Face1"]},
        None,
    ],
)
def test_an_unusable_load_is_refused(load):
    with pytest.raises(fem_contract.UnitError):
        fem_contract.load_payload(load)


# ---------------------------------------------------------------------------
# Geometry references
# ---------------------------------------------------------------------------


def test_a_reference_splits_into_object_and_sub_element():
    assert fem_contract.parse_reference("Beam:Face1") == ("Beam", "Face1")
    assert fem_contract.parse_reference(" Beamer_2:Vertex12 ") == ("Beamer_2", "Vertex12")


@pytest.mark.parametrize(
    "text",
    ["Beam", "Beam:", "Beam:1", "Beam:Solid1", "2Beam:Face1", "Beam:Face", ""],
)
def test_an_unqualified_reference_is_refused(text):
    """An object name alone would constrain the whole body: a different analysis
    that still converges."""
    with pytest.raises(fem_contract.UnitError):
        fem_contract.parse_reference(text)


# ---------------------------------------------------------------------------
# Unit schema detection from the generated solver input
# ---------------------------------------------------------------------------


def test_node_extents_name_the_length_unit_of_the_input():
    extents_mm = [BEAM_LENGTH_MM, BEAM_WIDTH_MM, BEAM_HEIGHT_MM]

    assert fem_contract.match_length_scale(extents_mm, extents_mm) == "mm"
    assert fem_contract.match_length_scale([v / 1000.0 for v in extents_mm], extents_mm) == "m"
    assert fem_contract.match_length_scale([v / 25.4 for v in extents_mm], extents_mm) == "in"
    assert fem_contract.match_length_scale([v / 10.0 for v in extents_mm], extents_mm) == "cm"


def test_an_ambiguous_or_unexplainable_extent_is_refused():
    extents_mm = [BEAM_LENGTH_MM, BEAM_WIDTH_MM, BEAM_HEIGHT_MM]

    # A 3x scale matches no unit; a flat axis carries no information and must
    # not be read as agreement.
    assert fem_contract.match_length_scale([v / 3.0 for v in extents_mm], extents_mm) is None
    assert fem_contract.match_length_scale([0.0, 0.0, 0.0], extents_mm) is None
    assert fem_contract.match_length_scale([1.0, 2.0], [1.0, 2.0]) is None


def test_a_flat_axis_is_skipped_rather_than_assumed():
    # A plate: one extent is zero in every unit, so only two axes can speak.
    assert fem_contract.match_length_scale([100.0, 10.0, 0.0], [100.0, 10.0, 0.0]) == "mm"


def test_the_length_unit_names_the_rest_of_the_schema():
    assert fem_contract.unit_schema_for_length("mm") == ("N", "MPa")
    assert fem_contract.unit_schema_for_length("m") == ("N", "Pa")
    assert fem_contract.unit_schema_for_length("in") == ("lbf", "psi")
    assert fem_contract.unit_schema_for_length("cm") is None


# ---------------------------------------------------------------------------
# Solver input parsing (the read-back that catches a 1000x force)
# ---------------------------------------------------------------------------

_INP_MILLIMETRES = """*NODE, NSET=Nall
1, 0.0, 0.0, 0.0
2, 100.0, 0.0, 0.0
3, 100.0, 10.0, 10.0
*ELEMENT, TYPE=C3D10
1, 1, 2, 3, 4
*CLOAD
12, 3, -25.0
13, 3, -50.0
14, 3, -25.0
*MATERIAL, NAME=Steel
*ELASTIC
210000.0, 0.3
"""


_INP_AS_UPSTREAM_WRITES_IT = """*NODE, NSET=Nall
** node coordinates
1, 0.0, 0.0, 0.0
2, 100.0, 0.0, 0.0
3, 100.0, 10.0, 10.0
*ELEMENT, TYPE=C3D10
1, 1, 2, 3, 4
*CLOAD
** DccMcpConstraintForce
** node loads on shape: Beam:Face2
12, 3, -25.0
13, 3, -50.0
14, 3, -25.0
*MATERIAL, NAME=Steel
*ELASTIC
210000.0, 0.3
"""


def _write(tmp_path: Path, text: str, name: str = "beam.inp") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_node_extents_are_measured_from_the_input(tmp_path: Path):
    path = _write(tmp_path, _INP_MILLIMETRES)

    assert freecad_driver._parse_node_extents(path) == pytest.approx([100.0, 10.0, 10.0])


def test_cload_components_sum_to_the_total_applied_force(tmp_path: Path):
    path = _write(tmp_path, _INP_MILLIMETRES)

    totals = freecad_driver._parse_cload(path)

    assert totals == {1: 0.0, 2: 0.0, 3: -100.0}


def test_a_thousandfold_force_is_a_visible_mismatch_not_a_surprise(tmp_path: Path):
    scaled = _INP_MILLIMETRES.replace("-25.0", "-25000.0").replace("-50.0", "-50000.0")
    path = _write(tmp_path, scaled)

    totals = freecad_driver._parse_cload(path)
    applied = totals[3]
    requested = FORCE_N * -1

    assert applied != pytest.approx(requested)
    assert applied == pytest.approx(requested * 1000.0)


def test_a_missing_block_is_reported_as_absent_rather_than_zero(tmp_path: Path):
    without_cload = _INP_MILLIMETRES.replace("*CLOAD", "*DLOAD")
    path = _write(tmp_path, without_cload)

    assert freecad_driver._parse_cload(path) is None


# A .frd that holds the mesh but no result dataset at all: the solver wrote the
# nodes and elements and stopped. Node blocks are 2C and element blocks 3C, so
# neither counts as a result.
_FRD_NODES_ONLY = """    1    1
    2 C
    1    1    1
-1    1
-2    1
   -1
   -2
"""


_GOLDEN_FRD = TESTS / "frd_box_static_golden.frd"


def test_an_frd_without_a_result_block_is_detected(tmp_path: Path):
    """A zero exit code does not mean the analysis was solved.

    CalculiX can write a .frd containing the mesh but no displacements or
    stresses. That file parses cleanly and yields an empty result, so the
    failure has to be caught here -- while the solver's own output is still
    available -- rather than downstream as a bare zero node count.

    The positive anchor is an excerpt of FreeCAD's own golden result file
    rather than a hand-written sample: in real output the step key precedes
    the dataset header and is not adjacent to it, and hand-written fixtures
    had that ordering backwards -- which let a detector that returns False
    on every real file still pass this test.
    """
    assert freecad_driver._frd_has_results(str(_GOLDEN_FRD)) is True

    empty = _write(tmp_path, _FRD_NODES_ONLY, "nodes_only.frd")
    assert freecad_driver._frd_has_results(empty) is False


def test_an_frd_header_without_values_is_not_a_result(tmp_path: Path):
    """A dataset header with no component record under it holds no results.

    Requiring a ``-4`` / ``-5`` component record, not merely a header, is what
    distinguishes a result from a header the solver wrote and left empty.
    """
    golden = _GOLDEN_FRD.read_text(encoding="utf-8").splitlines(True)
    stripped = [line for line in golden if (line.split(None, 1) or [""])[0] not in ("-4", "-5")]
    path = _write(tmp_path, "".join(stripped), "header_only.frd")

    assert freecad_driver._frd_has_results(path) is False


def test_solver_output_is_carried_into_the_failure(tmp_path: Path):
    """A failed check must not leave the solver's output behind.

    The whole point of the read-back is to explain a wrong answer, and an
    empty result usually means the solver failed while still exiting zero.
    Reporting only the name of the check that failed says nothing about why.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "dcc-mcp-ccx.stderr.log").write_text(
        "*ERROR: nonpositive jacobian determinant in element 12\n", encoding="utf-8"
    )
    (workdir / "dcc-mcp-ccx.stdout.log").write_text("job finished\n", encoding="utf-8")

    evidence = freecad_driver._solver_evidence(
        workdir, {"exit_code": 0, "duration_secs": 1.5, "stderr_tail": "*ERROR: in ccx\n"}
    )

    assert evidence["solver_exit_code"] == 0
    assert "nonpositive jacobian" in evidence["ccx_stderr_from_disk"]
    assert "ccx_stdout_from_disk" in evidence
    assert "solver_workdir" in evidence


def test_in_block_comment_lines_do_not_end_the_block(tmp_path: Path):
    """`**` is a CalculiX comment, not a keyword.

    Upstream writes a `** <label>` line after `*CLOAD` and again before each
    referenced shape's node rows, so the block the workbench actually produces
    always contains comment lines. Treating one as a keyword ends the block and
    discards every node row -- which silently turns the force read-back into
    a comparison against zeros.
    """
    path = _write(tmp_path, _INP_AS_UPSTREAM_WRITES_IT)

    assert freecad_driver._parse_cload(path) == {1: 0.0, 2: 0.0, 3: -100.0}
    assert freecad_driver._parse_node_extents(path) == pytest.approx([100.0, 10.0, 10.0])


def test_parsing_a_missing_file_returns_none(tmp_path: Path):
    assert freecad_driver._parse_cload(str(tmp_path / "absent.inp")) is None
    assert freecad_driver._parse_node_extents(str(tmp_path / "absent.inp")) is None


# ---------------------------------------------------------------------------
# Analytic comparison targets
# ---------------------------------------------------------------------------


def test_cantilever_analytic_values_are_the_verification_target():
    inertia = fem_contract.cantilever_second_moment(BEAM_WIDTH_MM, BEAM_HEIGHT_MM)

    assert inertia == pytest.approx(833.3333333333)
    deflection = fem_contract.cantilever_tip_deflection(
        FORCE_N, BEAM_LENGTH_MM, YOUNGS_MPA, inertia
    )
    stress = fem_contract.cantilever_root_stress(
        FORCE_N, BEAM_LENGTH_MM, BEAM_WIDTH_MM, BEAM_HEIGHT_MM
    )

    assert deflection == pytest.approx(0.1904761904)
    assert stress == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Structured errors
# ---------------------------------------------------------------------------


def test_a_structured_error_carries_its_code_and_survives_the_boundary():
    error = fem_contract.FemError(
        fem_contract.FemError.ERROR_SOLVER_MISSING,
        "ccx was not found",
        remediation="apt-get install calculix-ccx",
        details={"names": ["ccx"]},
        host_version="1.1.4",
    )

    revived = fem_contract.FemError.from_payload(json.loads(json.dumps(error.payload)))

    assert revived.payload == error.payload
    assert revived.error_code == fem_contract.FemError.ERROR_SOLVER_MISSING
    assert str(revived) == str(error)
    assert "apt-get install calculix-ccx" in str(error)
    assert "host FreeCAD 1.1.4" in str(error)


def test_non_finite_evidence_is_kept_visible_as_text():
    error = fem_contract.FemError(
        fem_contract.FemError.ERROR_RESULTS_MISSING,
        "no result",
        details={"value": float("nan")},
    )

    assert json.loads(json.dumps(error.payload))["details"]["value"] == "nan"


def test_the_driver_classifies_its_fem_methods():
    methods = set(freecad_driver._METHODS)

    assert {"system.fem_probe", "analysis.run_fem", "analysis.list_faces"} <= methods
    assert "analysis.run_fem" in write_contract.MUTATING_TOOLS
    assert "system.fem_probe" in write_contract.READ_ONLY_TOOLS
    assert "analysis.list_faces" in write_contract.READ_ONLY_TOOLS


def test_the_contract_loads_by_path_the_way_the_driver_loads_it():
    """The driver runs inside FreeCAD's interpreter with no importable package,
    so it loads this module from disk. That path must keep working."""
    path = Path(freecad_driver.__file__).with_name(fem_contract.__name__.rsplit(".", 1)[-1] + ".py")
    spec = importlib.util.spec_from_file_location("fem_contract_by_path", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.to_canonical(1.0, "kN", "force") == 1000.0


# ---------------------------------------------------------------------------
# The tool schema forbids what the contract forbids
# ---------------------------------------------------------------------------


def _fem_tool_schema():
    path = SKILLS / "freecad-analysis" / "tools.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {tool["name"]: tool["input_schema"] for tool in payload["tools"]}


@pytest.mark.parametrize(
    "path",
    [
        ("material", "properties", "youngs_modulus"),
        ("material", "properties", "density"),
        ("load", "properties", "force"),
        ("mesh_size",),
    ],
)
def test_every_quantity_parameter_requires_a_unit(path):
    schema = _fem_tool_schema()["run_fem_analysis"]["properties"]
    for key in path:
        schema = schema[key]

    assert schema["type"] == "object"
    assert set(schema["required"]) == {"value", "unit"}
    assert schema["properties"]["unit"]["enum"]


def test_the_schema_requires_either_an_existing_analysis_or_a_full_case():
    schema = _fem_tool_schema()["run_fem_analysis"]

    assert {"required": ["analysis_name"]} in schema["anyOf"]
    assert {"required": ["target_object", "fixed_faces", "load"]} in schema["anyOf"]


def test_face_references_are_pattern_bounded():
    schema = _fem_tool_schema()["run_fem_analysis"]["properties"]

    for key in ("fixed_faces",):
        assert schema[key]["items"]["pattern"].startswith("^")
    assert schema["load"]["properties"]["faces"]["items"]["pattern"].startswith("^")


# ---------------------------------------------------------------------------
# Host availability, without a host
#
# `system.fem_probe` and the entry point's refusal path are the two places that
# decide whether an answer may be reported at all. They are exercised here
# against a stubbed FreeCAD so the decision logic is covered on every platform,
# not only on a machine that happens to have CalculiX installed.
# ---------------------------------------------------------------------------


class _FakeQuantity:
    def __init__(self, text):
        self.text = text

    def getValueAs(self, unit):
        return float(self.text.split()[0])


class _FakeUnits:
    Quantity = _FakeQuantity


class _FakePreferences:
    def __init__(self, store):
        self.store = store

    def GetString(self, key, default=""):
        return self.store.get(key, default)

    def SetString(self, key, value):
        self.store[key] = value


class _FakeApp:
    Version = staticmethod(lambda: ["1", "1", "4", ""])
    Units = _FakeUnits

    def __init__(self):
        self.store = {}

    def ParamGet(self, path):
        return _FakePreferences(self.store)


def _install_fake_freecad(monkeypatch, *, with_solver):
    import types

    app = _FakeApp()
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "Fem", types.ModuleType("Fem"))
    femtools = types.ModuleType("femtools")
    ccxtools = types.ModuleType("femtools.ccxtools")
    femtools.ccxtools = ccxtools
    monkeypatch.setitem(sys.modules, "femtools", femtools)
    monkeypatch.setitem(sys.modules, "femtools.ccxtools", ccxtools)
    monkeypatch.setattr(
        freecad_driver.shutil,
        "which",
        lambda name: (
            "/usr/bin/ccx"
            if with_solver and name == "ccx"
            else ("/usr/bin/gmsh" if with_solver and name == "gmsh" else None)
        ),
    )
    return app


def test_the_probe_names_the_missing_binaries_and_how_to_fix_them(monkeypatch):
    _install_fake_freecad(monkeypatch, with_solver=False)

    report = freecad_driver.system_fem_probe({})

    assert report["available"] is False
    assert report["status"] == "host_limited"
    assert report["host_version"] == "1.1.4"
    # The stubbed host imports Fem but cannot construct the objects, so the
    # solver-API blocker is expected alongside the two missing binaries.
    assert {item["code"] for item in report["blocking"]} == {
        fem_contract.FemError.ERROR_SOLVER_MISSING,
        fem_contract.FemError.ERROR_MESHER_MISSING,
        fem_contract.FemError.ERROR_SOLVER_API,
    }
    assert any("calculix-ccx" in item for item in report["remediation"])


def test_a_host_that_cannot_solve_refuses_to_report_a_result(tmp_path, monkeypatch):
    _install_fake_freecad(monkeypatch, with_solver=False)

    with pytest.raises(RuntimeError) as excinfo:
        freecad_driver.analysis_run_fem(
            {"document_path": str(tmp_path / "part.FCStd"), "workdir": str(tmp_path)}
        )

    # The driver loads fem_contract by path inside FreeCAD's interpreter, so the
    # raised class is a distinct object from the imported one. The payload is the
    # contract that has to match, not the identity.
    error = excinfo.value
    assert error.error_code == fem_contract.FemError.ERROR_HOST_LIMITED
    assert error.payload["schema_version"] == fem_contract.SCHEMA_VERSION
    assert error.details["status"] == "host_limited"
    assert fem_contract.FemError.from_payload(error.payload).error_code == error.error_code


def test_a_structured_failure_keeps_its_code_across_the_process_boundary(tmp_path, monkeypatch):
    """The caller branches on the code, so it must survive as a field."""
    _install_fake_freecad(monkeypatch, with_solver=False)
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(
        json.dumps(
            {
                "method": "analysis.run_fem",
                "params": {
                    "document_path": str(tmp_path / "part.FCStd"),
                    "workdir": str(tmp_path),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["driver", "--pass", str(request), str(result)])

    freecad_driver.main()

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["error"]["error_code"] == fem_contract.FemError.ERROR_HOST_LIMITED
    assert payload["error"]["fem_error"]["host_version"] == "1.1.4"


def test_a_failed_check_carries_extra_evidence_into_the_error():
    """`extra` must reach the details of the raised error, not just be accepted.

    The solver output is what lets a caller tell a degenerate mesh from an
    unconstrained model. Collecting it is only half the wiring; this covers the
    other half -- that a check which fails actually hands it to the error.
    """
    checks = freecad_driver._fem_checks("1.1.4", {})

    with pytest.raises(Exception) as excinfo:
        checks.check(
            False,
            "results.node_count",
            ">0",
            0,
            fem_contract.FemError.ERROR_RESULTS_MISSING,
            "An empty result set is not a result.",
            extra={"solver_workdir": "/tmp/wd", "ccx_stderr_from_disk": "*ERROR: degenerate"},
        )

    details = excinfo.value.payload["details"]
    assert details["check"] == "results.node_count"
    assert details["solver_workdir"] == "/tmp/wd"
    assert "degenerate" in details["ccx_stderr_from_disk"]
    # the usual fields must still be there alongside the extra ones
    assert details["expected"] == ">0"
    assert details["actual"] == 0


def test_solver_output_reaches_the_logged_message():
    """A log line is often all a human sees, so it must carry the evidence.

    The structured details are what a program branches on, but CI logs show the
    message. Without this, a failure reports only "no results" and cannot be
    acted on without re-running the analysis on a real host.
    """
    error = fem_contract.FemError(
        fem_contract.FemError.ERROR_RESULTS_MISSING,
        "run_fem_analysis could not verify results.node_count",
        remediation="An empty result set is not a result.",
        details={
            "check": "results.node_count",
            "solver_workdir": "/tmp/wd",
            "ccx_stderr_from_disk": "*ERROR: nonpositive jacobian determinant in element 12",
        },
        host_version="1.0.2",
    )
    message = str(error)

    assert "solver_workdir" in message
    assert "nonpositive jacobian" in message
    # the structured fields must survive for callers that branch on them
    assert error.details["solver_workdir"] == "/tmp/wd"
    assert error.error_code == fem_contract.FemError.ERROR_RESULTS_MISSING


class _FakeFemMesh:
    def __init__(self, count):
        self.Nodes = {index: () for index in range(1, count + 1)}


class _FakeMeshResult:
    """Stands in for the MeshResult document object upstream assigns to .Mesh.

    It is a Fem::FemMeshObjectPython: the mesh is under .FemMesh and the object
    itself has no .Nodes, which is what made every solve look like it returned
    no results.
    """

    def __init__(self, count):
        self.FemMesh = _FakeFemMesh(count)


class _FakeResult:
    def __init__(self, count):
        self.Mesh = _FakeMeshResult(count)
        self.NodeCount = 0
        self.vonMises = []
        self.DisplacementVectors = []


def test_result_nodes_are_read_through_the_mesh_result_object():
    """`result.Mesh` is a document object, not the mesh.

    Upstream builds a MeshResult and puts the mesh on its FemMesh property, so
    reading .Mesh.Nodes finds nothing and the node count collapses to zero --
    reporting a successful solve as an empty result.
    """
    result = _FakeResult(11384)

    nodes = freecad_driver._result_nodes(result)
    assert nodes is not None
    assert len(nodes) == 11384
    assert freecad_driver._extract_results(result)["node_count"] == 11384


def test_a_result_without_a_mesh_has_no_nodes():
    """With no mesh anywhere, the caller's fail-closed check still fires."""

    class _Empty:
        Mesh = None

    assert freecad_driver._result_nodes(_Empty()) is None


def test_timeout_output_is_kept_when_the_stream_is_bytes():
    """A timed-out solve must still report what it managed to produce.

    The POSIX timeout path leaves the captured output as bytes even with
    universal_newlines set. Dropping it silently writes an empty log and leaves
    the caller with a message that promises partial output it does not have.
    """
    assert freecad_driver._decode_stream(b"*ERROR in ecp: element 12") == (
        "*ERROR in ecp: element 12"
    )
    assert freecad_driver._decode_stream("Job finished") == "Job finished"
    assert freecad_driver._decode_stream(None) == ""
    assert freecad_driver._decode_stream(b"") == ""
