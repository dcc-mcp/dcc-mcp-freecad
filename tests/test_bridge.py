from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge


class FakeFreecad(FreecadBridge):
    def __init__(self, root: Path):
        super().__init__(allowed_roots=[root])
        self.executable = "fake-freecadcmd"
        self.calls = []
        self.fail_method = None

    def _invoke(self, method, params, timeout_secs=120):
        self.calls.append((method, dict(params), timeout_secs))
        if method == self.fail_method:
            document = params.get("document_path")
            if document:
                Path(document).write_bytes(b"partial-corruption")
            raise BridgeError("simulated host failure")
        if method == "system.status":
            return {"version": "1.1.3", "python_version": "3.11.14"}
        if method == "document.create":
            Path(params["document_path"]).write_bytes(b"empty-fcstd")
            return {"file_name": params["document_path"], "object_count": 0}
        if method == "document.inspect":
            path = Path(params["document_path"])
            return {
                "file_name": params["document_path"],
                "name": path.stem.replace("-", "_"),
                "label": path.stem,
                "object_count": 0,
            }
        if method == "model.export_geometry":
            Path(params["output_path"]).write_bytes(b"geometry")
            return {"format": Path(params["output_path"]).suffix[1:]}
        if method.startswith("model.") or method == "document.remove_object":
            document = Path(params["document_path"])
            document.write_bytes(document.read_bytes() + b"-mutated")
            document.with_name("%s.20260811-020000.FCBak" % document.stem).write_bytes(b"backup")
            return {"document": {"file_name": params["document_path"]}}
        if method == "document.save_copy":
            Path(params["output_path"]).write_bytes(b"copied-fcstd")
            return {"object_count": 1}
        return {"object_count": 0}


def test_discovery_accepts_a_macos_application_bundle(tmp_path: Path):
    application = tmp_path / "FreeCAD.app"
    executable = application / "Contents" / "Resources" / "bin" / "FreeCADCmd"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")

    assert FreecadBridge._resolve_executable(str(application)) == str(executable.resolve())


def test_status_and_capabilities_are_explicit(tmp_path: Path):
    bridge = FakeFreecad(tmp_path)

    status = bridge.status()
    capabilities = bridge.capabilities()

    assert status["ready"] is True
    assert status["version"] == "1.1.3"
    assert status["instance_type"] == "standalone"
    assert capabilities["atomic_document_mutations"] is True
    assert capabilities["arbitrary_python"] is False
    assert "boolean_operation" in capabilities["methods"]


def test_paths_are_workspace_bounded(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.FCStd"
    outside.write_bytes(b"document")

    with pytest.raises(BridgeError, match="outside"):
        FakeFreecad(allowed).inspect_document(str(outside))


def test_create_document_is_atomic_and_normalizes_staged_paths(tmp_path: Path):
    bridge = FakeFreecad(tmp_path)
    output = tmp_path / "model.FCStd"

    result = bridge.create_document(str(output))

    assert output.read_bytes() == b"empty-fcstd"
    assert result["file_name"] == str(output)
    assert result["overwritten"] is False
    assert len(result["document_sha256"]) == 64
    assert not list(tmp_path.glob(".model.*.FCStd"))


def test_failed_mutation_preserves_original_bytes(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)
    bridge.fail_method = "model.add_primitive"

    with pytest.raises(BridgeError, match="simulated"):
        bridge.add_primitive(str(document), "box", "Body")

    assert document.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".model.*.FCStd"))


def test_successful_mutation_replaces_document_and_reports_provenance(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    result = bridge.add_primitive(
        str(document),
        "box",
        "Body",
        dimensions={"length": 10, "width": 8, "height": 4},
    )

    assert document.read_bytes() == b"known-good-mutated"
    assert result["document"]["file_name"] == str(document)
    assert result["document"]["label"] == "model"
    assert result["document_bytes"] == len(b"known-good-mutated")
    assert len(result["document_sha256"]) == 64
    assert not list(tmp_path.glob(".*.FCBak"))


def test_object_names_and_update_dimensions_are_validated(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"document")
    bridge = FakeFreecad(tmp_path)

    with pytest.raises(BridgeError, match="Object names"):
        bridge.add_primitive(str(document), "box", "bad name")
    with pytest.raises(BridgeError, match="at least one"):
        bridge.update_primitive(str(document), "Body", {})


def test_export_refuses_implicit_overwrite(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    output = tmp_path / "model.step"
    document.write_bytes(b"document")
    output.write_bytes(b"existing")
    bridge = FakeFreecad(tmp_path)

    with pytest.raises(BridgeError, match="overwrite=true"):
        bridge.export_geometry(str(document), ["Body"], str(output))

    result = bridge.export_geometry(str(document), ["Body"], str(output), overwrite=True)
    assert output.read_bytes() == b"geometry"
    assert result["overwritten"] is True


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_document_modeling_and_exchange(tmp_path: Path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "production-smoke.FCStd"

    created = bridge.create_document(
        str(document),
    )
    bridge.add_primitive(
        str(document),
        "box",
        "Body",
        dimensions={"length": 80, "width": 50, "height": 24},
    )
    bridge.update_primitive(str(document), "Body", {"length": 84})
    bridge.add_primitive(
        str(document),
        "cylinder",
        "PortCut",
        dimensions={"radius": 6, "height": 20},
    )
    bridge.transform_object(
        str(document),
        "PortCut",
        translation=[42, 25, 12],
        rotation_axis=[0, 1, 0],
        rotation_degrees=90,
    )
    bridge.boolean_operation(str(document), "cut", "Body", "PortCut", "BodyWithPort")
    validation = bridge.validate_document(str(document))
    inspected = bridge.inspect_document(str(document))
    step = bridge.export_geometry(str(document), ["BodyWithPort"], str(tmp_path / "model.step"))
    stl = bridge.export_geometry(str(document), ["BodyWithPort"], str(tmp_path / "model.stl"))

    assert created["document_sha256"]
    assert validation["valid"] is True
    assert inspected["label"] == "production-smoke"
    result_object = next(obj for obj in inspected["objects"] if obj["name"] == "BodyWithPort")
    assert result_object["shape"]["valid"] is True
    assert result_object["shape"]["solids"] == 1
    assert step["bytes"] > 100 and stl["bytes"] > 84

    imported_document = tmp_path / "imported.FCStd"
    bridge.create_document(str(imported_document))
    imported = bridge.import_geometry(
        str(imported_document), str(tmp_path / "model.step"), "ImportedBody"
    )
    copied = bridge.save_copy(str(imported_document), str(tmp_path / "imported-copy.FCStd"))
    assert imported["object"]["shape"]["valid"] is True
    assert copied["bytes"] > 0

    before_failure = document.read_bytes()
    with pytest.raises(BridgeError, match="dependents"):
        bridge.remove_object(str(document), "Body")
    assert document.read_bytes() == before_failure
    removed = bridge.remove_object(str(document), "Body", cascade=True)
    assert set(removed["removed_objects"]) == {"Body", "BodyWithPort"}
    assert not list(tmp_path.glob(".*.FCBak"))
