"""Snapshot and restore: recoverability for a process-per-call adapter.

Every modelling call in this adapter is a process-level commit, so a mistaken
``boolean_operation`` is permanent unless something copies the bytes first.
These tests cover the two promises that make snapshots worth trusting:

* a restore returns the document to the exact SHA-256 that was snapshotted;
* a restore never silently destroys work it was not told about -- not a
  concurrent write, not a stale ``expected_sha256``, not a full store.

The documents here are placeholder bytes rather than real ``.FCStd`` archives.
That is deliberate and sufficient: the snapshot path never opens the document,
it only copies and hashes it, so the real-host lane in ``tests/test_bridge.py``
is what proves real documents round-trip while this file proves the refusal
paths fire with byte-exact evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import bridge as bridge_module  # noqa: E402
from dcc_mcp_freecad import snapshots as snapshots_module  # noqa: E402
from dcc_mcp_freecad.bridge import FreecadBridge  # noqa: E402
from dcc_mcp_freecad.snapshots import (  # noqa: E402
    ERROR_CONFLICT,
    ERROR_CONTENT_MISMATCH,
    ERROR_COPY_TIMEOUT,
    ERROR_LIMIT_EXCEEDED,
    ERROR_NOT_FOUND,
    ERROR_STORE_OUTSIDE_ROOTS,
    SnapshotError,
    sha256_file,
)

# A snapshot is a byte copy, so the payload only has to be distinguishable.
DOCUMENT_BYTES = b"PK\x03\x04 fake FCStd payload v1"
MODIFIED_BYTES = b"PK\x03\x04 fake FCStd payload v2 - user kept working"


@pytest.fixture()
def workspace(tmp_path: Path):
    """A throwaway bridge whose allowed root is the only root there is."""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, outside


def make_bridge(root: Path, **kwargs) -> FreecadBridge:
    return FreecadBridge(executable=None, allowed_roots=[root], **kwargs)


def write_document(root: Path, name: str = "part.FCStd", payload: bytes = DOCUMENT_BYTES) -> Path:
    document = root / name
    document.write_bytes(payload)
    return document


def _leftover_names(root: Path) -> set:
    """Top-level names in the user's document root, store included.

    A leaked restore stage is a hidden full-size copy sitting next to the
    user's document, so the assertion is about what is visible in *their*
    directory rather than about the store's internals.
    """
    return {path.name for path in root.iterdir()}


# ---------------------------------------------------------------------------
# Store placement
# ---------------------------------------------------------------------------


def test_default_store_lives_inside_the_first_allowed_root(workspace):
    root, _ = workspace
    bridge = make_bridge(root)

    assert bridge.snapshot_directory == root / ".dcc-mcp-freecad" / "snapshots"
    assert str(root) in str(bridge.snapshot_directory)


def test_explicit_store_must_stay_inside_allowed_roots(workspace):
    root, outside = workspace

    with pytest.raises(SnapshotError) as caught:
        make_bridge(root, snapshot_directory=str(outside / "snapshots"))

    assert caught.value.error_code == ERROR_STORE_OUTSIDE_ROOTS
    assert str(root) in " ".join(caught.value.remediation)


def test_explicit_store_inside_allowed_roots_is_accepted(workspace):
    root, _ = workspace
    target = root / "snapshots"

    bridge = make_bridge(root, snapshot_directory=str(target))

    assert bridge.snapshot_directory == target.resolve()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_snapshot_records_source_identity(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)

    result = bridge.create_snapshot(str(document), label="before boolean cut")

    assert result["snapshot_id"]
    assert result["document_sha256"] == sha256_file(document)
    assert result["bytes"] == len(DOCUMENT_BYTES)
    assert result["label"] == "before boolean cut"
    assert result["origin"] == "manual"
    assert Path(result["snapshot_path"]).is_file()
    assert Path(result["snapshot_path"]).read_bytes() == DOCUMENT_BYTES
    # The snapshot is a copy, never a mutation of the source.
    assert document.read_bytes() == DOCUMENT_BYTES


def test_create_snapshot_stores_bytes_inside_allowed_roots(workspace):
    root, outside = workspace
    bridge = make_bridge(root)
    document = write_document(root)

    result = bridge.create_snapshot(str(document))

    stored = Path(result["snapshot_path"]).resolve()
    assert str(root.resolve()) in str(stored)


def test_create_snapshot_refuses_a_document_outside_allowed_roots(workspace):
    root, outside = workspace
    bridge = make_bridge(root)
    document = outside / "part.FCStd"
    document.write_bytes(DOCUMENT_BYTES)

    with pytest.raises(Exception) as caught:
        bridge.create_snapshot(str(document))

    assert "ALLOWED_ROOTS" in str(caught.value)


def test_two_snapshots_of_the_same_content_get_distinct_ids(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)

    first = bridge.create_snapshot(str(document))
    second = bridge.create_snapshot(str(document))

    assert first["snapshot_id"] != second["snapshot_id"]
    assert first["document_sha256"] == second["document_sha256"]


# ---------------------------------------------------------------------------
# The core promise: snapshot, change, restore
# ---------------------------------------------------------------------------


def test_restore_returns_the_document_to_the_snapshotted_sha(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    original_sha = sha256_file(document)

    snapshot = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)
    assert sha256_file(document) != original_sha

    result = bridge.restore_snapshot(str(document), snapshot["snapshot_id"])

    assert document.read_bytes() == DOCUMENT_BYTES
    assert result["document_sha256"] == original_sha
    assert result["restored_sha256"] == original_sha
    assert result["document_bytes"] == len(DOCUMENT_BYTES)


def test_restore_snapshots_what_it_replaces_so_it_can_be_undone(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    original_sha = sha256_file(document)

    snapshot = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)
    modified_sha = sha256_file(document)

    result = bridge.restore_snapshot(str(document), snapshot["snapshot_id"])

    # The replaced state is preserved and reported, not discarded.
    assert result["replaced_document_sha256"] == modified_sha
    assert result["undo_snapshot_id"]
    assert result["undo_snapshot_id"] != snapshot["snapshot_id"]
    undo = bridge.list_snapshots()["snapshots"]
    undo_entry = next(item for item in undo if item["snapshot_id"] == result["undo_snapshot_id"])
    assert undo_entry["document_sha256"] == modified_sha
    assert undo_entry["origin"] == "pre_restore"
    assert undo_entry["restored_snapshot_id"] == snapshot["snapshot_id"]

    # ...and restoring that id puts the discarded work straight back.
    undone = bridge.restore_snapshot(str(document), result["undo_snapshot_id"])
    assert undone["document_sha256"] == modified_sha
    assert document.read_bytes() == MODIFIED_BYTES
    assert original_sha != modified_sha


def test_restore_can_be_run_twice_in_a_row(workspace):
    """Restore, undo the restore, then restore again: the loop must be closed."""
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    original_sha = sha256_file(document)

    snapshot = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)
    modified_sha = sha256_file(document)

    first = bridge.restore_snapshot(str(document), snapshot["snapshot_id"])
    assert sha256_file(document) == original_sha

    bridge.restore_snapshot(str(document), first["undo_snapshot_id"])
    assert sha256_file(document) == modified_sha

    third = bridge.restore_snapshot(str(document), snapshot["snapshot_id"])
    assert sha256_file(document) == original_sha
    assert third["document_sha256"] == original_sha
    # Every restore left a fresh undo point behind.
    assert len({item["snapshot_id"] for item in bridge.list_snapshots()["snapshots"]}) >= 4


# ---------------------------------------------------------------------------
# Optimistic concurrency
# ---------------------------------------------------------------------------


def test_restore_refuses_a_stale_expected_sha256(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    stale_sha = sha256_file(document)

    snapshot = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)
    current_sha = sha256_file(document)
    before = bridge.list_snapshots()

    with pytest.raises(SnapshotError) as caught:
        bridge.restore_snapshot(str(document), snapshot["snapshot_id"], stale_sha)

    assert caught.value.error_code == ERROR_CONFLICT
    assert caught.value.details["expected_sha256"] == stale_sha
    assert caught.value.details["document_sha256"] == current_sha
    # The new work survives, and nothing was added to the store.
    assert document.read_bytes() == MODIFIED_BYTES
    assert bridge.list_snapshots()["count"] == before["count"]


def test_restore_accepts_the_current_expected_sha256(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)

    result = bridge.restore_snapshot(str(document), snapshot["snapshot_id"], sha256_file(document))

    assert result["document_sha256"] == snapshot["document_sha256"]


def test_restore_is_case_insensitive_about_the_expected_sha256(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))

    result = bridge.restore_snapshot(
        str(document), snapshot["snapshot_id"], sha256_file(document).upper()
    )

    assert result["document_sha256"] == snapshot["document_sha256"]


def test_restore_refuses_when_the_document_changes_mid_copy(
    workspace, monkeypatch: pytest.MonkeyPatch
):
    """A writer that lands during the pre-restore copy must lose, not be lost."""
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)
    modified_sha = sha256_file(document)
    before = bridge.list_snapshots()

    real_copy = snapshots_module.copy_and_hash

    def racing_copy(source, destination, deadline=None):
        result = real_copy(source, destination, deadline)
        # Someone commits new work while the undo copy is in flight.
        document.write_bytes(b"PK\x03\x04 concurrent writer won the race")
        return result

    monkeypatch.setattr(snapshots_module, "copy_and_hash", racing_copy)

    with pytest.raises(SnapshotError) as caught:
        bridge.restore_snapshot(str(document), snapshot["snapshot_id"], modified_sha)

    assert caught.value.error_code == ERROR_CONFLICT
    assert caught.value.details["conflict"] == "concurrent_write"
    # The racing writer's bytes are still the document's bytes.
    assert document.read_bytes() == b"PK\x03\x04 concurrent writer won the race"
    # The untrustworthy pre-restore copy was discarded, not left in the store.
    assert bridge.list_snapshots()["count"] == before["count"]


def test_restore_refuses_when_the_snapshot_bytes_were_tampered_with(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)
    modified_sha = sha256_file(document)

    Path(snapshot["snapshot_path"]).write_bytes(b"PK\x03\x04 corrupted in the store")

    with pytest.raises(SnapshotError) as caught:
        bridge.restore_snapshot(str(document), snapshot["snapshot_id"], modified_sha)

    assert caught.value.error_code == ERROR_CONTENT_MISMATCH
    assert caught.value.details["expected_sha256"] == snapshot["document_sha256"]
    assert document.read_bytes() == MODIFIED_BYTES


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_snapshot_count_limit_is_refused_without_evicting(workspace):
    root, _ = workspace
    bridge = make_bridge(root, max_snapshots=1)
    document = write_document(root)

    kept = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)

    with pytest.raises(SnapshotError) as caught:
        bridge.create_snapshot(str(document))

    assert caught.value.error_code == ERROR_LIMIT_EXCEEDED
    assert caught.value.details["snapshot_count"] == 1
    assert caught.value.details["max_snapshots"] == 1
    guidance = " ".join(caught.value.remediation)
    assert "delete_snapshot" in guidance
    # Nothing was silently evicted: the oldest snapshot is still restorable.
    remaining = bridge.list_snapshots()["snapshots"]
    assert [item["snapshot_id"] for item in remaining] == [kept["snapshot_id"]]


def test_snapshot_byte_limit_is_refused(workspace):
    root, _ = workspace
    bridge = make_bridge(root, max_snapshot_bytes=len(DOCUMENT_BYTES))
    document = write_document(root)

    bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)

    with pytest.raises(SnapshotError) as caught:
        bridge.create_snapshot(str(document))

    assert caught.value.error_code == ERROR_LIMIT_EXCEEDED
    assert caught.value.details["total_bytes"] == len(DOCUMENT_BYTES)
    assert caught.value.details["max_snapshot_bytes"] == len(DOCUMENT_BYTES)


def test_restore_refuses_when_there_is_no_capacity_for_the_undo_snapshot(workspace):
    """A restore the caller could not undo is worse than no restore."""
    root, _ = workspace
    bridge = make_bridge(root, max_snapshots=1)
    document = write_document(root)
    original = bridge.create_snapshot(str(document))
    document.write_bytes(MODIFIED_BYTES)

    with pytest.raises(SnapshotError) as caught:
        bridge.restore_snapshot(str(document), original["snapshot_id"])

    assert caught.value.error_code == ERROR_LIMIT_EXCEEDED
    assert document.read_bytes() == MODIFIED_BYTES
    # The restore stage lives in the user's document directory and holds a full
    # copy of the document, so a refusal must not leave it behind -- and this
    # refusal is on the path the design creates itself (restore always needs
    # room for one undo snapshot), so repeated refusals would leak repeatedly.
    assert _leftover_names(root) == {"part.FCStd", ".dcc-mcp-freecad"}


def test_every_refusal_path_leaves_no_stage_in_the_document_directory(
    workspace, monkeypatch: pytest.MonkeyPatch
):
    """One guard must cover all of them, not three hand-placed unlinks."""
    root, _ = workspace
    document = write_document(root)

    def refusal(exc: Exception):
        def raise_now(*_args, **_kwargs):
            raise exc

        return raise_now

    cases = {
        "capacity": (
            lambda bridge: monkeypatch.setattr(
                snapshots_module.SnapshotStore,
                "ensure_capacity",
                refusal(SnapshotError(ERROR_LIMIT_EXCEEDED, "full")),
            ),
            {},
        ),
        "cancelled": (
            lambda bridge: monkeypatch.setattr(
                bridge_module, "check_dcc_cancelled", refusal(RuntimeError("cancelled"))
            ),
            {},
        ),
        "undo_snapshot_failed": (
            lambda bridge: monkeypatch.setattr(
                snapshots_module.SnapshotStore,
                "write",
                refusal(OSError("disk full")),
            ),
            {},
        ),
        "tampered_snapshot": (lambda bridge: None, {"tamper": True}),
        "stale_expected_sha256": (lambda bridge: None, {"stale": True}),
    }

    for name, (arm, options) in cases.items():
        bridge = make_bridge(root)
        snapshot = bridge.create_snapshot(str(document))
        document.write_bytes(MODIFIED_BYTES)
        if options.get("tamper"):
            Path(snapshot["snapshot_path"]).write_bytes(b"corrupted in the store")
        arm(bridge)
        expected = "0" * 64 if options.get("stale") else None

        with pytest.raises((SnapshotError, RuntimeError, OSError)):
            bridge.restore_snapshot(str(document), snapshot["snapshot_id"], expected)

        assert document.read_bytes() == MODIFIED_BYTES, name
        assert _leftover_names(root) == {"part.FCStd", ".dcc-mcp-freecad"}, name
        monkeypatch.undo()
        # Reset for the next arm: the snapshot store persists across cases.
        for entry in bridge.list_snapshots()["snapshots"]:
            bridge.delete_snapshot(entry["snapshot_id"])


def test_restore_recreates_a_deleted_document(workspace):
    """Recovering a deleted document is the case snapshots exist for."""
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    original_sha = sha256_file(document)
    snapshot = bridge.create_snapshot(str(document))
    document.unlink()

    result = bridge.restore_snapshot(str(document), snapshot["snapshot_id"])

    assert result["created_document"] is True
    # There was no state to preserve, so this restore has no undo - reported,
    # not silently implied by a null id.
    assert result["undo_snapshot_id"] is None
    assert result["replaced_document_sha256"] is None
    assert document.read_bytes() == DOCUMENT_BYTES
    assert result["document_sha256"] == original_sha
    assert "no_state_to_preserve" in result["verified"]


def test_restoring_a_deleted_document_needs_no_store_capacity(workspace):
    """No undo snapshot is taken, so a full store must not block recovery."""
    root, _ = workspace
    bridge = make_bridge(root, max_snapshots=1)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))
    document.unlink()

    bridge.restore_snapshot(str(document), snapshot["snapshot_id"])

    assert document.read_bytes() == DOCUMENT_BYTES
    assert bridge.list_snapshots()["count"] == 1


def test_restoring_a_deleted_document_refuses_a_stale_expected_sha256(workspace):
    """A caller holding an expected hash believes bytes are there."""
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))
    document.unlink()

    with pytest.raises(SnapshotError) as caught:
        bridge.restore_snapshot(str(document), snapshot["snapshot_id"], "0" * 64)

    assert caught.value.error_code == ERROR_CONFLICT
    assert caught.value.details["conflict"] == "document_missing"
    assert not document.exists()
    assert _leftover_names(root) == {"part.FCStd", ".dcc-mcp-freecad"} or {
        ".dcc-mcp-freecad"
    } == _leftover_names(root)


def test_restore_refuses_a_missing_parent_directory(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    bridge.create_snapshot(str(document))

    with pytest.raises(Exception) as caught:
        bridge.restore_snapshot(
            str(root / "nope" / "part.FCStd"),
            bridge.list_snapshots()["snapshots"][0]["snapshot_id"],
        )

    assert "does not exist" in str(caught.value)


def test_listing_reports_bytes_on_the_same_basis_as_the_limit(workspace):
    """total_bytes must include orphans, or a refusal looks like a lie."""
    root, _ = workspace
    bridge = make_bridge(root, max_snapshot_bytes=len(DOCUMENT_BYTES) + 8)
    document = write_document(root)
    bridge.create_snapshot(str(document))
    (bridge.snapshot_directory / "junk.bin").write_bytes(b"0123456789abcdef")

    listing = bridge.list_snapshots()

    assert listing["snapshot_bytes"] == len(DOCUMENT_BYTES)
    # The byte limit is charged against orphans too, so the figure the caller
    # compares to max_snapshot_bytes has to count them.
    assert listing["total_bytes"] == len(DOCUMENT_BYTES) + 16
    assert listing["total_bytes"] > listing["max_snapshot_bytes"]


def test_delete_snapshot_frees_capacity(workspace):
    root, _ = workspace
    bridge = make_bridge(root, max_snapshots=1)
    document = write_document(root)

    first = bridge.create_snapshot(str(document))
    with pytest.raises(SnapshotError):
        document.write_bytes(MODIFIED_BYTES)
        bridge.create_snapshot(str(document))

    deleted = bridge.delete_snapshot(first["snapshot_id"])
    assert deleted["deleted"] is True
    assert deleted["snapshot_id"] == first["snapshot_id"]

    # The freed slot is usable again.
    second = bridge.create_snapshot(str(document))
    assert second["snapshot_id"] != first["snapshot_id"]
    assert not Path(first["snapshot_path"]).exists()


def test_delete_refuses_an_unknown_id(workspace):
    root, _ = workspace
    bridge = make_bridge(root)

    with pytest.raises(SnapshotError) as caught:
        bridge.delete_snapshot("20261007T021530.123456Z-000000000000")

    assert caught.value.error_code == ERROR_NOT_FOUND


def test_delete_removes_both_halves_of_the_entry(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))
    data = Path(snapshot["snapshot_path"])
    sidecar = data.with_suffix(".json")

    assert sidecar.is_file()
    bridge.delete_snapshot(snapshot["snapshot_id"])
    assert not data.exists()
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# Listing and identity
# ---------------------------------------------------------------------------


def test_list_snapshots_filters_by_source_document(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    first = write_document(root, "a.FCStd")
    second = write_document(root, "b.FCStd", MODIFIED_BYTES)

    bridge.create_snapshot(str(first), label="first")
    bridge.create_snapshot(str(second), label="second")

    everything = bridge.list_snapshots()
    assert everything["count"] == 2

    only_first = bridge.list_snapshots(str(first))
    assert only_first["count"] == 1
    assert only_first["snapshots"][0]["label"] == "first"
    assert only_first["snapshots"][0]["document_path"] == str(first)


def test_list_snapshots_reports_limits_and_usage(workspace):
    root, _ = workspace
    bridge = make_bridge(root, max_snapshots=7, max_snapshot_bytes=4096)
    document = write_document(root)
    bridge.create_snapshot(str(document))

    listing = bridge.list_snapshots()

    assert listing["max_snapshots"] == 7
    assert listing["max_snapshot_bytes"] == 4096
    assert listing["count"] == 1
    assert listing["total_bytes"] == len(DOCUMENT_BYTES)
    assert listing["orphans"] == {"count": 0, "bytes": 0, "files": []}


def test_list_snapshots_works_for_a_document_that_no_longer_exists(workspace):
    """The moment a user most needs this list is after something was deleted."""
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    bridge.create_snapshot(str(document))
    document.unlink()

    listing = bridge.list_snapshots(str(document))

    assert listing["count"] == 1


def test_list_snapshots_refuses_a_filter_outside_allowed_roots(workspace):
    root, outside = workspace
    bridge = make_bridge(root)

    with pytest.raises(Exception) as caught:
        bridge.list_snapshots(str(outside / "part.FCStd"))

    assert "ALLOWED_ROOTS" in str(caught.value)


def test_snapshot_ids_reject_traversal(workspace):
    root, _ = workspace
    bridge = make_bridge(root)

    for hostile in ("..", "../../etc/passwd", "a/b", "20261007T021530.123456Z-xyz"):
        with pytest.raises(SnapshotError) as caught:
            bridge.delete_snapshot(hostile)
        assert caught.value.error_code == ERROR_NOT_FOUND


def test_restoring_across_documents_is_reported_not_silent(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    source = write_document(root, "a.FCStd")
    target = write_document(root, "b.FCStd", MODIFIED_BYTES)
    snapshot = bridge.create_snapshot(str(source))

    result = bridge.restore_snapshot(str(target), snapshot["snapshot_id"])

    assert result["cross_document"] is True
    assert result["snapshot_document_path"] == str(source)
    assert target.read_bytes() == DOCUMENT_BYTES


def test_restoring_onto_its_own_source_document_is_not_cross_document(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    snapshot = bridge.create_snapshot(str(document))

    result = bridge.restore_snapshot(str(document), snapshot["snapshot_id"])

    assert result["cross_document"] is False


def test_partial_files_are_reported_as_orphans_not_snapshots(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    bridge.create_snapshot(str(document))

    store = bridge.snapshot_directory
    (store / ".part-leftover.part").write_bytes(b"interrupted copy")

    listing = bridge.list_snapshots()

    assert listing["count"] == 1
    assert listing["orphans"]["count"] == 1
    assert listing["orphans"]["files"] == [".part-leftover.part"]


def test_orphan_bytes_count_against_the_limit_but_not_the_count(workspace):
    root, _ = workspace
    bridge = make_bridge(root, max_snapshots=5, max_snapshot_bytes=len(DOCUMENT_BYTES) + 8)
    document = write_document(root)
    bridge.create_snapshot(str(document))
    (bridge.snapshot_directory / "junk.bin").write_bytes(b"0123456789abcdef")

    listing = bridge.list_snapshots()
    assert listing["count"] == 1
    assert listing["orphans"]["bytes"] == 16

    with pytest.raises(SnapshotError) as caught:
        document.write_bytes(MODIFIED_BYTES)
        bridge.create_snapshot(str(document))
    assert caught.value.error_code == ERROR_LIMIT_EXCEEDED


def test_status_reports_the_snapshot_store(workspace):
    root, _ = workspace
    bridge = make_bridge(root)
    document = write_document(root)
    bridge.create_snapshot(str(document))

    summary = bridge.status()["snapshot_store"]

    assert summary["directory"] == str(bridge.snapshot_directory)
    assert summary["count"] == 1
    assert summary["total_bytes"] == len(DOCUMENT_BYTES)
    assert summary["max_snapshots"] == bridge.max_snapshots
    assert "delete_snapshot" in bridge.capabilities()["methods"]


def test_copy_deadline_is_enforced(workspace):
    root, _ = workspace
    document = write_document(root)
    destination = root / "copy.bin"

    with pytest.raises(SnapshotError) as caught:
        snapshots_module.copy_and_hash(document, destination, deadline=-1)

    assert caught.value.error_code == ERROR_COPY_TIMEOUT
    assert not destination.exists()
