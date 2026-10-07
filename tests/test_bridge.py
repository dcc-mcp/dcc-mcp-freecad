from __future__ import annotations

import errno
import os
import shutil
import stat
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
        if method == "sketch.info":
            # Read-only: a sketch inspection must not touch the document it read.
            return {"object_count": 0, "dof": 0}
        if (
            method.startswith("model.")
            or method.startswith("sketch.")
            or method == "document.remove_object"
        ):
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


def test_transform_additions_forward_their_parameters(tmp_path: Path):
    """The three new tools must reach the driver with what the caller asked."""
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"document")
    bridge = FakeFreecad(tmp_path)

    bridge.scale_object(str(document), "Body", [1, 2, 3], "BodyScaled", around="origin")
    method, params, _ = bridge.calls[0]
    assert method == "model.scale_object"
    assert params["object_name"] == "Body"
    assert params["scale"] == [1, 2, 3]
    assert params["result_name"] == "BodyScaled"
    assert params["around"] == "origin"

    bridge.copy_object(
        str(document), "Body", "BodyCopy", translation=[10, 0, 0], rotation_degrees=90
    )
    method, params, _ = bridge.calls[-2]
    assert method == "model.copy_object"
    assert params["new_name"] == "BodyCopy"
    assert params["translation"] == [10, 0, 0]
    assert params["rotation_degrees"] == 90

    bridge.mirror_object(
        str(document), "Body", "BodyMirror", plane="yz", origin=[40, 0, 0], keep_source=False
    )
    method, params, _ = bridge.calls[-2]
    assert method == "model.mirror_object"
    assert params["result_name"] == "BodyMirror"
    assert params["plane"] == "yz"
    assert params["origin"] == [40, 0, 0]
    assert params["keep_source"] is False


def test_new_object_names_are_validated_before_the_host_runs(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"document")
    bridge = FakeFreecad(tmp_path)

    with pytest.raises(BridgeError, match="Object names"):
        bridge.scale_object(str(document), "Body", 2, "bad name")
    with pytest.raises(BridgeError, match="Object names"):
        bridge.copy_object(str(document), "Body", "bad name")
    with pytest.raises(BridgeError, match="Object names"):
        bridge.mirror_object(str(document), "Body", "bad name", plane="xy")


def test_export_geometry_accepts_3mf(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"document")
    bridge = FakeFreecad(tmp_path)

    result = bridge.export_geometry(str(document), ["Body"], str(tmp_path / "model.3mf"))

    assert result["format"] == "3mf"
    # The host writes to a staged path; only the requested extension is stable.
    assert bridge.calls[-1][1]["output_path"].endswith(".3mf")


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


@pytest.mark.parametrize("stage_mode", [0o644, 0o400], ids=["umask-narrowing", "umask-widening"])
@pytest.mark.parametrize("link_failures", [None, errno.EPERM], ids=["hard-link", "fallback-copy"])
def test_both_publication_paths_keep_the_stage_mode(
    tmp_path, monkeypatch, link_failures, stage_mode
):
    """The fallback must publish the mode the host gave the stage, either way.

    A hard link shares the stage inode, so a fallback that lets the umask decide
    disagrees with it in both directions: ``0666 & ~umask`` widens a ``0600``
    stage, and ``os.open`` masks a ``0644`` stage down to ``0600`` under a
    ``077`` umask. The volumes that need the fallback are the shared mounts
    where either direction is visible to other users.

    The umask is raised to ``0o077`` for the call so the narrowing direction is
    reproducible: ``os.open`` masks the mode with it, and only the post-copy
    ``chmod`` restores the staged mode. ``0o400`` gives the assertion a
    discriminating value on Windows too, where a read-only file is reported as
    ``0o444``; POSIX reports the mode as set and masks it through the umask.
    """
    source = tmp_path / "model.FCStd"
    output = tmp_path / "copy.FCStd"
    source.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)
    staged_modes = []

    def native(_method, params, _timeout):
        staged = Path(params["output_path"])
        staged.write_bytes(b"copied-fcstd")
        os.chmod(staged, stage_mode)
        staged_modes.append(stat.S_IMODE(staged.stat().st_mode))
        return {"object_count": 1}

    monkeypatch.setattr(bridge, "_invoke", native)
    if link_failures is not None:
        monkeypatch.setattr("os.link", _linkless(link_failures))

    original_umask = os.umask(0o077)
    try:
        bridge.save_copy(str(source), str(output))
    finally:
        os.umask(original_umask)

    assert staged_modes == [stat.S_IMODE(output.stat().st_mode)]
    assert output.read_bytes() == b"copied-fcstd"


