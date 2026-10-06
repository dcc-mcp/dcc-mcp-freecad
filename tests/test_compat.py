"""FreeCAD host compatibility matrix: classification, guards, and evidence.

The matrix is the contract that stops host version drift from becoming a
silent modelling bug. These tests pin both directions: a version inside the
matrix is accepted with its declared breaks reported, and a version outside it
is rejected with a machine-readable code.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import (
    compat,  # noqa: E402
    freecad_driver,  # noqa: E402
)

MATRIX_PATH = Path(compat.MATRIX_PATH)


@pytest.fixture()
def matrix():
    return compat.load_matrix()


def _driver_compat():
    """Load compat.py the way the in-FreeCAD driver does, by file path."""
    return freecad_driver._compat_module()


# --------------------------------------------------------------------------
# The matrix file itself
# --------------------------------------------------------------------------


def test_matrix_is_machine_readable_and_shipped_next_to_the_driver():
    payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    driver_dir = Path(freecad_driver.__file__).parent

    assert payload["schema_version"] >= 1
    assert payload["host"] == "freecad"
    assert payload["executable_names"] == ["FreeCADCmd", "freecadcmd"]
    assert MATRIX_PATH.parent == driver_dir


def test_every_declared_range_carries_real_machine_evidence(matrix):
    for entry in matrix["supported_ranges"]:
        assert entry["id"]
        assert compat.parse_version(entry["min_version"]) is not None
        assert compat.parse_version(entry["max_version"]) is not None
        assert entry["ci_matrix_entry"], entry["id"]
        assert "passed" in entry["evidence"], entry["id"]


def test_matrix_declares_the_known_1_0_to_1_1_breaks(matrix):
    identifiers = {entry["id"] for entry in matrix["breaking_changes"]}

    assert "sketcher-symmetric-renamed-to-midplane" in identifiers
    assert "sketcher-external-geometry-count-removed" in identifiers
    assert "meshpart-tessellate-deflection" in identifiers
    for entry in matrix["breaking_changes"]:
        assert entry["changed_in"] == "1.1"
        assert entry["remediation"], entry["id"]
        assert entry["adapter_usage"] in {"guarded", "unused"}, entry["id"]
        assert entry["enforcement"] in {"guard", "postcondition"}, entry["id"]


# --------------------------------------------------------------------------
# Version classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version",
    ["1.0.0", "1.0.2", "1.0.9999", "1.1.0", "1.1.4", "1.1.99"],
)
def test_supported_versions_are_classified_as_supported(version, matrix):
    verdict = compat.classify_host(version, matrix)

    assert verdict["status"] == compat.SUPPORTED
    assert verdict["range"]["id"] in {"1.0.x", "1.1.x"}
    assert verdict["supported_ranges"] == ["1.0.x", "1.1.x"]


@pytest.mark.parametrize("version", ["0.20.2", "0.21.2", "0.99.9"])
def test_legacy_versions_are_rejected_as_too_old(version, matrix):
    verdict = compat.classify_host(version, matrix)

    assert verdict["status"] == compat.TOO_OLD
    assert "unsupported" in compat.unsupported_reason(verdict)
    assert "1.0.x" in compat.unsupported_reason(verdict)


@pytest.mark.parametrize("version", ["1.2.0", "2.0.0", "1.1.10000"])
def test_unverified_newer_versions_are_rejected_instead_of_assumed_supported(version, matrix):
    verdict = compat.classify_host(version, matrix)

    assert verdict["status"] == compat.TOO_NEW
    reason = compat.unsupported_reason(verdict)
    assert "newer than the verified compatibility matrix" in reason
    assert "refuses to run unverified" in reason


@pytest.mark.parametrize("version", ["", "not-a-version", "1", "FreeCAD 1.1"])
def test_unparsable_versions_are_reported_as_unknown(version, matrix):
    verdict = compat.classify_host(version, matrix)

    assert verdict["status"] == compat.UNKNOWN
    assert "unrecognised version" in compat.unsupported_reason(verdict)


def test_a_version_in_a_gap_between_ranges_is_not_supported():
    gapped = {
        "schema_version": 1,
        "host": "freecad",
        "supported_ranges": [
            {"id": "1.0.x", "min_version": "1.0.0", "max_version": "1.0.9999"},
            {"id": "1.2.x", "min_version": "1.2.0", "max_version": "1.2.9999"},
        ],
        "breaking_changes": [],
    }

    assert compat.classify_host("1.1.4", gapped)["status"] == compat.UNLISTED
    assert compat.classify_host("1.0.5", gapped)["status"] == compat.SUPPORTED


def test_breaking_changes_apply_from_their_declared_version(matrix):
    assert [entry["id"] for entry in compat.breaking_changes_for("1.1.4", matrix)]
    assert compat.breaking_changes_for("1.0.2", matrix) == []
    assert compat.breaking_changes_for("not-a-version", matrix) == []


def test_breaking_changes_are_reportable_without_internal_fields(matrix):
    verdict = compat.classify_host("1.1.4", matrix)

    for entry in verdict["breaking_changes"]:
        assert set(entry) >= {
            "id",
            "title",
            "changed_in",
            "kind",
            "adapter_usage",
            "enforcement",
            "remediation",
        }
        json.dumps(entry)


# --------------------------------------------------------------------------
# Doctor / verify reporting
# --------------------------------------------------------------------------


def _doctor_report(monkeypatch, capsys, version):
    from dcc_mcp_freecad import cli, doctor

    executable = Path(monkeypatch.getattr("os", "name", os.name) and "")  # placeholder
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: doctor.MIN_CORE_VERSION)
    monkeypatch.setattr(
        doctor.FreecadBridge,
        "status",
        lambda self, timeout_secs=30: {
            "version": version,
            "python_version": "3.11.9",
            "ready": True,
            "api_probe": [],
        },
    )
    code = cli.main(["doctor", "--json"])
    return code, json.loads(capsys.readouterr().out), executable


def test_doctor_reports_the_detected_version_and_matrix_verdict(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = tmp_path / "FreeCAD 1.1" / "bin" / "FreeCADCmd.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: doctor.MIN_CORE_VERSION)
    monkeypatch.setattr(
        doctor.FreecadBridge,
        "status",
        lambda self, timeout_secs=30: {
            "version": "1.1.4",
            "python_version": "3.11.14",
            "ready": True,
            "api_probe": [
                {
                    "id": "meshpart-tessellate-deflection",
                    "attribute": "meshFromShape",
                    "expected": "present",
                    "observed": "present",
                    "matches_expected": True,
                }
            ],
        },
    )

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 0

    report = json.loads(capsys.readouterr().out)
    runtime = report["checks"]["runtime"]
    assert report["error_code"] is None
    assert runtime["freecad_version"] == "1.1.4"
    assert runtime["host_matrix"]["status"] == "supported"
    assert runtime["host_matrix"]["range"]["id"] == "1.1.x"
    assert runtime["host_matrix"]["supported_ranges"] == ["1.0.x", "1.1.x"]
    # Declared 1.1 breaks are reported, not hidden.
    assert [entry["id"] for entry in runtime["host_matrix"]["breaking_changes"]]
    assert runtime["host_matrix"]["api_probe"][0]["matches_expected"] is True


def test_doctor_rejects_a_version_above_the_matrix_with_a_next_step(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = tmp_path / "FreeCADCmd.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: doctor.MIN_CORE_VERSION)
    monkeypatch.setattr(
        doctor.FreecadBridge,
        "status",
        lambda self, timeout_secs=30: {
            "version": "1.2.0",
            "python_version": "3.12.1",
            "ready": True,
            "api_probe": [],
        },
    )

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 10

    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "freecad_host_version_unverified"
    assert report["verify"]["failure_stage"] == "host_version"
    assert "1.2.0" in report["verify"]["failure_reason"]
    assert report["checks"]["runtime"]["host_matrix"]["status"] == "too_new"
    assert report["next_steps"][0]["command"]


# --------------------------------------------------------------------------
# Driver-side guards (they run inside FreeCAD's interpreter)
# --------------------------------------------------------------------------


def test_the_driver_loads_the_same_matrix_as_the_service():
    assert _driver_compat().MATRIX_PATH == compat.MATRIX_PATH
    assert _driver_compat().classify_host("1.1.4")["status"] == compat.SUPPORTED


class _FakeObject:
    TypeId = "Part::Box"

    def __init__(self, **properties):
        for name, value in properties.items():
            setattr(self, name, value)


def test_a_renamed_property_raises_with_its_replacement():
    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver._resolve_property(_FakeObject(Symmetric=True), "Symmetric", "1.1.4")

    message = str(error.value)
    assert "Symmetric" in message
    assert "Midplane" in message
    assert "1.1" in message


def test_a_removed_property_raises_with_its_replacement():
    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver._resolve_property(
            _FakeObject(ExternalGeometryCount=2), "ExternalGeometryCount", "1.1.4"
        )

    message = str(error.value)
    assert "removed" in message
    assert "ExternalGeometry" in message


def test_a_missing_property_raises_instead_of_silently_doing_nothing():
    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver._resolve_property(_FakeObject(), "Length", "1.1.4")

    assert "does not expose property Length" in str(error.value)


def test_dimension_writes_go_through_the_property_guard():
    obj = _FakeObject(Length=10.0, Width=5.0, Height=2.0)

    freecad_driver._apply_dimensions(obj, {"length": 84}, "1.1.4")

    assert obj.Length == 84
    assert freecad_driver._resolve_property(obj, "Length", "1.1.4") == "Length"


def test_the_property_guard_does_not_fire_on_supported_symbols():
    obj = _FakeObject(Length=10.0, Width=5.0, Height=2.0, Radius=1.0)

    freecad_driver._apply_dimensions(obj, {"length": 2, "width": 2, "height": 2}, "1.0.2")

    assert (obj.Length, obj.Width, obj.Height) == (2, 2, 2)


def test_dimension_writes_route_through_the_guard_and_refuse_a_missing_property():
    """The guard must be wired into the write path, not merely exist.

    Without it, setattr on a host that no longer exposes the property adds a
    plain attribute and the call succeeds while the geometry never changes.
    """
    obj = _FakeObject()  # a Part::Box that lost its Length property

    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver._apply_dimensions(obj, {"length": 84}, "1.1.4")

    assert "Length" in str(error.value)
    assert not hasattr(obj, "Length")


def test_an_unsupported_host_cannot_reach_geometry_work():
    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver._require_supported_host("1.2.0")

    assert "refuses to run unverified" in str(error.value)


def test_a_supported_host_passes_the_preflight_gate():
    assert freecad_driver._require_supported_host("1.1.4")["status"] == "supported"


def _run_driver(monkeypatch, tmp_path, method, params, version):
    """Drive freecad_driver.main() the way FreeCADCmd does, and read the result."""
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(
        json.dumps({"method": method, "params": params}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(freecad_driver, "_host_version", lambda: version)
    monkeypatch.setattr(sys, "argv", ["driver", "--pass", str(request), str(result)])
    freecad_driver.main()
    return json.loads(result.read_text(encoding="utf-8"))


def test_the_dispatch_gate_blocks_geometry_work_on_an_unsupported_host(tmp_path, monkeypatch):
    """The gate lives in the request loop, so it is tested through main().

    A policy function that is never called by the dispatcher is the exact
    silent-success failure this matrix exists to prevent, so this assertion
    runs a real dispatch rather than poking _require_supported_host directly.
    """
    payload = _run_driver(
        monkeypatch,
        tmp_path,
        "model.add_primitive",
        {
            "document_path": str(tmp_path / "doc.FCStd"),
            "primitive": "box",
            "name": "Body",
            "dimensions": {"length": 10, "width": 5, "height": 2},
            "translation": [0, 0, 0],
            "rotation_axis": [0, 0, 1],
            "rotation_degrees": 0,
        },
        "1.2.0",
    )

    assert payload["ok"] is False
    assert payload["error"]["type"] == "IncompatibleHostError"
    assert "refuses to run unverified" in payload["error"]["message"]
    # Nothing was written: the geometry work never ran.
    assert not (tmp_path / "doc.FCStd").exists()


def test_the_dispatch_gate_spares_the_version_probe(tmp_path, monkeypatch):
    """system.status must still answer on the host the gate is about to reject."""
    fake_freecad = type(sys)("FreeCAD")
    fake_freecad.Version = lambda: ["1", "2", "0", "", "", ""]
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_freecad)

    payload = _run_driver(monkeypatch, tmp_path, "system.status", {}, "1.2.0")

    assert payload["ok"] is True
    assert payload["result"]["version"] == "1.2.0"
    assert payload["result"]["host_matrix"]["status"] == "too_new"
    assert payload["result"]["host_matrix"]["supported_ranges"] == ["1.0.x", "1.1.x"]


def test_a_supported_host_is_dispatched_normally(tmp_path, monkeypatch):
    """The gate must not block the hosts the matrix covers."""
    dispatched = []

    def fake_add_primitive(params):
        dispatched.append(params)
        return {"object": {"name": params["name"]}}

    monkeypatch.setitem(freecad_driver._METHODS, "model.add_primitive", fake_add_primitive)

    payload = _run_driver(
        monkeypatch,
        tmp_path,
        "model.add_primitive",
        {"document_path": str(tmp_path / "doc.FCStd"), "name": "Body"},
        "1.1.4",
    )

    assert payload["ok"] is True
    assert dispatched and dispatched[0]["name"] == "Body"


class _Mesh:
    CountPoints = 0
    CountFacets = 0


def test_an_empty_tessellation_is_an_explicit_error_not_a_short_file():
    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver._assert_tessellation(_FakeObject(Mesh=_Mesh()), "BodyWithPort", "1.1.4")

    message = str(error.value)
    assert "empty mesh" in message
    assert "BodyWithPort" in message
    assert "1.1.4" in message


def test_a_real_tessellation_passes_the_postcondition():
    class _GoodMesh:
        CountPoints = 120
        CountFacets = 240

    mesh = freecad_driver._assert_tessellation(
        _FakeObject(Mesh=_GoodMesh()), "BodyWithPort", "1.1.4"
    )
    assert mesh.CountFacets == 240


def test_an_unbounded_tessellation_is_an_explicit_error():
    class _Box:
        XLength = float("inf")
        YLength = 2.0
        ZLength = 3.0

    class _UnboundedMesh:
        CountPoints = 120
        CountFacets = 240
        BoundBox = _Box()

    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver._assert_tessellation(
            _FakeObject(Mesh=_UnboundedMesh()), "BodyWithPort", "1.1.4"
        )

    assert "unbounded" in str(error.value)


def test_a_mesh_without_a_bounding_box_is_not_treated_as_unbounded():
    """A thinner host API must not turn the guard into a false rejection."""

    class _ThinMesh:
        CountPoints = 120
        CountFacets = 240

    mesh = freecad_driver._assert_tessellation(
        _FakeObject(Mesh=_ThinMesh()), "BodyWithPort", "1.1.4"
    )
    assert mesh.CountFacets == 240


class _FakeShape:
    def isNull(self):
        return False


class _EmptyMesh:
    CountPoints = 0
    CountFacets = 0


class _FakeDocument:
    Name = "DccMcpExportDoc"

    def __init__(self, objects):
        self._objects = objects
        self.removed = []
        self.added = []

    def getObject(self, name):
        return self._objects.get(name)

    def addObject(self, type_id, name):
        self.added.append(name)
        holder = _FakeObject(Name=name, Mesh=_EmptyMesh())
        self._objects[name] = holder
        return holder

    def removeObject(self, name):
        self.removed.append(name)


def _stub_freecad(monkeypatch, document, version="1.1.4"):
    """Install the smallest FreeCAD surface model_export_geometry needs."""
    fake_freecad = type(sys)("FreeCAD")
    fake_freecad.Version = lambda: list(version.split(".")) + ["", ""]
    fake_freecad.openDocument = lambda path: document
    fake_freecad.closeDocument = lambda name: None
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_freecad)

    mesh = type(sys)("Mesh")
    mesh.export = lambda objects, path: None
    monkeypatch.setitem(sys.modules, "Mesh", mesh)

    mesh_part = type(sys)("MeshPart")
    # A deflection that silently produces nothing, which is the 1.1 failure mode.
    mesh_part.meshFromShape = lambda **kwargs: _EmptyMesh()
    monkeypatch.setitem(sys.modules, "MeshPart", mesh_part)
    return fake_freecad


def test_export_refuses_an_empty_tessellation_instead_of_writing_a_file(monkeypatch, tmp_path):
    """The postcondition must be wired into the export path, not merely exist.

    Without it a changed deflection writes a plausible-looking short STL and
    reports success, which is the failure this matrix exists to prevent.
    """
    document = _FakeDocument({"BodyWithPort": _FakeObject(Name="BodyWithPort", Shape=_FakeShape())})
    _stub_freecad(monkeypatch, document)
    output = tmp_path / "model.stl"

    with pytest.raises(freecad_driver.IncompatibleHostError) as error:
        freecad_driver.model_export_geometry(
            {
                "document_path": str(tmp_path / "doc.FCStd"),
                "object_names": ["BodyWithPort"],
                "output_path": str(output),
                "linear_deflection": 0.1,
                "angular_deflection_degrees": 15,
            }
        )

    assert "empty mesh" in str(error.value)
    assert not output.exists()
    # The throwaway mesh object is cleaned up even when the export is refused.
    assert document.removed == ["DccMcpExportMesh0"]


def test_the_api_probe_reads_module_level_symbols(monkeypatch):
    fake_module = type(sys)("MeshPart")
    fake_module.meshFromShape = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "MeshPart", fake_module)
    monkeypatch.setattr(
        freecad_driver,
        "_probe_instance_property",
        lambda type_id, attribute: "absent",
    )

    probes = {entry["id"]: entry for entry in freecad_driver._probe_breaking_changes("1.1.4")}

    tessellate = probes["meshpart-tessellate-deflection"]
    assert tessellate["observed"] == "present"
    assert tessellate["matches_expected"] is True


def test_the_api_probe_instantiates_a_live_object_for_dynamic_properties(monkeypatch):
    """Many FreeCAD properties exist only on instances, so a live probe is used."""
    monkeypatch.setitem(sys.modules, "MeshPart", type(sys)("MeshPart"))
    monkeypatch.setitem(sys.modules, "Sketcher", type(sys)("Sketcher"))
    calls = []

    def fake_probe(type_id, attribute):
        calls.append((type_id, attribute))
        return "absent" if attribute == "Symmetric" else "present"

    monkeypatch.setattr(freecad_driver, "_probe_instance_property", fake_probe)

    probes = {entry["id"]: entry for entry in freecad_driver._probe_breaking_changes("1.1.4")}

    symmetric = probes["sketcher-symmetric-renamed-to-midplane"]
    assert symmetric["observed"] == "absent"
    assert symmetric["matches_expected"] is True
    # The replacement is probed too: a rename is only really handled if the new
    # spelling is there to move to.
    assert symmetric["replacement_observed"] == "present"
    assert ("Sketcher::SketchObject", "Symmetric") in calls
    assert ("Sketcher::SketchObject", "Midplane") in calls


def test_the_api_probe_is_unavailable_but_never_crashes_on_a_partial_host(monkeypatch):
    monkeypatch.setitem(sys.modules, "MeshPart", None)
    monkeypatch.setitem(sys.modules, "Sketcher", None)

    probes = freecad_driver._probe_breaking_changes("1.1.4")

    assert len(probes) == len(compat.load_matrix()["breaking_changes"])
    for entry in probes:
        assert entry["observed"] == "unavailable"
        assert entry["matches_expected"] is None


def test_the_api_probe_covers_every_declared_break_on_a_pre_change_host(monkeypatch):
    """A pre-change host is probed too, so the whole matrix gets evidence."""
    fake_meshpart = type(sys)("MeshPart")
    fake_meshpart.meshFromShape = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "MeshPart", fake_meshpart)
    monkeypatch.setitem(sys.modules, "Sketcher", type(sys)("Sketcher"))
    monkeypatch.setattr(
        freecad_driver, "_probe_instance_property", lambda type_id, attribute: "present"
    )

    probes = freecad_driver._probe_breaking_changes("1.0.2")

    assert {entry["id"] for entry in probes} == {
        entry["id"] for entry in compat.load_matrix()["breaking_changes"]
    }
    assert all(entry["applies_to_host"] is False for entry in probes)
    assert all(entry["observed"] != "unavailable" for entry in probes)


def test_the_api_probe_flags_evidence_drift_instead_of_hiding_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "MeshPart", type(sys)("MeshPart"))
    monkeypatch.setitem(sys.modules, "Sketcher", type(sys)("Sketcher"))
    monkeypatch.setattr(
        freecad_driver,
        "_probe_instance_property",
        lambda type_id, attribute: "present",
    )

    probes = {entry["id"]: entry for entry in freecad_driver._probe_breaking_changes("1.1.4")}

    symmetric = probes["sketcher-symmetric-renamed-to-midplane"]
    assert symmetric["observed"] == "present"
    assert symmetric["expected"] == "absent"
    assert symmetric["matches_expected"] is False


def test_an_unverified_expectation_records_evidence_without_claiming_a_match(monkeypatch):
    """A pre-change claim the real host cannot confirm stays unverified."""
    fake_meshpart = type(sys)("MeshPart")
    fake_meshpart.meshFromShape = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "MeshPart", fake_meshpart)
    monkeypatch.setitem(sys.modules, "Sketcher", type(sys)("Sketcher"))
    monkeypatch.setattr(
        freecad_driver, "_probe_instance_property", lambda type_id, attribute: "absent"
    )

    probes = {entry["id"]: entry for entry in freecad_driver._probe_breaking_changes("1.0.2")}

    symmetric = probes["sketcher-symmetric-renamed-to-midplane"]
    assert symmetric["expected"] == "unverified"
    assert symmetric["observed"] == "absent"
    assert symmetric["matches_expected"] is None
    # The declared break is still reported for the host that it applies to.
    assert probes["meshpart-tessellate-deflection"]["matches_expected"] is True


def test_a_live_property_probe_uses_a_throwaway_document(monkeypatch):
    created = []
    closed = []

    class _Object:
        Symmetric = True

    class _Document:
        Name = "DccMcpCompatProbe"

        def addObject(self, type_id, name):
            created.append((type_id, name))
            return _Object()

    fake_freecad = type(sys)("FreeCAD")
    fake_freecad.newDocument = lambda name: _Document()
    fake_freecad.closeDocument = lambda name: closed.append(name)
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_freecad)

    assert freecad_driver._probe_instance_property("Sketcher::SketchObject", "Symmetric") == (
        "present"
    )
    assert created == [("Sketcher::SketchObject", "DccMcpProbeObject")]
    # The probe document is always closed, even though nothing was saved.
    assert closed == ["DccMcpCompatProbe"]


# --------------------------------------------------------------------------
# Real host: the matrix is checked against the FreeCAD that CI installed
# --------------------------------------------------------------------------


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_version_is_covered_by_the_compatibility_matrix(tmp_path):
    """Runs on every real CI leg (1.0.x and 1.1.x) as matrix evidence."""
    from dcc_mcp_freecad.bridge import FreecadBridge

    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    status = bridge.status()

    version = status["version"]
    host_matrix = status["host_matrix"]

    assert host_matrix["status"] == compat.SUPPORTED, (
        "FreeCAD %s is not covered by the compatibility matrix; add a verified range for it "
        "instead of running unverified" % version
    )
    assert host_matrix["range"]["id"] == "%d.%d.x" % (
        compat.parse_version(version)[0],
        compat.parse_version(version)[1],
    )
    # Every declared break is probed on every supported host, so the real legs
    # keep producing evidence for the whole matrix, not just for this version.
    probes = {entry["id"]: entry for entry in status["api_probe"]}
    assert set(probes) == {entry["id"] for entry in compat.load_matrix()["breaking_changes"]}
    # Hard gate: the API this adapter actually calls must exist on this host.
    assert probes["meshpart-tessellate-deflection"]["observed"] == "present"
    # Declared breaks are evidence, not a gate: drift is surfaced loudly so the
    # matrix can be corrected, instead of being silently absorbed.
    for entry in status["api_probe"]:
        if entry["matches_expected"] is False:
            warnings.warn(
                "FreeCAD %s disagrees with the compatibility matrix for %s: expected %s, "
                "observed %s" % (version, entry["id"], entry["expected"], entry["observed"]),
                stacklevel=2,
            )
