"""Document snapshots: this adapter's answer to "there is no undo stack".

Every modelling call here is a *process-level commit*: a ``FreeCADCmd`` process
starts, mutates a document, saves it, and exits. There is no in-process undo
stack to rewind, so a single mistaken ``boolean_operation`` or
``remove_object`` is permanent -- the user's only recovery is their own version
control.

This module gives that loop an explicit recovery path instead:

    create_snapshot  -> copy the document's bytes under a store we own
    list_snapshots   -> show what can be restored, and what the limits are
    restore_snapshot -> put those bytes back, after snapshotting what they
                        replace (so the restore is itself undoable)
    delete_snapshot  -> free capacity, because a hard limit with no way out
                        is a dead end

Why this never spawns FreeCAD
-----------------------------

An ``.FCStd`` is a self-contained archive, so a snapshot is a byte copy and a
restore is a byte replacement. No host call is needed, which buys three things:

* snapshots keep working when FreeCAD is missing, broken, or mid-upgrade --
  exactly when recovery matters most;
* creating one is I/O-bound rather than host-bound, so it is fast enough to
  take before every risky call;
* the integrity check is a SHA-256 over the bytes, which is strictly stronger
  than the object-level read-back a host round-trip could give us.

Why these tools are not in ``write_contract.MUTATING_TOOLS``
------------------------------------------------------------

That list classifies the *native driver's* method table, and a driver method
means "a FreeCAD process did this". Nothing here reaches the driver. The
read-back obligation still applies and is still enforced -- see
:func:`SnapshotStore.write` and the post-restore verification in
``bridge.restore_snapshot`` -- but it is satisfied at the byte level rather
than through ``_METHODS``.

Error taxonomy
--------------

:class:`SnapshotError` deliberately does not subclass
:class:`dcc_mcp_freecad.bridge.BridgeError`. "The FreeCAD bridge failed" and
"the snapshot store refused this call" are different failures with different
remedies, and a caller that catches one should not silently swallow the other.
Both remain plain ``RuntimeError`` subclasses for callers that do not care.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .write_contract import WriteVerificationError

SCHEMA_VERSION = 1

SNAPSHOT_SUFFIX = ".FCStd"
SIDECAR_SUFFIX = ".json"

# Default store location, relative to the first allowed root.
STORE_PARENT = ".dcc-mcp-freecad"
STORE_NAME = "snapshots"

DEFAULT_MAX_SNAPSHOTS = 50
DEFAULT_MAX_SNAPSHOT_BYTES = 1024**3

COPY_CHUNK_BYTES = 1024 * 1024

# Generated ids embed a UTC timestamp and 12 hex chars of the content hash, so
# they sort into creation order and stay guess-proof. The exact format is also
# the admission test: anything that does not match cannot name a file, which is
# why traversal attempts are rejected before a path is ever built.
SNAPSHOT_ID = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}(?:-[0-9]{1,3})?$")

ERROR_LIMIT_EXCEEDED = "snapshot_limit_exceeded"
ERROR_CONFLICT = "snapshot_conflict"
ERROR_NOT_FOUND = "snapshot_not_found"
ERROR_CONTENT_MISMATCH = "snapshot_content_mismatch"
ERROR_COPY_TIMEOUT = "snapshot_copy_timeout"
ERROR_STORE_OUTSIDE_ROOTS = "snapshot_store_outside_allowed_roots"

MANUAL_ORIGIN = "manual"
PRE_RESTORE_ORIGIN = "pre_restore"

CLEANUP_GUIDANCE = (
    "Call list_snapshots to review the stored snapshots, then call "
    "delete_snapshot with an id you no longer need."
)


class SnapshotError(RuntimeError):
    """A bounded snapshot-store refusal that carries a stable error code.

    ``error_code`` is machine-readable so a caller can branch on *why* a
    restore was refused instead of parsing prose, and ``remediation`` is an
    ordered list of things the caller can actually do next -- a hard limit the
    caller cannot act on is just a wall.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        remediation: Any = (),
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.remediation = [str(item) for item in remediation]
        self.details = {key: value for key, value in details.items() if value is not None}


