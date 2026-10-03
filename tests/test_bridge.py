from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge, WriteVerificationError


class FakeFreecad(FreecadBridge):
    def __init__(self, root: Path):
        super().__init__(allowed_roots=[root])
        self.executable = "fake-freecadcmd"
        self.calls = []
        self.fail_method = None
        # Set together with ``fail_method``: fail with a structured read-back
        # mismatch instead of a plain error, so a test can inspect the payload.
        self.fail_verification = None

    def _invoke(self, method, params, timeout_secs=120):
        self.calls.append((method, dict(params), timeout_secs))
        if method == self.fail_method:
            document = params.get("document_path")
            if document:
                Path(document).write_bytes(b"partial-corruption")
            if self.fail_verification is not None:
                raise WriteVerificationError(
                    {
                        "tool": method,
                        "check": self.fail_verification,
                        "expected": 84.0,
                        "actual": 80.0,
                        "host_version": "1.1.4",
                        "params": dict(params),
                    },
                    "simulated read-back mismatch",
                )
            raise BridgeError("simulated host failure on %s" % (document or ""))
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


def test_a_failed_mutation_reports_the_callers_document_path(tmp_path: Path):
    """A mismatch must name a path the caller can still open.

    The staging copy is deleted before the error surfaces, so a payload that
    still points at it sends whoever follows the report to a file that is gone.
    """
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)
    bridge.fail_method = "model.add_primitive"
    bridge.fail_verification = "dimension.Length"

    with pytest.raises(WriteVerificationError) as excinfo:
        bridge.add_primitive(str(document), "box", "Body", dimensions={"length": 84})

    reported = excinfo.value.payload["params"]["document_path"]
    assert reported == str(document)
    assert ".model." not in reported
    assert not list(tmp_path.glob(".model.*.FCStd"))


def test_a_plain_host_failure_also_loses_the_staged_path(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)
    bridge.fail_method = "model.add_primitive"

    with pytest.raises(BridgeError) as excinfo:
        bridge.add_primitive(str(document), "box", "Body")

    message = str(excinfo.value)
    assert str(document) in message, "the caller's path must survive in the message"
    assert ".model." not in message, "the deleted staging copy must not"


def test_a_failed_export_reports_the_callers_output_path(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    output = tmp_path / "model.step"
    bridge = FakeFreecad(tmp_path)
    bridge.fail_method = "model.export_geometry"
    bridge.fail_verification = "artifact.solids"

    with pytest.raises(WriteVerificationError) as excinfo:
        bridge.export_geometry(str(document), ["Body"], str(output))

    reported = excinfo.value.payload["params"]["output_path"]
    assert reported == str(output)
    assert ".model." not in reported


def test_a_failed_copy_reports_the_callers_output_path(tmp_path: Path):
    source = tmp_path / "model.FCStd"
    source.write_bytes(b"known-good")
    output = tmp_path / "copy.FCStd"
    bridge = FakeFreecad(tmp_path)
    bridge.fail_method = "document.save_copy"
    bridge.fail_verification = "copy.objects"

    with pytest.raises(WriteVerificationError) as excinfo:
        bridge.save_copy(str(source), str(output))

    reported = excinfo.value.payload["params"]["output_path"]
    assert reported == str(output)
    assert ".copy." not in reported


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


def _linkless(code: int):
    """Stand in for a volume where ``os.link`` cannot work (FAT, exFAT, SMB)."""

    def link(_source, _target, **_kwargs):
        raise OSError(code, "hard links are not supported on this volume")

    return link


def test_plain_copy_publishes_without_overwrite(tmp_path: Path):
    """The plain copy path is what an agent hits without an opt-in selection."""
    source = tmp_path / "model.FCStd"
    source.write_bytes(b"known-good")
    output = tmp_path / "copy.FCStd"
    bridge = FakeFreecad(tmp_path)

    result = bridge.save_copy(str(source), str(output))

    assert output.read_bytes() == b"copied-fcstd"
    assert result["output_path"] == str(output)
    assert result["bytes"] == len(b"copied-fcstd")
    assert result["overwritten"] is False
    assert source.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".copy.*")), "the owned stage must not survive"


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP])
def test_plain_copy_falls_back_on_a_volume_without_hardlinks(tmp_path, monkeypatch, code):
    """A linkless volume must publish by exclusive copy, not fail.

    Every CI runner is ext4 or NTFS, so this branch is structurally unreachable
    there; only an injected ``os.link`` failure pins it.
    """
    source = tmp_path / "model.FCStd"
    source.write_bytes(b"known-good")
    output = tmp_path / "copy.FCStd"
    bridge = FakeFreecad(tmp_path)
    attempts = []

    def link(_source, _target, **_kwargs):
        attempts.append(True)
        raise OSError(code, "hard links are not supported on this volume")

    monkeypatch.setattr("os.link", link)

    result = bridge.save_copy(str(source), str(output))

    assert attempts == [True], "the hard-link publication is still attempted first"
    assert output.read_bytes() == b"copied-fcstd"
    assert result["output_path"] == str(output)
    assert result["bytes"] == len(b"copied-fcstd")
    assert result["overwritten"] is False
    assert source.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".copy.*")), "the owned stage must not survive"


