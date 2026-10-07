"""FEM bridge behaviour: honest degradation and the no-write guarantee.

The real-host lane in ``tests/test_fem_analysis.py`` proves the solve produces
right numbers. This file proves the other half: that the adapter never claims a
capability it does not have, and that a solve never writes the caller's
document. Both are failure modes that only appear on a machine that is not the
one the feature was developed on, which is exactly the machine a user has.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dcc_mcp_freecad import FemAnalysisError
from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge
from dcc_mcp_freecad.fem_contract import FemError


def _bridge(tmp_path: Path) -> FreecadBridge:
    return FreecadBridge(sys.executable, allowed_roots=[tmp_path])


def _document(tmp_path: Path) -> Path:
    document = tmp_path / "part.FCStd"
    document.write_bytes(b"not-a-real-fcstd")
    return document


def _unavailable_probe():
    return {
        "host_version": "1.1.4",
        "fem_workbench": True,
        "solver_tools": True,
        "solver": {"name": "calculix", "binary": None, "version": None},
        "mesher": {"name": "gmsh", "binary": None, "version": None},
        "available": False,
        "status": "host_limited",
        "blocking": [
            {
                "code": FemError.ERROR_SOLVER_MISSING,
                "message": "ccx was not found",
                "remediation": "apt-get install calculix-ccx",
            },
            {
                "code": FemError.ERROR_MESHER_MISSING,
                "message": "gmsh was not found",
                "remediation": "apt-get install gmsh",
            },
        ],
        "remediation": ["apt-get install calculix-ccx", "apt-get install gmsh"],
    }


def test_a_host_without_a_solver_reports_the_tool_as_host_limited(tmp_path, monkeypatch):
    """`get_capabilities` is where a caller decides whether to even try."""
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", lambda method, params, timeout: _unavailable_probe())

    capabilities = bridge.capabilities()

    tool = capabilities["fem"]["tools"]["run_fem_analysis"]
    assert capabilities["fem"]["available"] is False
    assert capabilities["fem"]["status"] == "host_limited"
    assert tool["available"] is False
    assert tool["status"] == "host_limited"
    # The remediation has to be executable, not a description of the problem.
    assert "apt-get install calculix-ccx" in tool["reason"]
    assert "apt-get install gmsh" in tool["reason"]
    assert "run_fem_analysis" in capabilities["methods"]


def test_a_failed_probe_is_reported_not_swallowed(tmp_path, monkeypatch):
    def explode(method, params, timeout):
        if method == "system.fem_probe":
            raise BridgeError("FreeCADCmd was not found")
        return {"version": "1.1.4", "ready": True}

    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", explode)

    capabilities = bridge.capabilities()

    assert capabilities["fem"]["status"] == "probe_failed"
    assert capabilities["fem"]["tools"]["run_fem_analysis"]["status"] == "host_limited"
    assert "FreeCADCmd was not found" in capabilities["fem"]["message"]


def test_a_ready_host_reports_the_tool_as_ready(tmp_path, monkeypatch):
    probe = _unavailable_probe()
    probe.update(
        {
            "available": True,
            "status": "ready",
            "blocking": [],
            "remediation": [],
            "solver": {"name": "calculix", "binary": "/usr/bin/ccx", "version": "2.22"},
            "mesher": {"name": "gmsh", "binary": "/usr/bin/gmsh", "version": "4.11"},
        }
    )
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", lambda method, params, timeout: probe)

    tool = bridge.capabilities()["fem"]["tools"]["run_fem_analysis"]

    assert tool["available"] is True
    assert tool["status"] == "ready"
    assert tool["reason"] is None


def test_a_structured_host_failure_reaches_the_caller_with_its_code(tmp_path, monkeypatch):
    def explode(method, params, timeout):
        raise FemAnalysisError(
            {
                "schema_version": 1,
                "error_code": FemError.ERROR_SOLVER_TIMEOUT,
                "message": "CalculiX exceeded the 60.0 second solver budget",
                "remediation": "coarse the mesh",
                "details": {"partial_stdout": "tail"},
                "host_version": "1.1.4",
            },
            "CalculiX exceeded the 60.0 second solver budget; host FreeCAD 1.1.4",
        )

    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", explode)
    document = _document(tmp_path)

    with pytest.raises(FemAnalysisError) as excinfo:
        bridge.run_fem_analysis(str(document), target_object="Beam")

    assert isinstance(excinfo.value, BridgeError), "existing handlers must still catch it"
    assert excinfo.value.error_code == FemError.ERROR_SOLVER_TIMEOUT
    assert excinfo.value.remediation == "coarse the mesh"
    assert excinfo.value.details == {"partial_stdout": "tail"}
    assert excinfo.value.host_version == "1.1.4"


def test_a_solve_that_changed_the_source_is_refused(tmp_path, monkeypatch):
    document = _document(tmp_path)

    def mutate(method, params, timeout):
        if method == "analysis.run_fem":
            document.write_bytes(b"changed-by-the-solver")
            return {"solver_exit_code": 0, "node_count": 1, "verified": []}
        raise AssertionError(method)

    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", mutate)

    with pytest.raises(BridgeError, match="must only read the source"):
        bridge.run_fem_analysis(str(document), target_object="Beam")


def test_a_solve_stages_a_copy_and_reports_the_work_directory(tmp_path, monkeypatch):
    document = _document(tmp_path)
    seen = {}

    def solve(method, params, timeout):
        if method == "analysis.run_fem":
            seen.update(params)
            return {"solver_exit_code": 0, "node_count": 12, "verified": ["results.present"]}
        raise AssertionError(method)

    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", solve)

    result = bridge.run_fem_analysis(
        str(document),
        target_object="Beam",
        load={"force": {"value": 100, "unit": "N"}, "direction": [0, 0, -1], "faces": ["B:Face1"]},
        mesh_size={"value": 2, "unit": "mm"},
        timeout_secs=600,
    )

    staged = Path(seen["document_path"])
    workdir = Path(seen["workdir"])
    assert staged.parent == workdir, "the analysed document must live in the staging directory"
    assert staged.name == document.name
    assert staged.read_bytes() == b"not-a-real-fcstd"
    assert seen["solver_timeout_secs"] == 570.0, (
        "the driver must finish before the process deadline"
    )
    assert result["solver_workdir"] == str(workdir)
    assert workdir.is_dir(), "the work directory is kept so a result can be audited"


def test_neither_an_analysis_nor_a_target_is_refused(tmp_path):
    bridge = _bridge(tmp_path)

    with pytest.raises(BridgeError, match="requires analysis_name or target_object"):
        bridge.run_fem_analysis(str(_document(tmp_path)))


def test_object_names_are_validated_before_a_solve_starts(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", lambda method, params, timeout: {})

    with pytest.raises(BridgeError, match="Object names must start with"):
        bridge.run_fem_analysis(str(_document(tmp_path)), target_object="9 bad name")