def sha256_file(path: Any) -> str:
    """Hex SHA-256 of a file, streamed so a 2 GiB document never lands in RAM."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_equal(left: Any, right: Any) -> bool:
    """Compare two digests as text, tolerating case and surrounding space."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.strip().lower() == right.strip().lower()


def copy_and_hash(
    source: Any, destination: Any, deadline: Optional[float] = None
) -> Tuple[str, int]:
    """Copy ``source`` to a new ``destination`` and hash the bytes copied.

    Hashing the stream as it is written is what makes the returned digest a
    statement about *this* copy rather than about the source at some earlier
    instant -- and it is the only reason a snapshot can claim a SHA-256 without
    re-reading the file it just wrote.

    ``destination`` is created exclusively (``xb``): a snapshot store entry and
    a restore stage are both files this call owns outright, and silently
    replacing either could destroy the very bytes the caller is trying to
    protect.

    ``deadline`` is a :func:`time.monotonic` value checked per chunk. The copy
    is the only unbounded step in a snapshot call, and a stalled network volume
    must not pin a process forever just because the work is "only" a copy.
    """
    digest = hashlib.sha256()
    written = 0
    try:
        with Path(source).open("rb") as reader:
            with open(str(destination), "xb") as writer:
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise SnapshotError(
                            ERROR_COPY_TIMEOUT,
                            "Copying %s exceeded the call deadline" % source,
                            remediation=[
                                "Retry with a larger timeout_secs.",
                                "Snapshot a smaller document, or free space on the "
                                "destination volume.",
                            ],
                        )
                    chunk = reader.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    writer.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    except BaseException:
        # This call created the destination, so this call removes it. A partial
        # file left behind would become an orphan the caller has to reason
        # about, and a store entry that never existed must not be listable.
        _unlink(destination)
        raise
    return digest.hexdigest(), written


def _isoformat(moment: float) -> str:
    micros = min(999_999, int(round((moment - int(moment)) * 1_000_000)))
    return "%s.%06dZ" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(moment)), micros)