@pytest.mark.parametrize("stage_mode", [0o444, 0o400])
def test_a_failed_fallback_leaves_no_fragment_on_a_read_only_stage(
    tmp_path, monkeypatch, stage_mode
):
    """A failed copy must always be able to discard its own partial output.

    A read-only stage mode makes the exclusive create produce a read-only
    target, and on Windows ``unlink`` then refuses it: the fragment survives on
    the publication path - exactly where the next call reads and replaces - and
    the swallowed ``PermissionError`` leaves no trace of why. The create adds
    owner-write so cleanup can always run; the staged mode is only committed
    once the copy has succeeded.

    POSIX can delete a read-only file from a writable directory, so the
    discriminating evidence is on the Windows legs; the contract it locks - a
    failed publication leaves nothing behind on the output path - is platform
    independent. The owned stage is cleaned up on a best-effort basis and is
    not asserted here.
    """
    source = tmp_path / "model.FCStd"
    output = tmp_path / "copy.FCStd"
    source.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    def native(_method, params, _timeout):
        staged = Path(params["output_path"])
        staged.write_bytes(b"copied-fcstd")
        os.chmod(staged, stage_mode)
        return {"object_count": 1}

    def exhausted(_source, _destination, *args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on the output volume")

    monkeypatch.setattr(bridge, "_invoke", native)
    monkeypatch.setattr("os.link", _linkless(errno.ENOTSUP))
    monkeypatch.setattr(shutil, "copyfileobj", exhausted)

    with pytest.raises(BridgeError):
        bridge.save_copy(str(source), str(output))

    assert not output.exists(), "a failed publication must not leave a fragment"
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
def test_real_freecad_scale_copy_and_mirror(tmp_path: Path):
    """Scaling, copying and mirroring must move geometry, not just report it.

    Each operation is checked against the source's own measured bounds rather
    than against a hard-coded number, so the assertions hold on both supported
    release lines and still fail if the host starts ignoring the request.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "transforms.FCStd"
    bridge.create_document(str(document))
    bridge.add_primitive(
        str(document), "box", "Body", dimensions={"length": 20, "width": 10, "height": 5}
    )
    bridge.transform_object(str(document), "Body", translation=[100, 0, 0])
    source = _named_object(bridge, document, "Body")
    size = source["shape"]["bounding_box"]["size"]
    centre = source["shape"]["bounding_box"]["center"]
    assert size == pytest.approx([20, 10, 5], abs=1e-6)

    scaled = bridge.scale_object(str(document), "Body", 2.0, "BodyScaled")
    scaled_object = scaled["object"]
    assert scaled_object["type_id"] == "Part::Feature"
    assert scaled_object["shape"]["bounding_box"]["size"] == pytest.approx(
        [value * 2 for value in size], abs=1e-6
    )
    # about the centroid leaves the centre where it was
    assert scaled_object["shape"]["bounding_box"]["center"] == pytest.approx(centre, abs=1e-6)

    stretched = bridge.scale_object(
        str(document), "Body", [1, 1, 3], "BodyStretched", around="origin"
    )
    stretched_object = stretched["object"]
    assert stretched_object["shape"]["bounding_box"]["size"] == pytest.approx(
        [size[0], size[1], size[2] * 3], abs=1e-6
    )
    assert stretched_object["shape"]["bounding_box"]["center"][2] == pytest.approx(
        centre[2] * 3, abs=1e-6
    )

    copied = bridge.copy_object(str(document), "Body", "BodyCopy", translation=[0, 50, 0])
    copied_object = copied["object"]
    # A copy of a primitive stays a primitive, so it can still be re-dimensioned.
    assert copied_object["type_id"] == "Part::Box"
    assert copied_object["placement"]["translation"] == pytest.approx([0, 50, 0], abs=1e-9)
    assert copied_object["shape"]["bounding_box"]["size"] == pytest.approx(size, abs=1e-6)
    # The copy takes the requested placement absolutely, like transform_object.
    assert copied_object["shape"]["bounding_box"]["center"] == pytest.approx(
        [size[0] / 2, 50 + size[1] / 2, size[2] / 2], abs=1e-6
    )
    assert _named_object(bridge, document, "Body") is not None, "the copy must not consume it"

    mirrored = bridge.mirror_object(
        str(document), "Body", "BodyMirror", plane="yz", origin=[60, 0, 0]
    )
    mirrored_object = mirrored["object"]
    assert mirrored_object["type_id"] == "Part::Mirroring"
    assert mirrored_object["shape"]["bounding_box"]["size"] == pytest.approx(size, abs=1e-6)
    # Reflection of the centre through the plane x = 60.
    assert mirrored_object["shape"]["bounding_box"]["center"] == pytest.approx(
        [2 * 60 - centre[0], centre[1], centre[2]], abs=1e-6
    )
    assert mirrored["document_validation"] == {"invalid_objects": [], "empty_shape_objects": []}

    # Dropping the source bakes the mirrored geometry instead of keeping a link
    # that would outlive what it points at.
    bridge.add_primitive(str(document), "cylinder", "Lone", dimensions={"radius": 4, "height": 12})
    baked = bridge.mirror_object(
        str(document), "Lone", "LoneMirror", normal=[1, 0, 0], keep_source=False
    )
    assert baked["object"]["type_id"] == "Part::Feature"
    assert baked["object"]["shape"]["solids"] == 1
    names = {obj["name"] for obj in bridge.inspect_document(str(document))["objects"]}
    assert "Lone" not in names and "LoneMirror" in names

    validation = bridge.validate_document(str(document))
    assert validation["valid"] is True, validation

    # Refusals: a degenerate factor, a taken name, and an ambiguous plane are
    # rejected before anything is written rather than modelled and then found.
    before = document.read_bytes()
    with pytest.raises(BridgeError, match="greater than zero"):
        bridge.scale_object(str(document), "Body", 0, "Nope")
    with pytest.raises(BridgeError, match="greater than zero"):
        bridge.scale_object(str(document), "Body", -2, "Nope")
    with pytest.raises(BridgeError, match="already exists"):
        bridge.copy_object(str(document), "Body", "BodyCopy")
    with pytest.raises(BridgeError, match="exactly one of plane or normal"):
        bridge.mirror_object(str(document), "Body", "Nope", plane="xy", normal=[0, 0, 1])
    with pytest.raises(BridgeError, match="dependents"):
        bridge.mirror_object(str(document), "Body", "Nope", plane="yz", keep_source=False)
    assert document.read_bytes() == before, "a refused call must not touch the document"


def _named_object(bridge, document, name):
    """Fetch one object's payload out of a real document inspection."""
    for obj in bridge.inspect_document(str(document))["objects"]:
        if obj["name"] == name:
            return obj
    return None


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_copy_places_every_source_type_the_same_way(tmp_path: Path):
    """``translation`` must mean the same thing whatever the source is made of.

    A primitive is rebuilt from its dimensions, so its geometry starts in local
    coordinates; a boolean result and a mesh are copied from ``Shape`` / ``Mesh``,
    which FreeCAD reports already carrying the source's placement. Without the
    copies being rebased, the same ``translation`` lands a primitive at the
    document origin and a boolean result on top of its source - a "copy" that
    looks like a no-op. This covers the two branches the primitive-only case
    cannot reach, with a non-zero translation so the difference is measurable.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "copy-semantics.FCStd"
    bridge.create_document(str(document))

    # A boolean result: Part::Cut, so it takes the shape branch, not the
    # primitive branch. Placed away from the origin on purpose.
    bridge.add_primitive(
        str(document), "box", "Blank", dimensions={"length": 20, "width": 10, "height": 5}
    )
    bridge.add_primitive(
        str(document), "box", "Tool", dimensions={"length": 6, "width": 10, "height": 5}
    )
    bridge.transform_object(str(document), "Tool", translation=[100, 40, 0])
    bridge.transform_object(str(document), "Blank", translation=[100, 40, 0])
    bridge.boolean_operation(str(document), "cut", "Blank", "Tool", "Notched")
    source = _named_object(bridge, document, "Notched")
    size = source["shape"]["bounding_box"]["size"]
    source_centre = source["shape"]["bounding_box"]["center"]
    # The tool sits at the blank's left end, so the cut removes the first 6mm:
    # the result spans x 106..120 rather than starting at the placement origin.
    assert size == pytest.approx([14, 10, 5], abs=1e-6)
    assert source_centre[0] == pytest.approx(100 + 6 + size[0] / 2, abs=1e-6)

    offset = [0, 50, 0]
    copied = bridge.copy_object(str(document), "Notched", "NotchedCopy", translation=offset)
    copied_object = copied["object"]
    # A boolean result has no parametric definition to carry over.
    assert copied_object["type_id"] == "Part::Feature"
    assert copied_object["placement"]["translation"] == pytest.approx(offset, abs=1e-9)
    assert copied_object["shape"]["bounding_box"]["size"] == pytest.approx(size, abs=1e-6)
    # Absolute, like the primitive branch: the requested offset, not the source
    # position plus the offset.
    assert copied_object["shape"]["bounding_box"]["center"] == pytest.approx(
        [offset[0] + size[0] / 2, offset[1] + size[1] / 2, offset[2] + size[2] / 2], abs=1e-6
    )

    # The default is the same absolute convention the primitive branch already
    # uses: an untranslated copy lands at the document origin, so the source's
    # own position is not silently added to it.
    plain = bridge.copy_object(str(document), "Notched", "NotchedSame")
    plain_object = plain["object"]
    assert plain_object["shape"]["bounding_box"]["center"] == pytest.approx(
        [size[0] / 2, size[1] / 2, size[2] / 2], abs=1e-6
    )

    # A mesh: the third branch, also placed away from the origin. The mesh is
    # tessellated and re-imported, because a Mesh::Feature is the only source
    # that reaches the mesh branch.
    bridge.add_primitive(
        str(document), "box", "MeshSource", dimensions={"length": 12, "width": 8, "height": 4}
    )
    bridge.transform_object(str(document), "MeshSource", translation=[0, 80, 0])
    tessellated = tmp_path / "source.stl"
    # The format comes from the suffix, which is why the mesh has to make a
    # round trip through a file instead of being built directly.
    bridge.export_geometry(str(document), ["MeshSource"], str(tessellated))
    bridge.import_geometry(str(document), str(tessellated), "MeshObject")
    mesh_source = _named_object(bridge, document, "MeshObject")
    assert mesh_source["type_id"] == "Mesh::Feature", mesh_source["type_id"]
    mesh_box = mesh_source["mesh"]["bounding_box"]
    mesh_centre = mesh_box["center"]
    # The source must sit away from the origin, or the two conventions agree
    # and the assertion below would pass either way.
    assert mesh_centre[1] == pytest.approx(80 + mesh_box["size"][1] / 2, abs=1e-6)

    mesh_copy = bridge.copy_object(str(document), "MeshObject", "MeshCopy", translation=offset)
    assert mesh_copy["object"]["type_id"] == "Mesh::Feature"
    copied_box = mesh_copy["object"]["mesh"]["bounding_box"]
    assert copied_box["size"] == pytest.approx(mesh_box["size"], abs=1e-6)
    # Same absolute convention as the shape branch: the source's own position
    # must not be added on top of the requested offset.
    assert copied_box["center"] == pytest.approx(
        [
            offset[0] + mesh_box["size"][0] / 2,
            offset[1] + mesh_box["size"][1] / 2,
            offset[2] + mesh_box["size"][2] / 2,
        ],
        abs=1e-6,
    )

    assert bridge.validate_document(str(document))["valid"] is True


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_copy_rotation_is_absolute_for_a_primitive(tmp_path: Path):
    """A primitive copy takes the requested rotation, not the source's.

    A primitive is re-created from its dimensions, so it has no orientation of
    its own to inherit and the request is the whole answer. The assertion is
    written as "the copy of a rotated source equals the copy of an unrotated
    one" because that is the property the contract promises and it does not
    depend on how a given host normalises an axis/angle pair.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "copy-rotation.FCStd"
    bridge.create_document(str(document))

    bridge.add_primitive(
        str(document), "box", "Flat", dimensions={"length": 20, "width": 10, "height": 5}
    )
    bridge.add_primitive(
        str(document), "box", "Turned", dimensions={"length": 20, "width": 10, "height": 5}
    )
    bridge.transform_object(str(document), "Turned", rotation_axis=[0, 0, 1], rotation_degrees=35)

    flat_copy = bridge.copy_object(
        str(document), "Flat", "FlatCopy", rotation_axis=[0, 0, 1], rotation_degrees=30
    )
    turned_copy = bridge.copy_object(
        str(document), "Turned", "TurnedCopy", rotation_axis=[0, 0, 1], rotation_degrees=30
    )

    flat_box = flat_copy["object"]["shape"]["bounding_box"]
    turned_box = turned_copy["object"]["shape"]["bounding_box"]
    # The source's own 35 degrees must not leak into the copy: both copies carry
    # exactly the requested 30, so their extents match.
    assert turned_box["size"] == pytest.approx(flat_box["size"], abs=1e-6)
    assert bridge.validate_document(str(document))["valid"] is True


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_copy_rotation_composes_on_a_baked_orientation(tmp_path: Path):
    """A shape copy keeps the orientation baked into the source's geometry.

    A boolean result holds its orientation in its coordinates and carries an
    identity ``Placement``, so there is nothing for the rebase to invert and the
    request composes on top. This pins that documented behaviour rather than
    leaving it to drift: a reader (or a future rebase that "fixes" rotation the
    same way translation was fixed) would otherwise have no signal either way.

    The assertion is differential - a rotated request must move the copy's
    bounding box relative to an unrotated copy of the same source - so it holds
    whatever the source's baked orientation happens to be.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "copy-rotation-baked.FCStd"
    bridge.create_document(str(document))

    bridge.add_primitive(
        str(document), "box", "Blank", dimensions={"length": 20, "width": 10, "height": 5}
    )
    bridge.add_primitive(
        str(document), "box", "Tool", dimensions={"length": 6, "width": 10, "height": 5}
    )
    bridge.transform_object(str(document), "Tool", translation=[100, 40, 0])
    bridge.transform_object(str(document), "Blank", translation=[100, 40, 0])
    bridge.boolean_operation(str(document), "cut", "Blank", "Tool", "Notched")

    plain = bridge.copy_object(str(document), "Notched", "NotchedPlain")
    turned = bridge.copy_object(
        str(document), "Notched", "NotchedTurned", rotation_axis=[0, 0, 1], rotation_degrees=90
    )

    plain_box = plain["object"]["shape"]["bounding_box"]
    turned_box = turned["object"]["shape"]["bounding_box"]
    # A 90 degree turn about Z swaps the two horizontal extents, so the copy is
    # measurably different from the unrotated one. Asserting on the swap rather
    # than on an absolute number keeps this true however the source is oriented.
    assert turned_box["size"][0] == pytest.approx(plain_box["size"][1], abs=1e-6)
    assert turned_box["size"][1] == pytest.approx(plain_box["size"][0], abs=1e-6)
    assert turned_box["size"][2] == pytest.approx(plain_box["size"][2], abs=1e-6)
    assert bridge.validate_document(str(document))["valid"] is True


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_wedge_helix_and_3mf(tmp_path: Path):
    """The new primitives and the 3MF exporter, measured against the geometry.

    The helix is the one primitive whose parameterisation could plausibly drift
    between release lines, so its length is compared with the closed-form length
    of the requested helix rather than with a number copied off one host.
    """
    import math

    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "new-surface.FCStd"
    bridge.create_document(str(document))

    wedge = bridge.add_primitive(
        str(document),
        "wedge",
        "Ramp",
        dimensions={
            "xmin": 0,
            "ymin": 0,
            "zmin": 0,
            "x2min": 2,
            "z2min": 2,
            "xmax": 10,
            "ymax": 10,
            "zmax": 10,
            "x2max": 8,
            "z2max": 8,
        },
    )
    wedge_shape = wedge["object"]["shape"]
    assert wedge["object"]["type_id"] == "Part::Wedge"
    assert wedge_shape["solids"] == 1
    assert wedge_shape["valid"] is True
    assert wedge_shape["bounding_box"]["size"] == pytest.approx([10, 10, 10], abs=1e-6)

    updated = bridge.update_primitive(str(document), "Ramp", {"xmax": 20})
    assert updated["object"]["shape"]["bounding_box"]["size"][0] == pytest.approx(20, abs=1e-6)

    pitch, height, radius = 2.5, 9.0, 4.0
    helix = bridge.add_primitive(
        str(document),
        "helix",
        "Spring",
        dimensions={"pitch": pitch, "height": height, "radius": radius, "angle": 0},
    )
    helix_shape = helix["object"]["shape"]
    assert helix["object"]["type_id"] == "Part::Helix"
    assert helix_shape["shape_type"] == "Wire"
    assert helix_shape["valid"] is True
    assert helix_shape["bounding_box"]["size"][2] == pytest.approx(height, abs=1e-6)
    turns = height / pitch
    expected_length = turns * math.hypot(2 * math.pi * radius, pitch)
    assert helix_shape["length"] == pytest.approx(expected_length, rel=1e-3), (
        "the host built a helix of a different length than requested"
    )

    # 3MF goes through the same tessellation as STL, so it must describe the
    # same mesh - and it must declare the unit FreeCAD models in.
    stl = bridge.export_geometry(str(document), ["Ramp"], str(tmp_path / "ramp.stl"))
    three_mf = bridge.export_geometry(str(document), ["Ramp"], str(tmp_path / "ramp.3mf"))
    assert three_mf["format"] == "3mf"
    assert three_mf["unit"] == "millimeter"
    assert three_mf["bytes"] > 0 and len(three_mf["sha256"]) == 64
    assert "artifact.unit" in three_mf["verified"]
    assert three_mf["object_names"] == stl["object_names"]
    assert three_mf["bytes"] != stl["bytes"], "3MF is a zip container, not a raw STL"

    validation = bridge.validate_document(str(document))
    assert validation["valid"] is True, validation


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
