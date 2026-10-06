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
def test_real_freecad_sketch_is_constrained_identically_across_versions(tmp_path: Path):
    """A fully constrained sketch must come out the same on every host.

    The same call sequence runs on FreeCAD 1.0.2 and 1.1.4, so anything here that
    depends on a version-specific spelling fails on one leg and not the other --
    that is what makes this the parity check rather than a smoke test.

    A rectangle is four segments plus the constraints that pin them: coincident
    to close the loop, horizontal/vertical to square it, and two dimensions to
    size it. The counts and the remaining freedom are asserted as exact numbers
    rather than a range, because the point of the test is that both hosts agree
    on them.

    The rectangle is deliberately left with two degrees of freedom: what these
    constraints remove is its shape, and what is left is its position, which the
    constraint vocabulary here has no way to anchor to the origin. Adding more
    dimensions to force zero only produces conflicting and redundant constraints,
    so the profile is asserted as square and sized rather than as immovable.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "sketch-parity.FCStd"
    bridge.create_document(str(document))

    created = bridge.create_sketch(str(document), "Profile", plane="xy")
    assert created["attached_plane"] == "XY_Plane"

    geometry = bridge.add_sketch_geometry(
        str(document),
        "Profile",
        {"kind": "rectangle", "corner": [0, 0], "width": 80, "height": 50},
    )
    # A rectangle has no Sketcher primitive, so it expands to four segments.
    assert geometry["element_ids"] == [0, 1, 2, 3]

    for index in range(4):
        bridge.add_sketch_constraint(
            str(document),
            "Profile",
            "coincident",
            [
                {"element": index, "position": "end"},
                {"element": (index + 1) % 4, "position": "start"},
            ],
        )
    for index in (0, 2):
        bridge.add_sketch_constraint(str(document), "Profile", "horizontal", [{"element": index}])
    for index in (1, 3):
        bridge.add_sketch_constraint(str(document), "Profile", "vertical", [{"element": index}])
    bridge.add_sketch_constraint(str(document), "Profile", "distance_x", [{"element": 0}], value=80)
    bridge.add_sketch_constraint(str(document), "Profile", "distance_y", [{"element": 1}], value=50)

    info = bridge.get_sketch_info(str(document), "Profile")

    assert info["geometry_count"] == 4, "the rectangle must survive as four segments"
    # Four coincident to close the loop, two horizontal, two vertical, two
    # dimensional: ten constraints, leaving the two degrees of freedom that are
    # the rectangle's position.
    assert info["constraint_count"] == 10
    assert info["dof"] == 2, "a squared and sized rectangle is free only to move"
    assert info["fully_constrained"] is False
    assert not info["conflicting_constraints"]
    assert not info["redundant_constraints"]

    # Topological parity: the counts must be identical on 1.0.2 and 1.1.4, so a
    # version difference in how the sketch is built shows up here rather than
    # silently producing a different profile.
    inspected = bridge.inspect_document(str(document))
    sketch_object = next(obj for obj in inspected["objects"] if obj["name"] == "Profile")
    assert sketch_object["shape"]["vertices"] == 4
    assert sketch_object["shape"]["edges"] == 4
    assert sketch_object["shape"]["faces"] == 0, "a sketch is a wire, not a face"


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