def new_snapshot_id(moment: float, content_sha256: str) -> str:
    """Build a sortable, content-tagged id: ``20261007T021530.123456Z-a1b2c3d4e5f6``."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(moment))
    micros = min(999_999, int(round((moment - int(moment)) * 1_000_000)))
    return "%s.%06dZ-%s" % (stamp, micros, str(content_sha256).lower()[:12])


def _unlink(path: Any) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _safe_size(path: Any) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


class SnapshotStore:
    """The on-disk snapshot directory: metadata sidecars beside data files.

    Layout is deliberately *one directory of pairs*, not an index file::

        <store>/20261007T021530.123456Z-a1b2c3d4e5f6.FCStd
        <store>/20261007T021530.123456Z-a1b2c3d4e5f6.json

    The filesystem is therefore the only registry. An index would be one more
    thing that can disagree with the bytes -- and the failure this whole module
    exists to prevent is a report that disagrees with reality. A sidecar that
    lost its data file is simply not a snapshot.

    Both halves must exist for an entry to be listed, so a crash mid-write
    leaves a visible orphan rather than a snapshot that restores nothing.
    """

    def __init__(
        self,
        directory: Any,
        max_snapshots: int = DEFAULT_MAX_SNAPSHOTS,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self.max_snapshots = max(1, int(max_snapshots))
        self.max_snapshot_bytes = max(1, int(max_snapshot_bytes))

    # -- layout ---------------------------------------------------------

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def _pair(self, snapshot_id: str) -> Tuple[Path, Path]:
        """Resolve an id to its data/sidecar pair, refusing anything else.

        The id regex already excludes separators and ``..``; the containment
        check below is defence in depth, not the primary gate.
        """
        if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.match(snapshot_id):
            raise SnapshotError(
                ERROR_NOT_FOUND,
                "Unknown snapshot id: %r" % (snapshot_id,),
                remediation=[
                    "Call list_snapshots to get ids this store actually holds.",
                    "Snapshot ids are generated by create_snapshot; they are not paths.",
                ],
            )
        root = self.directory.resolve()
        data = (self.directory / (snapshot_id + SNAPSHOT_SUFFIX)).resolve()
        sidecar = (self.directory / (snapshot_id + SIDECAR_SUFFIX)).resolve()
        if data.parent != root or sidecar.parent != root:
            raise SnapshotError(
                ERROR_NOT_FOUND,
                "Snapshot id resolves outside the snapshot store: %r" % (snapshot_id,),
                remediation=["Call list_snapshots to get ids this store actually holds."],
            )
        return data, sidecar

    def _unique_target(self, snapshot_id: str) -> Path:
        candidate = self.directory / (snapshot_id + SNAPSHOT_SUFFIX)
        index = 1
        while candidate.exists():
            index += 1
            candidate = self.directory / ("%s-%d%s" % (snapshot_id, index, SNAPSHOT_SUFFIX))
        return candidate

    # -- reading --------------------------------------------------------

    def read(self, snapshot_id: str) -> Dict[str, Any]:
        """Return one snapshot's metadata, proven against the bytes on disk."""
        data, sidecar = self._pair(snapshot_id)
        if not data.is_file() or not sidecar.is_file():
            raise SnapshotError(
                ERROR_NOT_FOUND,
                "Snapshot %s is not present in %s" % (snapshot_id, self.directory),
                remediation=["Call list_snapshots to get ids this store actually holds."],
                snapshot_id=snapshot_id,
            )
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SnapshotError(
                ERROR_CONTENT_MISMATCH,
                "Snapshot %s has an unreadable sidecar (%s)" % (snapshot_id, error),
                remediation=[
                    "Restore a different snapshot; this entry's metadata cannot be trusted.",
                    "Remove the damaged pair from %s manually." % self.directory,
                ],
                snapshot_id=snapshot_id,
            ) from None
        if not isinstance(metadata, dict):
            raise SnapshotError(
                ERROR_CONTENT_MISMATCH,
                "Snapshot %s has a malformed sidecar" % snapshot_id,
                remediation=["Restore a different snapshot."],
                snapshot_id=snapshot_id,
            )
        entry = dict(metadata)
        entry["snapshot_path"] = str(data)
        # The filesystem is the registry; a sidecar's own byte count is a claim.
        entry["bytes"] = data.stat().st_size
        return entry

    def scan(self) -> Tuple[List[Dict[str, Any]], List[Path]]:
        """Return ``(snapshots, orphans)`` as the store currently stands."""
        snapshots: List[Dict[str, Any]] = []
        if not self.directory.is_dir():
            return [], []
        paired = set()
        for sidecar in sorted(self.directory.glob("*" + SIDECAR_SUFFIX)):
            snapshot_id = sidecar.name[: -len(SIDECAR_SUFFIX)]
            if not SNAPSHOT_ID.match(snapshot_id):
                continue
            try:
                entry = self.read(snapshot_id)
            except SnapshotError:
                continue
            paired.add(Path(entry["snapshot_path"]).name)
            paired.add(sidecar.name)
            snapshots.append(entry)
        snapshots.sort(
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("snapshot_id") or ""))
        )
        orphans = [
            candidate
            for candidate in sorted(self.directory.iterdir())
            if candidate.is_file() and candidate.name not in paired
        ]
        return snapshots, orphans

    def usage(self, snapshots: Any, orphans: Any = ()) -> Tuple[int, int]:
        """``(count, bytes)`` currently charged against the store's limits."""
        total = sum(int(item.get("bytes") or 0) for item in snapshots)
        total += sum(_safe_size(path) for path in orphans)
        return len(list(snapshots)), total

    # -- capacity -------------------------------------------------------

    def ensure_capacity(
        self,
        snapshots: Any,
        orphans: Any,
        additional_bytes: int,
        additional_count: int = 1,
    ) -> None:
        """Refuse to grow past the configured limits instead of evicting.

        Auto-evicting the oldest snapshot would be the convenient choice and
        the wrong one: the oldest snapshot is often the only known-good state,
        and dropping it to make room is the same class of silent data loss this
        module exists to prevent. A full store is a decision for the caller.
        """
        count, used = self.usage(snapshots, orphans)
        guidance = [CLEANUP_GUIDANCE]
        if orphans:
            guidance.append(
                "Remove %d orphaned file(s) from %s manually; delete_snapshot only "
                "removes entries with complete metadata." % (len(list(orphans)), self.directory)
            )
        if count + additional_count > self.max_snapshots:
            raise SnapshotError(
                ERROR_LIMIT_EXCEEDED,
                "Snapshot store is full: %d of %d snapshots are stored in %s and this "
                "call needs %d more. Nothing was evicted."
                % (count, self.max_snapshots, self.directory, additional_count),
                remediation=guidance,
                snapshot_count=count,
                max_snapshots=self.max_snapshots,
                requested=additional_count,
                snapshot_store=str(self.directory),
            )
        if used + additional_bytes > self.max_snapshot_bytes:
            raise SnapshotError(
                ERROR_LIMIT_EXCEEDED,
                "Snapshot store is full: %d of %d bytes are stored in %s and this call "
                "needs %d more. Nothing was evicted."
                % (used, self.max_snapshot_bytes, self.directory, additional_bytes),
                remediation=guidance,
                total_bytes=used,
                max_snapshot_bytes=self.max_snapshot_bytes,
                requested_bytes=additional_bytes,
                snapshot_store=str(self.directory),
            )

    # -- writing --------------------------------------------------------

    def write(
        self,
        document: Any,
        deadline: Optional[float] = None,
        label: Optional[str] = None,
        origin: str = MANUAL_ORIGIN,
        restored_snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Copy ``document`` into the store and return its metadata.

        The copy is staged under a ``.part`` name and renamed into place only
        after its sidecar is readable, so an interrupted snapshot never appears
        as a snapshot that restores nothing.
        """
        self.ensure()
        moment = time.time()
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".part-", suffix=".part", dir=str(self.directory)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        # mkstemp only reserved the name; the copy publishes it exclusively so
        # a name that collided after reservation cannot be overwritten.
        staged.unlink()
        try:
            content_sha, size = copy_and_hash(document, staged, deadline)
            snapshot_id = new_snapshot_id(moment, content_sha)
            target = self._unique_target(snapshot_id)
            sidecar = target.with_name(target.name[: -len(SNAPSHOT_SUFFIX)] + SIDECAR_SUFFIX)
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": target.name[: -len(SNAPSHOT_SUFFIX)],
                "document_path": str(document),
                "document_sha256": content_sha,
                "bytes": size,
                "created_at": _isoformat(moment),
                "label": label,
                "origin": origin,
                "restored_snapshot_id": restored_snapshot_id,
            }
            sidecar.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            # Sidecar first, data second. An entry is only listed once both
            # halves exist, so publishing the bytes last means a partial
            # snapshot is invisible rather than restorable-but-wrong.
            os.replace(str(staged), str(target))
            entry = dict(metadata)
            entry["snapshot_path"] = str(target)
            entry["bytes"] = target.stat().st_size
            return entry
        except BaseException:
            _unlink(staged)
            raise

    def discard(self, snapshot_id: str) -> None:
        """Best-effort removal of a pair this call just created and rejected.

        Used on abort paths where the bytes are known to be untrustworthy -- a
        copy that was overtaken by a concurrent writer, for instance. Keeping
        it would put a snapshot in the store whose id advertises a hash it does
        not hold.
        """
        try:
            data, sidecar = self._pair(snapshot_id)
        except SnapshotError:
            return
        _unlink(sidecar)
        _unlink(data)

    def delete(self, snapshot_id: str) -> Dict[str, Any]:
        entry = self.read(snapshot_id)
        data, sidecar = self._pair(snapshot_id)
        # Sidecar first: stop advertising the entry before its bytes go away.
        _unlink(sidecar)
        _unlink(data)
        return entry


def verification_failure(tool: str, check: str, expected: Any, actual: Any, remediation: str):
    """Build the repo-standard read-back error for a snapshot read-back."""
    return WriteVerificationError(
        tool=tool,
        check=check,
        expected=expected,
        actual=actual,
        remediation=remediation,
    )