def test_the_fallback_still_refuses_a_destination_from_the_native_call(tmp_path, monkeypatch):
    """``O_EXCL`` must keep the TOCTOU fix even without hard links."""
    source = tmp_path / "model.FCStd"
    target = tmp_path / "copy.FCStd"
    source.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    def native(_method, params, _timeout):
        Path(params["output_path"]).write_bytes(b"native copy")
        target.write_bytes(b"concurrent-destination")
        return {"object_count": 1}

    monkeypatch.setattr("os.link", _linkless(errno.ENOTSUP))
    monkeypatch.setattr(bridge, "_invoke", native)

    with pytest.raises(BridgeError, match="Output already exists"):
        bridge.save_copy(str(source), str(target))

    assert target.read_bytes() == b"concurrent-destination"
    assert not list(tmp_path.glob(".copy.*"))


def test_a_volume_without_hardlinks_reports_a_bridge_error(tmp_path, monkeypatch):
    """The agent must be told what failed, not handed a bare ``OSError``."""
    source = tmp_path / "model.FCStd"
    output = tmp_path / "copy.FCStd"
    source.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    def exhausted(_source, _destination, *args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on the output volume")

    monkeypatch.setattr("os.link", _linkless(errno.EPERM))
    monkeypatch.setattr(shutil, "copyfileobj", exhausted)

    with pytest.raises(BridgeError) as excinfo:
        bridge.save_copy(str(source), str(output))

    message = str(excinfo.value)
    assert "hard link" in message, "the message must name the real cause"
    assert "overwrite=true" in message, "the message must name the escape hatch"
    assert not output.exists(), "a partial publication must not be left behind"
    assert source.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".copy.*"))


def test_the_overwrite_escape_hatch_does_not_need_hardlinks(tmp_path, monkeypatch):
    source = tmp_path / "model.FCStd"
    target = tmp_path / "copy.FCStd"
    source.write_bytes(b"known-good")
    target.write_bytes(b"older-copy")
    bridge = FakeFreecad(tmp_path)
    monkeypatch.setattr("os.link", _linkless(errno.EPERM))

    result = bridge.save_copy(str(source), str(target), overwrite=True)

    assert target.read_bytes() == b"copied-fcstd"
    assert result["overwritten"] is True
    assert source.read_bytes() == b"known-good"


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_read_back_reaches_the_caller_structured(tmp_path: Path):
    """A disagreement inside the host must reach the caller as a mismatch.

    A box a nanometre across is accepted by the property write and then rejected
    by the kernel, so the object exists with the requested dimensions while its
    shape is unusable. That is precisely the shape of the bug this contract
    exists for: the call looks like it worked, and the geometry is not there.

    What is pinned here is the failure, not the value: an ``isinstance``
    -checkable error naming the tool, the check, both sides, and the host
    version, with the document left untouched behind it.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "read-back.FCStd"
    bridge.create_document(str(document))

    with pytest.raises(WriteVerificationError) as excinfo:
        bridge.add_primitive(
            str(document),
            "box",
            "Body",
            dimensions={"length": 1e-9, "width": 1e-9, "height": 1e-9},
        )

    error = excinfo.value
    assert isinstance(error, BridgeError), "existing handlers must still catch it"
    assert error.tool == "model.add_primitive"
    assert error.check
    assert "expected" in error.payload and "actual" in error.payload
    assert error.host_version == bridge.status()["version"]

    # The mutation was refused, so the document must be exactly as it was.
    inspected = bridge.inspect_document(str(document))
    assert inspected["object_count"] == 0


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
