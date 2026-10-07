from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

from . import parts_library, raster, sketch_rules
from .capabilities import DEFAULT_MAX_SCRIPT_TIMEOUT_SECS, build_capabilities
from .presentation import (
    DEFAULT_RENDER_HEIGHT,
    DEFAULT_RENDER_WIDTH,
    MAX_RENDER_HEIGHT,
    MAX_RENDER_WIDTH,
    VIEWS,
    validate_options,
    validate_render_size,
)
from .snapshots import (
    DEFAULT_MAX_SNAPSHOT_BYTES,
    DEFAULT_MAX_SNAPSHOTS,
    ERROR_CONFLICT,
    ERROR_CONTENT_MISMATCH,
    ERROR_STORE_OUTSIDE_ROOTS,
    PRE_RESTORE_ORIGIN,
    STORE_NAME,
    STORE_PARENT,
    SnapshotError,
    SnapshotStore,
    copy_and_hash,
    sha256_equal,
    sha256_file,
    verification_failure,
)

_DOCUMENT_SUFFIX = ".fcstd"
_RENDER_SUFFIX = ".png"
_IMPORT_SUFFIXES = {".brep", ".brp", ".iges", ".igs", ".obj", ".step", ".stl", ".stp"}
_EXPORT_SUFFIXES = set(_IMPORT_SUFFIXES)
_OBJECT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: Largest PNG the adapter will inline as base64. Rendering is bounded to
#: 1280x720, so this only triggers on pathological content; it exists so an
#: opt-in image can never grow without limit into an agent's context.
MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024

_RENDER_REMEDIATION = {
    "degenerate_render": (
        "The captured frame is a flat fill, so the host's offscreen rendering pipeline "
        "produced no content. On Linux this usually means no software GL is reachable: set "
        "LIBGL_ALWAYS_SOFTWARE=1 (Mesa llvmpipe) or install libgl1-mesa-dri/mesa-utils. On "
        "Windows and macOS, confirm the FreeCAD build ships its GUI libraries. Confirm the "
        "host once with `dcc-mcp-freecad doctor --json` and check the render_view block in "
        "get_capabilities."
    ),
    "empty_render": (
        "The captured frame does not differ from the same camera with every object hidden, so "
        "the selected objects contributed no pixels. Check that visible_objects names objects "
        "that exist and carry geometry, that the chosen view is not framed away from them, and "
        "that frame_margin is not so large the model falls below the detection threshold."
    ),
}

# ``run_script`` is the one entry point this adapter gives a caller for work
# none of its typed tools cover, so it gets its own timeout ceiling instead of
# inheriting the 1800s document default. A modal dialog opened by a script
# (upstream #148) is the failure mode being bounded: the worst case degrades to
# "the call hung until the deadline killed it", and half an hour of that is not
# a recoverable wait for an agent or an operator. Setting the env var is the
# documented way to widen it for a long offline batch.
#
# The default is owned by ``capabilities`` (see the note there) and re-exported
# so the number an operator configures and the number the capability block
# advertises have exactly one definition.
MAX_SCRIPT_TIMEOUT_SECS = 1_800
SCRIPT_SUFFIXES = {".py"}
# The script runner is a packaged module next to freecad_driver.py, so it is
# covered by the same "packaged driver is missing" check and ships in the wheel.
SCRIPT_RUNNER_NAME = "script_runner.py"


class BridgeError(RuntimeError):
    """A bounded FreeCAD bridge failure safe to return to a local caller.

    ``code`` carries the driver's stable refusal code when it gave one, so a
    caller can branch on why a geometry request was refused instead of matching
    on prose. It stays ``None`` for failures that have no code, which is every
    failure that predates the typed geometry tools.
    """

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class BridgeTimeoutError(BridgeError):
    """FreeCADCmd exceeded the configured deadline.

    ``partial`` carries whatever the child had already written before it was
    killed. A timeout is the one failure where the output *before* the failure is
    the most valuable thing the call produced: a script blocked on a modal dialog
    has usually printed where it got to, and that trace is the only clue to what
    blocked it. Without it the caller learns only "it hung", and the answer to
    "where" is gone with the terminated process.

    ``timed_out`` is always ``True`` here, and is carried explicitly rather than
    inferred from the exception type so the skill layer can hand it to the caller
    without importing this module's exception hierarchy.
    """

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        partial: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message, code)
        self.partial = dict(partial or {})
        self.timed_out = True


class RenderVerificationError(BridgeError):
    """A render produced a frame this adapter refuses to hand back.

    The whole reason ``render_view`` measures its output. A host that captures a
    flat frame, or a frame identical to an empty scene, has not rendered the
    caller's model, and returning that image would be worse than failing: the
    agent would reason about a picture that contains nothing. The payload carries
    the ``error_code`` (``degenerate_render`` or ``empty_render``), both pixel
    summaries, the measured geometry fraction, and the remediation, so the caller
    can act on numbers instead of a sentence.
    """

    def __init__(self, payload: Mapping[str, Any], message: str):
        super().__init__(message)
        self.payload = dict(payload)

    @property
    def error_code(self) -> Any:
        return self.payload.get("error_code")

    @property
    def statistics(self) -> Any:
        return self.payload.get("statistics")

    @property
    def geometry_pixel_fraction(self) -> Any:
        return self.payload.get("geometry_pixel_fraction")


class WriteVerificationError(BridgeError):
    """A mutating tool's post-write read-back disagreed with the request.

    Raised instead of a plain :class:`BridgeError` so a caller can branch on the
    structured mismatch instead of parsing the message. The keys mirror
    ``write_contract.WriteVerificationError``: ``tool``, ``check``,
    ``expected``, ``actual``, ``host_version``, ``host_matrix``, ``params``.
    """

    def __init__(self, payload: Mapping[str, Any], message: str):
        super().__init__(message)
        self.payload = dict(payload)

    @property
    def tool(self) -> Any:
        return self.payload.get("tool")

    @property
    def check(self) -> Any:
        return self.payload.get("check")

    @property
    def expected(self) -> Any:
        return self.payload.get("expected")

    @property
    def actual(self) -> Any:
        return self.payload.get("actual")

    @property
    def host_version(self) -> Any:
        return self.payload.get("host_version")


class SketchStateError(BridgeError):
    """A sketch is in a state that must stop the caller.

    Carries the machine-readable ``error_code`` from ``sketch_rules`` (for
    example ``sketch_underconstrained``) and the full state payload, so a caller
    can branch on the code instead of matching prose that differs between hosts.
    """

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        state: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message, code)
        # Set explicitly as well: the base class only gained the ``code``
        # parameter with the typed geometry tools, and a refusal that loses
        # its code is a refusal the caller cannot branch on.
        self.code = code
        self.state = dict(state or {})


def _within(path: Path, roots: Sequence[Path]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        root_value = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate, root_value)) == root_value:
                return True
        except ValueError:
            continue
    return False


def _split_roots(value: str) -> list[Path]:
    return [
        Path(item.strip()).expanduser().resolve()
        for item in value.split(os.pathsep)
        if item.strip()
    ]


def _replace_staged_path(value: Any, staged: Path, final: Path) -> Any:
    if isinstance(value, dict):
        return {key: _replace_staged_path(item, staged, final) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_staged_path(item, staged, final) for item in value]
    if isinstance(value, str):
        return value.replace(str(staged), str(final)).replace(
            str(staged).replace("\\", "/"), str(final).replace("\\", "/")
        )
    return value


def _unstage_failure(error: BaseException, staged: Path, final: Path) -> BaseException:
    """Rewrite ``staged`` back to ``final`` inside a failure report.

    The staging copy is unlinked in ``finally``, so a path that survives into an
    error names a file the caller can no longer open. That matters because the
    point of a read-back mismatch is that the agent can act on it: told the
    document is ``.model.FCStd.x7fk2b``, the obvious next step is to inspect it,
    and that step answers "file does not exist" instead of describing the
    mismatch. Reporting the path the caller passed in keeps the recovery path
    open, so a report is rewritten before it leaves the staging wrapper.
    """
    payload = getattr(error, "payload", None)
    if isinstance(payload, dict):
        error.payload = _replace_staged_path(payload, staged, final)
    if error.args:
        error.args = tuple(
            _replace_staged_path(arg, staged, final) if isinstance(arg, str) else arg
            for arg in error.args
        )
    return error


OUTPUT_LIMIT = 65_536


def _transport_fields(
    started: float,
    exit_code: Optional[int],
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    """Build the per-call transport block: timing, exit status, captured I/O.

    Defined once so the typed driver path and the script path report a child
    process the same way. ``exit_code`` is reported on success as well as on
    failure: a driver that writes a result file and then exits non-zero is a
    real outcome a caller has to be able to see, not a transport detail to be
    swallowed. ``stdout``/``stderr`` are truncated at ``OUTPUT_LIMIT`` with an
    explicit ``*_truncated`` flag rather than silently cut, so a caller can tell
    "that is all it printed" from "there was more".
    """
    return {
        "duration_secs": round(time.monotonic() - started, 3),
        "exit_code": exit_code,
        "stdout": stdout[:OUTPUT_LIMIT],
        "stderr": stderr[:OUTPUT_LIMIT],
        "stdout_truncated": len(stdout) > OUTPUT_LIMIT,
        "stderr_truncated": len(stderr) > OUTPUT_LIMIT,
    }


def _captured(started: float, exit_code: Optional[int], stdout_file, stderr_file) -> dict[str, Any]:
    """Build the transport block straight from a child's open stream files.

    Same shape as :func:`_transport_fields`, for the one case where the streams
    have to be read while the child is still alive - so a timeout can report what
    the script printed before it was killed instead of discarding it.
    """
    stdout_file.flush()
    stderr_file.flush()
    stdout_file.seek(0)
    stderr_file.seek(0)
    return _transport_fields(
        started,
        exit_code,
        stdout_file.read(OUTPUT_LIMIT + 1),
        stderr_file.read(OUTPUT_LIMIT + 1),
    )


_OUTPUT_EXISTS_MESSAGE = "Output already exists; set overwrite=true to replace it"


def _hardlink_unsupported_message(detail: object) -> str:
    return (
        "The output filesystem does not support hard links (%s). No-overwrite copy "
        "publication must publish exclusively so a destination created during the "
        "native call is never replaced. Retry with overwrite=true to replace the "
        "destination, or publish to a filesystem with hard-link support." % (detail,)
    )


def _discard_partial(path: Path) -> None:
    """Remove a publication target this process just created with ``O_EXCL``."""
    try:
        path.unlink()
    except OSError:
        pass


def _publish_exclusive(staged: Path, final: Path) -> None:
    """Publish ``staged`` at ``final`` without replacing an existing file.

    A hard link is the cheapest exclusive publication: it is a single atomic
    commit, and it cannot clobber a destination that appeared while the native
    call was running. Volumes without link support (FAT/exFAT, some SMB and
    FUSE mounts, network shares) are common enough on DCC workstations that
    losing them would turn a working path into a failure, so an exclusive
    create plus copy is kept as the fallback. ``O_EXCL`` preserves the same
    refusal to replace a concurrent destination; only the single-inode commit
    is given up.

    The fallback reproduces the mode the host gave the stage instead of
    asserting its own: a link shares the stage inode, so publishing
    ``0666 & ~umask`` would let the two paths disagree whenever a host or site
    policy creates the stage as ``0600`` - and the volumes that need the
    fallback are exactly the shared mounts where a wider mode is visible to
    other users. The mode is set again after the copy because ``os.open`` masks
    it with the umask, which would strip bits back off in the other direction.

    The create adds owner-write so a failed copy can always discard its own
    partial output: given a read-only stage mode, Windows creates a read-only
    target that ``unlink`` then refuses, leaving an undeletable fragment on the
    publication path this tool owns. The post-copy chmod still commits the
    staged mode.
    """
    try:
        os.link(str(staged), str(final))
        return
    except FileExistsError:
        raise BridgeError(_OUTPUT_EXISTS_MESSAGE) from None
    except OSError as error:
        link_error = error
    try:
        mode = stat.S_IMODE(staged.stat().st_mode)
    except OSError as error:
        raise BridgeError(
            _hardlink_unsupported_message("the staged copy has no readable mode (%s)" % error)
        ) from None
    try:
        descriptor = os.open(str(final), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode | stat.S_IWUSR)
    except FileExistsError:
        raise BridgeError(_OUTPUT_EXISTS_MESSAGE) from None
    except OSError as error:
        raise BridgeError(_hardlink_unsupported_message(error)) from None
    try:
        with os.fdopen(descriptor, "wb") as destination, staged.open("rb") as source:
            shutil.copyfileobj(source, destination)
            # os.open masked the mode with the umask; commit the staged mode
            # through the descriptor where the platform offers it, so the mode
            # lands on the file this call created rather than on whatever now
            # answers to ``final``. Windows has no fchmod.
            if hasattr(os, "fchmod"):
                os.fchmod(destination.fileno(), mode)
            else:
                os.chmod(final, mode)
    except OSError as error:
        _discard_partial(final)
        raise BridgeError(
            _hardlink_unsupported_message(
                "exclusive copy failed with %s after hard links failed with %s"
                % (error, link_error)
            )
        ) from None


def _remove_staged_backups(staged: Path) -> None:
    """Remove only FreeCAD backups derived from a unique staged filename."""
    prefix = "%s." % staged.stem.lower()
    for candidate in staged.parent.iterdir():
        name = candidate.name.lower()
        if candidate.is_file() and name.startswith(prefix) and name.endswith(".fcbak"):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


class FreecadBridge:
    """Run typed, package-owned operations in an isolated FreeCADCmd process."""

    def __init__(
        self,
        executable: Optional[str] = None,
        allowed_roots: Optional[Iterable[Path]] = None,
        max_document_bytes: int = 2 * 1024 * 1024 * 1024,
        max_timeout_secs: float = 1_800,
        backend: str = "freecadcmd",
        module_directory: Optional[str] = None,
        snapshot_directory: Optional[str] = None,
        max_snapshots: int = DEFAULT_MAX_SNAPSHOTS,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        max_script_timeout_secs: float = DEFAULT_MAX_SCRIPT_TIMEOUT_SECS,
    ):
        if backend not in {"freecadcmd", "python-module"}:
            raise ValueError("FreeCAD backend must be freecadcmd or python-module")
        self.backend = backend
        self.module_directory = None
        if backend == "python-module":
            if not executable or not module_directory:
                raise ValueError(
                    "python-module requires an explicit interpreter and module directory"
                )
            interpreter = Path(executable).expanduser().resolve()
            if not interpreter.is_file() or not os.access(str(interpreter), os.X_OK):
                raise ValueError(
                    "python-module interpreter must be an existing executable; "
                    "configure DCC_MCP_FREECAD_PYTHON"
                )
            directory = Path(module_directory).expanduser().resolve()
            if not directory.is_dir() or not any(
                (directory / name).is_file() for name in ("FreeCAD.so", "FreeCAD.pyd")
            ):
                raise ValueError(
                    "Module directory must contain the installed native FreeCAD library"
                )
            self.module_directory = directory
        self.executable = (
            str(interpreter) if backend == "python-module" else self._resolve_executable(executable)
        )
        roots = list(allowed_roots or (Path.cwd(),))
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.max_document_bytes = max(1, int(max_document_bytes))
        self.max_timeout_secs = max(1.0, float(max_timeout_secs))
        self.driver_path = Path(__file__).with_name("freecad_driver.py").resolve()
        self.snapshot_directory = self._snapshot_directory(snapshot_directory)
        self.max_snapshots = max(1, int(max_snapshots))
        self.max_snapshot_bytes = max(1, int(max_snapshot_bytes))
        # A script ceiling is allowed to be lower than the document ceiling but
        # never higher: the point of the separate knob is to bound the escape
        # hatch more tightly than the typed tools, not to open a second, wider
        # limit behind the same process.
        self.max_script_timeout_secs = min(
            float(MAX_SCRIPT_TIMEOUT_SECS), max(1.0, float(max_script_timeout_secs))
        )

    def _snapshot_directory(self, override: Optional[str]) -> Path:
        """Resolve the snapshot store, reusing the allowed-roots gate.

        The store holds verbatim copies of the user's documents, so it must
        live inside ``allowed_roots`` for the same reason documents must: a
        snapshot is a readable copy of the work, and writing it anywhere the
        operator did not sanction would be an exfiltration path. The check is
        the same ``_within`` every other path here goes through rather than a
        second resolver that could drift away from it.
        """
        if override:
            directory = Path(override).expanduser().resolve()
        else:
            directory = Path(self.allowed_roots[0]) / STORE_PARENT / STORE_NAME
        if not _within(directory, self.allowed_roots):
            raise SnapshotError(
                ERROR_STORE_OUTSIDE_ROOTS,
                "Snapshot store %s is outside DCC_MCP_FREECAD_ALLOWED_ROOTS" % directory,
                remediation=[
                    "Point DCC_MCP_FREECAD_SNAPSHOT_DIR inside one of: %s"
                    % ", ".join(str(root) for root in self.allowed_roots),
                    "Or leave it unset to use <first allowed root>/%s/%s."
                    % (STORE_PARENT, STORE_NAME),
                ],
                snapshot_store=str(directory),
            )
        return directory

    def _snapshot_store(self) -> SnapshotStore:
        return SnapshotStore(
            self.snapshot_directory,
            max_snapshots=self.max_snapshots,
            max_snapshot_bytes=self.max_snapshot_bytes,
        )

    def _snapshot_summary(self) -> dict:
        store = self._snapshot_store()
        snapshots, orphans = store.scan()
        count, total = store.usage(snapshots, orphans)
        return {
            "directory": str(store.directory),
            "count": count,
            "total_bytes": total,
            "max_snapshots": store.max_snapshots,
            "max_snapshot_bytes": store.max_snapshot_bytes,
            "orphan_files": len(orphans),
        }

    @classmethod
    def from_env(cls, executable: Optional[str] = None) -> "FreecadBridge":
        backend = os.environ.get("DCC_MCP_FREECAD_BACKEND", "freecadcmd")
        selected_executable = (
            os.environ.get("DCC_MCP_FREECAD_PYTHON")
            if backend == "python-module"
            else os.environ.get("DCC_MCP_FREECAD_EXECUTABLE")
        )
        roots_value = os.environ.get("DCC_MCP_FREECAD_ALLOWED_ROOTS", "")
        roots = _split_roots(roots_value) if roots_value else [Path.cwd().resolve()]
        max_document_bytes = int(
            os.environ.get("DCC_MCP_FREECAD_MAX_DOCUMENT_BYTES", str(2 * 1024**3))
        )
        max_timeout_secs = float(os.environ.get("DCC_MCP_FREECAD_MAX_TIMEOUT_SECS", "1800"))
        max_snapshots = int(
            os.environ.get("DCC_MCP_FREECAD_MAX_SNAPSHOTS", str(DEFAULT_MAX_SNAPSHOTS))
        )
        max_snapshot_bytes = int(
            os.environ.get("DCC_MCP_FREECAD_MAX_SNAPSHOT_BYTES", str(DEFAULT_MAX_SNAPSHOT_BYTES))
        )
        max_script_timeout_secs = float(
            os.environ.get(
                "DCC_MCP_FREECAD_MAX_SCRIPT_TIMEOUT_SECS", str(DEFAULT_MAX_SCRIPT_TIMEOUT_SECS)
            )
        )
        if max_document_bytes <= 0 or max_timeout_secs <= 0:
            raise ValueError("FreeCAD document and timeout limits must be positive")
        if max_snapshots <= 0 or max_snapshot_bytes <= 0:
            raise ValueError("FreeCAD snapshot limits must be positive")
        if max_script_timeout_secs <= 0:
            raise ValueError("FreeCAD script timeout limit must be positive")
        return cls(
            executable or selected_executable or None,
            allowed_roots=roots,
            max_document_bytes=max_document_bytes,
            max_timeout_secs=max_timeout_secs,
            backend=backend,
            module_directory=os.environ.get("DCC_MCP_FREECAD_MODULE_DIRECTORY") or None,
            snapshot_directory=os.environ.get("DCC_MCP_FREECAD_SNAPSHOT_DIR") or None,
            max_snapshots=max_snapshots,
            max_snapshot_bytes=max_snapshot_bytes,
            max_script_timeout_secs=max_script_timeout_secs,
        )

    @staticmethod
    def _resolve_executable(explicit: Optional[str]) -> Optional[str]:
        candidates = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        else:
            for name in ("FreeCADCmd", "freecadcmd"):
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
            if os.name == "nt":
                program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                candidates.extend(
                    (
                        program_files / "FreeCAD 1.1" / "bin" / "FreeCADCmd.exe",
                        program_files / "FreeCAD 1.0" / "bin" / "FreeCADCmd.exe",
                        program_files / "FreeCAD" / "bin" / "FreeCADCmd.exe",
                    )
                )
            elif sys.platform == "darwin":
                candidates.extend(
                    (
                        Path("/Applications/FreeCAD.app"),
                        Path.home() / "Applications" / "FreeCAD.app",
                    )
                )
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_dir():
                options = (
                    candidate / "FreeCADCmd.exe",
                    candidate / "bin" / "FreeCADCmd.exe",
                    candidate / "freecadcmd",
                    candidate / "bin" / "freecadcmd",
                    candidate / "Contents" / "Resources" / "bin" / "FreeCADCmd",
                    candidate / "Contents" / "Resources" / "bin" / "freecadcmd",
                    candidate / "Contents" / "MacOS" / "FreeCADCmd",
                )
                candidate = next((item for item in options if item.is_file()), candidate)
            if candidate.is_file():
                return str(candidate)
        return None

    def _timeout(self, value: float) -> float:
        timeout = float(value)
        if timeout <= 0 or timeout > self.max_timeout_secs:
            raise BridgeError(
                "timeout_secs must be greater than 0 and no more than %s"
                % int(self.max_timeout_secs)
            )
        return timeout

    def _document_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != _DOCUMENT_SUFFIX:
            raise BridgeError("FreeCAD document paths must end with .FCStd")
        if not path.is_file():
            raise BridgeError("FreeCAD document does not exist: %s" % path)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Document is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        if path.stat().st_size > self.max_document_bytes:
            raise BridgeError("FreeCAD document exceeds the configured size limit")
        return path

    def _restorable_document_path(self, value: str) -> Path:
        """Like :meth:`_document_path` but tolerates a document that is gone.

        Same suffix, same allowed-roots gate, same size ceiling when a file is
        present -- the only difference is that a missing path is allowed, because
        ``restore_snapshot`` onto a deleted document *is* the recovery path. The
        parent directory must still exist: creating a document tree the caller
        did not ask for would turn one typo into a whole new subtree.
        """
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != _DOCUMENT_SUFFIX:
            raise BridgeError("FreeCAD document paths must end with .FCStd")
        if not _within(path, self.allowed_roots):
            raise BridgeError("Document is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        if not path.parent.is_dir():
            raise BridgeError("Document directory does not exist: %s" % path.parent)
        if path.is_file() and path.stat().st_size > self.max_document_bytes:
            raise BridgeError("FreeCAD document exceeds the configured size limit")
        return path

    def _output_path(self, value: str, suffixes: set[str]) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in suffixes:
            raise BridgeError("Unsupported output extension: %s" % path.suffix)
        if not path.parent.is_dir():
            raise BridgeError("Output directory does not exist: %s" % path.parent)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Output is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        return path

    def _input_path(self, value: str, suffixes: set[str]) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in suffixes:
            raise BridgeError("Unsupported input extension: %s" % path.suffix)
        if not path.is_file():
            raise BridgeError("Input geometry does not exist: %s" % path)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Input geometry is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        return path

    @staticmethod
    def _object_name(value: str) -> str:
        if not _OBJECT_NAME.fullmatch(value):
            raise BridgeError(
                "Object names must start with a letter or underscore and contain at most "
                "64 letters, digits, or underscores"
            )
        return value

    def _invoke(
        self, method: str, params: Mapping[str, Any], timeout_secs: float = 120
    ) -> dict[str, Any]:
        if not self.executable:
            raise BridgeError("FreeCADCmd was not found; set DCC_MCP_FREECAD_EXECUTABLE")
        if not self.driver_path.is_file():
            raise BridgeError("Packaged FreeCAD driver is missing")
        timeout = self._timeout(timeout_secs)
        with tempfile.TemporaryDirectory(prefix="dcc-mcp-freecad-") as temp_dir_value:
            temp_dir = Path(temp_dir_value)
            request_path = temp_dir / "request.json"
            result_path = temp_dir / "result.json"
            config_path = temp_dir / "user.cfg"
            request_path.write_text(
                json.dumps({"method": method, "params": dict(params)}, ensure_ascii=False),
                encoding="utf-8",
            )
            if self.backend == "python-module":
                command = [
                    self.executable,
                    "-I",
                    str(self.driver_path.with_name("module_runner.py")),
                    str(self.module_directory),
                    str(request_path),
                    str(result_path),
                ]
            else:
                command = [
                    self.executable,
                    "--safe-mode",
                    "--user-cfg",
                    str(config_path),
                    str(self.driver_path),
                    "--pass",
                    str(request_path),
                    str(result_path),
                ]
            started = time.monotonic()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            environment = os.environ.copy()
            environment.setdefault("QT_QPA_PLATFORM", "offscreen")
            # Ask Mesa for a software rasteriser. A headless call has no GPU, and
            # FreeCAD's default saveImage backend creates its own OpenGL context,
            # so without this a host with only a hardware driver may produce a
            # context it cannot use. This is a hint Mesa honours and every other
            # platform ignores, and an operator can still override it.
            environment.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
            if self.backend == "python-module":
                # A compatible installed native library, never arbitrary caller code.
                # Keep every temporary preference/cache write out of the operator home.
                for key, child in [
                    ("HOME", "home"),
                    ("XDG_CONFIG_HOME", "config"),
                    ("XDG_CACHE_HOME", "cache"),
                    ("XDG_DATA_HOME", "data"),
                    ("FREECAD_USER_HOME", "freecad-home"),
                ]:
                    folder = temp_dir / child
                    folder.mkdir()
                    environment[key] = str(folder)
                environment.pop("PYTHONPATH", None)
                environment.pop("PYTHONHOME", None)
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file:
                with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
                    process = subprocess.Popen(
                        command,
                        cwd=str(temp_dir),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        text=True,
                        creationflags=creationflags,
                    )
                    try:
                        deadline = started + timeout
                        while process.poll() is None:
                            check_dcc_cancelled()
                            if time.monotonic() >= deadline:
                                raise BridgeTimeoutError(
                                    "FreeCAD %s backend exceeded the %.1f second timeout"
                                    % (self.backend, timeout)
                                )
                            time.sleep(0.05)
                    except BaseException:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read(65_537)
                    stderr = stderr_file.read(65_537)
            if not result_path.is_file():
                raise BridgeError(
                    "FreeCAD %s backend did not return a result (exit %s): %s"
                    % (self.backend, process.returncode, (stderr or stdout).strip()[:1_000])
                )
            if result_path.stat().st_size > 16 * 1024 * 1024:
                raise BridgeError("FreeCAD result exceeded the 16 MiB response limit")
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if not payload.get("ok"):
                error = payload.get("error") or {}
                message = str(error.get("message") or "FreeCAD operation failed")
                verification = error.get("write_verification")
                if isinstance(verification, dict):
                    raise WriteVerificationError(verification, message)
                state = error.get("sketch_state")
                if isinstance(state, dict):
                    raise SketchStateError(message, error.get("code"), state)
                code = error.get("code")
                raise BridgeError(message, str(code) if code else None)
            result = payload.get("result")
            if not isinstance(result, dict):
                result = {"result": result}
            result.update(_transport_fields(started, process.returncode, stdout, stderr))
            return result

    def _script_path(self, value: str) -> Path:
        """Resolve a script the caller is allowed to name.

        The gate is the same one every other path here goes through. Symbolic
        links are resolved *before* the check rather than after, so a link
        inside an allowed root that points at a file outside it is refused
        instead of read - without that, containment would be a property of the
        link's name and not of the file actually executed.
        """
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in SCRIPT_SUFFIXES:
            raise BridgeError("Script paths must end with .py")
        if not path.is_file():
            raise BridgeError("Script does not exist: %s" % path)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Script is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        return path

    def _script_timeout(self, value: float) -> float:
        """Bound a script deadline by the script ceiling, not the document one.

        ``_timeout`` enforces ``max_timeout_secs`` (1800s by default), which is
        the right bound for a document round-trip and the wrong one for the
        escape hatch: a script that blocks on a modal dialog should be killed in
        minutes. Both ceilings are applied, the stricter one winning.
        """
        timeout = float(value)
        if timeout <= 0 or timeout > self.max_script_timeout_secs:
            raise BridgeError(
                "timeout_secs must be greater than 0 and no more than %s for run_script"
                % int(self.max_script_timeout_secs)
            )
        return self._timeout(timeout)

    def run_script(
        self,
        script_path: str,
        timeout_secs: float = DEFAULT_MAX_SCRIPT_TIMEOUT_SECS,
    ) -> dict[str, Any]:
        """Execute one caller-named ``.py`` file in a disposable FreeCAD child.

        This is the adapter's escape hatch: the typed tools cover a bounded
        document workflow and everything outside it has nowhere to go. It is
        deliberately narrower than a general ``execute_code`` and is **not a
        sandbox**:

        * Only a path is accepted - never source text - so there is no inline
          string to inject through.
        * The path is resolved and then checked against
          ``DCC_MCP_FREECAD_ALLOWED_ROOTS``, so a link is judged by the file it
          points at rather than by where it is named.
        * The child runs as the operator's own account with that account's full
          privileges. Allowed roots constrain *which script may be named*, not
          *what the script may do*.
        * The child is a package-owned runner, and the native-library directory
          is spliced behind the standard library rather than in front of it, so
          a same-named module shipped next to a script cannot be imported by the
          runner before the script runs.
        * ``--safe-mode`` plus a temporary user config means no user workbench,
          plugin, or macro is loaded (the vacuum mode), which is what keeps the
          upstream modal-dialog and broken-FeaturePython hangs from being
          reachable through this door.

        ``script_sha256`` is returned so a caller can tell two different
        scripts apart, and ``exit_code`` so a script that reports success on
        stdout but fails is not mistaken for a clean run.

        A deadline overrun raises :class:`BridgeTimeoutError` rather than
        returning, because the alternative - returning a payload whose
        ``timed_out`` the caller might ignore - reports a hang as a completed
        run. The exception carries ``partial``, the transport block plus the
        output captured before the child was killed: with a script blocked on a
        modal dialog, that output is the only evidence of where it stopped.
        """
        if not self.executable:
            raise BridgeError("FreeCADCmd was not found; set DCC_MCP_FREECAD_EXECUTABLE")
        # Checked before the caller's own argument: a broken install is the more
        # actionable error, and refusing on it first keeps a misconfigured wheel
        # from being reported as a bad path the caller should fix.
        if not self.driver_path.is_file():
            raise BridgeError("Packaged FreeCAD driver is missing")
        runner_path = self.driver_path.with_name(SCRIPT_RUNNER_NAME)
        if not runner_path.is_file():
            raise BridgeError("Packaged FreeCAD script runner is missing")
        script = self._script_path(script_path)
        # Hashed before the child starts, so the digest is a fingerprint of what
        # was executed rather than of whatever the file looks like afterwards. A
        # script that rewrites itself would otherwise be reported under the hash
        # of its replacement, which makes the audit field actively misleading.
        script_sha256 = sha256_file(script)
        timeout = self._script_timeout(timeout_secs)
        with tempfile.TemporaryDirectory(prefix="dcc-mcp-freecad-") as temp_dir_value:
            temp_dir = Path(temp_dir_value)
            config_path = temp_dir / "user.cfg"
            if self.backend == "python-module":
                command = [
                    self.executable,
                    "-I",
                    str(runner_path),
                    str(self.module_directory),
                    str(script),
                ]
            else:
                command = [
                    self.executable,
                    "--safe-mode",
                    "--user-cfg",
                    str(config_path),
                    str(runner_path),
                    "--script",
                    str(script),
                ]
            started = time.monotonic()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            environment = os.environ.copy()
            environment.setdefault("QT_QPA_PLATFORM", "offscreen")
            if self.backend == "python-module":
                # Identical containment to _invoke: keep every temporary
                # preference/cache write out of the operator home, and do not
                # let a caller-controlled PYTHONPATH reach the runner.
                for key, child in [
                    ("HOME", "home"),
                    ("XDG_CONFIG_HOME", "config"),
                    ("XDG_CACHE_HOME", "cache"),
                    ("XDG_DATA_HOME", "data"),
                    ("FREECAD_USER_HOME", "freecad-home"),
                ]:
                    folder = temp_dir / child
                    folder.mkdir()
                    environment[key] = str(folder)
                environment.pop("PYTHONPATH", None)
                environment.pop("PYTHONHOME", None)
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file:
                with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
                    process = subprocess.Popen(
                        command,
                        cwd=str(temp_dir),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        text=True,
                        creationflags=creationflags,
                    )
                    timed_out = False
                    try:
                        deadline = started + timeout
                        while process.poll() is None:
                            check_dcc_cancelled()
                            if time.monotonic() >= deadline:
                                timed_out = True
                                raise BridgeTimeoutError(
                                    "FreeCAD %s backend exceeded the %.1f second script timeout"
                                    % (self.backend, timeout),
                                    # Read the child's streams *before* it is
                                    # terminated: a script blocked on a modal
                                    # dialog has usually already printed where it
                                    # got to, and that trace is the only answer to
                                    # "what was it doing when it hung".
                                    partial={
                                        "script_path": str(script),
                                        "script_sha256": script_sha256,
                                        "timed_out": True,
                                        **_captured(
                                            started,
                                            None,
                                            stdout_file,
                                            stderr_file,
                                        ),
                                    },
                                )
                            time.sleep(0.05)
                    except BaseException:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read(OUTPUT_LIMIT + 1)
                    stderr = stderr_file.read(OUTPUT_LIMIT + 1)
        return {
            "script_path": str(script),
            "script_sha256": script_sha256,
            "timed_out": timed_out,
            **_transport_fields(started, process.returncode, stdout, stderr),
        }

    def _mutate_document(
        self,
        method: str,
        document_path: str,
        params: Mapping[str, Any],
        timeout_secs: float,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % document.stem,
            suffix=document.suffix,
            dir=str(document.parent),
        )
        os.close(descriptor)
        staged = Path(temp_name)
        try:
            shutil.copy2(str(document), str(staged))
            request = dict(params)
            request["document_path"] = str(staged)
            try:
                result = self._invoke(method, request, timeout_secs)
            except BaseException as exc:
                raise _unstage_failure(exc, staged, document) from None
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD mutation did not produce a durable document")
            os.replace(str(staged), str(document))
            result = _replace_staged_path(result, staged, document)
            durable = self._invoke(
                "document.inspect", {"document_path": str(document)}, timeout_secs
            )
            transport_keys = {
                "duration_secs",
                "stderr",
                "stderr_truncated",
                "stdout",
                "stdout_truncated",
            }
            result["document"] = {
                key: value for key, value in durable.items() if key not in transport_keys
            }
            result.update(
                {
                    "document_path": str(document),
                    "document_bytes": document.stat().st_size,
                    "document_sha256": sha256_file(document),
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()
            _remove_staged_backups(staged)

    def status(self, timeout_secs: float = 30) -> dict[str, Any]:
        if not self.executable:
            return {
                "ready": False,
                "executable": None,
                "instance_type": "standalone",
                "reason": "freecadcmd_not_found",
                "allowed_roots": [str(root) for root in self.allowed_roots],
                "snapshot_store": self._snapshot_summary(),
            }
        result = self._invoke("system.status", {}, timeout_secs)
        result.update(
            {
                "ready": True,
                "snapshot_store": self._snapshot_summary(),
                "backend": self.backend,
                "module_directory": str(self.module_directory) if self.module_directory else None,
                "executable": self.executable,
                "instance_type": "standalone",
                "allowed_roots": [str(root) for root in self.allowed_roots],
                "max_document_bytes": self.max_document_bytes,
                "max_timeout_secs": self.max_timeout_secs,
                "max_script_timeout_secs": self.max_script_timeout_secs,
            }
        )
        return result

    def capabilities(self) -> dict[str, Any]:
        # Every declaration below is derived from skills/*/tools.yaml and from
        # the suffix sets enforced by _input_path / _output_path, so the
        # capability list and the schemas the tools actually enforce cannot
        # drift apart. The parts and render blocks describe the running process
        # rather than a tool schema, so the bridge computes them and passes them
        # in. See dcc_mcp_freecad.capabilities.
        return build_capabilities(
            host_status=self.status(),
            import_extensions=_IMPORT_SUFFIXES,
            export_extensions=_EXPORT_SUFFIXES,
            parts={
                "part_extensions": sorted(parts_library.PART_SUFFIXES),
                "parts_library": self._parts_library_capability(),
            },
            render={"render_view": self._render_capability(self.status())},
            max_script_timeout_secs=self.max_script_timeout_secs,
        )

    @staticmethod
    def _render_capability(status: Mapping[str, Any]) -> dict[str, Any]:
        """Describe raster rendering, marking a host that cannot do it.

        A console-only FreeCAD build has no ``FreeCADGui``, and an offscreen host
        may have it while still being unable to drive GL. Neither is allowed to
        look like a working renderer, so the report says which situation this is
        and how to fix it rather than waiting for a caller to render and wonder.
        """
        gui = status.get("gui_library")
        importable = isinstance(gui, dict) and gui.get("importable") is True
        if not status.get("ready"):
            state = "host_limited"
            remediation = (
                "No usable FreeCAD backend was found, so nothing can be rendered. Run "
                "`dcc-mcp-freecad doctor --json` and set DCC_MCP_FREECAD_EXECUTABLE to a "
                "supported FreeCADCmd."
            )
        elif not importable:
            state = "host_limited"
            remediation = (
                "This FreeCAD build does not expose the FreeCADGui module, so no view can be "
                "captured. Install a FreeCAD build that ships the GUI libraries (the official "
                "AppImage and the Windows/macOS installers do); a console-only or "
                "headless-only build cannot render. Reported by the host as: %s"
                % (gui.get("error") if isinstance(gui, dict) else "unknown")
            )
        else:
            state = "available"
            remediation = None
        return {
            "status": state,
            "views": sorted(VIEWS),
            "default_width": DEFAULT_RENDER_WIDTH,
            "default_height": DEFAULT_RENDER_HEIGHT,
            "max_width": MAX_RENDER_WIDTH,
            "max_height": MAX_RENDER_HEIGHT,
            "image_included_by_default": False,
            "waste_image_detection": True,
            "error_codes": sorted(_RENDER_REMEDIATION),
            "host_gui_importable": importable,
            "remediation": remediation,
        }

    def _parts_library_capability(self) -> dict[str, Any]:
        """Describe the configured library without letting it break discovery.

        An unconfigured library is the normal state, not a fault, so
        ``get_capabilities`` reports it with the error code that explains how
        to configure it instead of failing the whole call.
        """
        capability = {
            "offline_only": True,
            "remote_sources": False,
            "max_limit": parts_library.MAX_LIMIT,
            "env_var": parts_library.ENV_LIBRARY_ROOTS,
        }
        try:
            roots = self.parts_library_roots()
        except parts_library.PartLibraryError as error:
            capability.update(
                {"configured": False, "roots": [], "error_code": error.code, "reason": str(error)}
            )
            return capability
        capability.update({"configured": True, "roots": [str(root) for root in roots]})
        return capability

    def create_document(
        self,
        path: str,
        overwrite: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        output = self._output_path(path, {_DOCUMENT_SUFFIX})
        name = re.sub(r"[^A-Za-z0-9_]", "_", output.stem)
        if not name or name[0].isdigit():
            name = "Document_%s" % name
        replaced_existing = output.exists()
        if replaced_existing and not overwrite:
            raise BridgeError("Document already exists; set overwrite=true to replace it")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output.stem, suffix=output.suffix, dir=str(output.parent)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        try:
            try:
                result = self._invoke(
                    "document.create",
                    {"document_path": str(staged), "name": name},
                    timeout_secs,
                )
            except BaseException as exc:
                raise _unstage_failure(exc, staged, output) from None
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD did not create a durable document")
            os.replace(str(staged), str(output))
            durable = self._invoke("document.inspect", {"document_path": str(output)}, timeout_secs)
            if "duration_secs" in result:
                durable["create_duration_secs"] = result["duration_secs"]
            result = durable
            result.update(
                {
                    "document_path": str(output),
                    "document_bytes": output.stat().st_size,
                    "document_sha256": sha256_file(output),
                    "overwritten": replaced_existing,
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()
            _remove_staged_backups(staged)

    def inspect_document(self, path: str, timeout_secs: float = 120) -> dict[str, Any]:
        document = self._document_path(path)
        result = self._invoke("document.inspect", {"document_path": str(document)}, timeout_secs)
        result.update(
            {
                "document_path": str(document),
                "document_bytes": document.stat().st_size,
                "document_sha256": sha256_file(document),
            }
        )
        return result

    def validate_document(self, path: str, timeout_secs: float = 120) -> dict[str, Any]:
        document = self._document_path(path)
        return self._invoke("document.validate", {"document_path": str(document)}, timeout_secs)

    def save_copy(
        self,
        source_path: str,
        output_path: str,
        overwrite: bool = False,
        timeout_secs: float = 120,
        visible_objects: Optional[list[str]] = None,
        view: str = "isometric",
        appearances: Optional[list[dict[str, Any]]] = None,
        frame_margin: Optional[float] = None,
    ) -> dict[str, Any]:
        source = self._document_path(source_path)
        output = self._output_path(output_path, {_DOCUMENT_SUFFIX})
        presentation = {}
        if visible_objects is None and (
            view != "isometric" or appearances is not None or frame_margin is not None
        ):
            raise BridgeError("Presentation options require an explicit visible_objects selection")
        if visible_objects is not None:
            from .presentation import validate_options

            validate_options(visible_objects, view, appearances, frame_margin)
            for name in visible_objects:
                self._object_name(name)
            if source == output:
                raise BridgeError("Presentation copies must not replace the source")
            presentation = {"visible_objects": visible_objects, "view": view}
            if appearances is not None:
                presentation["appearances"] = appearances
            if frame_margin is not None:
                presentation["frame_margin"] = frame_margin
        replaced_existing = output.exists()
        if replaced_existing and not overwrite:
            raise BridgeError(_OUTPUT_EXISTS_MESSAGE)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output.stem, suffix=output.suffix, dir=str(output.parent)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        try:
            try:
                result = self._invoke(
                    "document.save_copy",
                    {"document_path": str(source), "output_path": str(staged), **presentation},
                    timeout_secs,
                )
            except BaseException as exc:
                raise _unstage_failure(exc, staged, output) from None
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD did not create the document copy")
            # Read metadata and prepare the response before the publication
            # boundary. An unreadable stage must leave any old output intact.
            result = _replace_staged_path(result, staged, output)
            result.update(
                {
                    "source_path": str(source),
                    "output_path": str(output),
                    "bytes": staged.stat().st_size,
                    "sha256": sha256_file(staged),
                    "overwritten": replaced_existing,
                }
            )
            check_dcc_cancelled()
            if overwrite:
                os.replace(str(staged), str(output))
            else:
                # Publish the complete native file exclusively. A destination
                # created during the native call must never be overwritten.
                _publish_exclusive(staged, output)
            return result
        finally:
            # Cleanup owns only the unique stage and its native backups. A
            # filesystem cleanup error must not change a committed success or
            # obscure the original native failure/cancellation.
            try:
                if staged.exists():
                    staged.unlink()
            except OSError:
                pass
            try:
                _remove_staged_backups(staged)
            except OSError:
                pass

    @staticmethod
    def _analyse_render(subject_path: Path, baseline_path: Path) -> dict[str, Any]:
        """Measure a captured frame, refusing anything that carries no signal.

        Runs before publication, so a frame that fails here never reaches the
        caller's output path and never appears in a response as an image.
        """
        for path in (subject_path, baseline_path):
            if not path.is_file():
                raise BridgeError("The native host captured no image at %s" % path)
        subject_bytes = subject_path.read_bytes()
        baseline_bytes = baseline_path.read_bytes()
        subject_width, subject_height, subject_opaque, subject_rgb = raster.decode_rgb(
            subject_bytes
        )
        baseline_width, baseline_height, baseline_opaque, baseline_rgb = raster.decode_rgb(
            baseline_bytes
        )
        subject = raster.describe(
            subject_width, subject_height, subject_opaque, raster.statistics(subject_rgb)
        )
        baseline = raster.describe(
            baseline_width, baseline_height, baseline_opaque, raster.statistics(baseline_rgb)
        )
        reasons = raster.degeneracy_reasons(subject)
        changed = 0
        fraction = 0.0
        if (subject_width, subject_height) != (baseline_width, baseline_height):
            reasons.append(
                "the requested frame is %dx%d but the empty-scene reference is %dx%d, so the "
                "two captures are not comparable"
                % (subject_width, subject_height, baseline_width, baseline_height)
            )
        else:
            changed, fraction = raster.differing_pixel_fraction(subject_rgb, baseline_rgb)
        geometry = raster.geometry_reasons(changed, subject["pixel_count"], fraction)
        if reasons or geometry:
            code = "degenerate_render" if reasons else "empty_render"
            combined = reasons + geometry
            raise RenderVerificationError(
                {
                    "error_code": code,
                    "tool": "document.render_view",
                    "statistics": subject,
                    "baseline_statistics": baseline,
                    "geometry_pixel_count": changed,
                    "geometry_pixel_fraction": fraction,
                    "reasons": combined,
                    "remediation": _RENDER_REMEDIATION[code],
                },
                "The native host captured a frame this adapter refuses to return (%s): %s "
                "Remediation: %s" % (code, "; ".join(combined), _RENDER_REMEDIATION[code]),
            )
        return {
            "statistics": subject,
            "baseline_statistics": baseline,
            "geometry_pixel_count": changed,
            "geometry_pixel_fraction": fraction,
        }

    def render_view(
        self,
        document_path: str,
        output_path: Optional[str] = None,
        overwrite: bool = False,
        timeout_secs: float = 300,
        visible_objects: Optional[list[str]] = None,
        view: str = "isometric",
        appearances: Optional[list[dict[str, Any]]] = None,
        frame_margin: Optional[float] = None,
        width: int = DEFAULT_RENDER_WIDTH,
        height: int = DEFAULT_RENDER_HEIGHT,
        include_image: bool = False,
    ) -> dict[str, Any]:
        """Render a document view to a PNG and prove the frame is not waste.

        ``include_image`` defaults to false on purpose: returning a picture on
        every call is how a visual-feedback feature becomes a context-cost
        problem. The default response is text -- whether the render is usable,
        the pixel summary, and what was in frame -- and the image is attached
        only when it is explicitly asked for.

        The render is read-only with respect to the document: the host records
        the native camera, visibility and selection, frames the scene, captures,
        and puts all of them back before returning. The source file is never
        written.
        """
        source = self._document_path(document_path)
        if not isinstance(include_image, bool):
            raise BridgeError("include_image must be a boolean")
        if not isinstance(overwrite, bool):
            raise BridgeError("overwrite must be a boolean")
        width, height = validate_render_size(width, height)
        if visible_objects is None:
            if appearances is not None or frame_margin is not None:
                raise BridgeError(
                    "appearances and frame_margin require an explicit visible_objects selection"
                )
        else:
            validate_options(visible_objects, view, appearances, frame_margin)
            for name in visible_objects:
                self._object_name(name)
        output = self._output_path(output_path, {_RENDER_SUFFIX}) if output_path else None
        replaced_existing = bool(output is not None and output.exists())
        if replaced_existing and not overwrite:
            raise BridgeError(_OUTPUT_EXISTS_MESSAGE)
        request: dict[str, Any] = {
            "document_path": str(source),
            "view": view,
            "width": width,
            "height": height,
        }
        if visible_objects is not None:
            request["visible_objects"] = list(visible_objects)
        if appearances is not None:
            request["appearances"] = appearances
        if frame_margin is not None:
            request["frame_margin"] = frame_margin
        with tempfile.TemporaryDirectory(prefix="dcc-mcp-freecad-render-") as work_value:
            work = Path(work_value)
            baseline = work / "empty-scene.png"
            if output is None:
                # Nothing is published; the frame is measured and discarded so a
                # caller asking "did this render?" leaves no artefact behind.
                staged = work / "render.png"
            else:
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=".%s." % output.stem, suffix=_RENDER_SUFFIX, dir=str(output.parent)
                )
                os.close(descriptor)
                staged = Path(temp_name)
                staged.unlink()
            try:
                result = self._invoke(
                    "document.render_view",
                    dict(request, image_path=str(staged), baseline_path=str(baseline)),
                    timeout_secs,
                )
                image = staged.read_bytes()
                verdict = self._analyse_render(staged, baseline)
                if include_image and len(image) > MAX_INLINE_IMAGE_BYTES:
                    raise BridgeError(
                        "The rendered PNG is %d bytes, over the %d byte inline limit. Publish "
                        "it with output_path and read the file instead."
                        % (len(image), MAX_INLINE_IMAGE_BYTES)
                    )
                response = dict(result, **verdict)
                response.update(
                    {
                        "document_path": str(source),
                        "output_path": None if output is None else str(output),
                        "image_bytes": len(image),
                        "image_sha256": hashlib.sha256(image).hexdigest(),
                        "overwritten": replaced_existing,
                    }
                )
                check_dcc_cancelled()
                if output is not None:
                    if overwrite:
                        os.replace(str(staged), str(output))
                    else:
                        _publish_exclusive(staged, output)
                    # Read back from the published file, so the reported hash
                    # describes the artefact the caller can actually open rather
                    # than the staged copy it was published from.
                    response["bytes"] = output.stat().st_size
                    response["sha256"] = sha256_file(output)
                if include_image:
                    response["image_base64"] = base64.b64encode(image).decode("ascii")
                    response["image_media_type"] = "image/png"
                return response
            finally:
                try:
                    if staged.exists():
                        staged.unlink()
                except OSError:
                    pass

    def add_primitive(
        self,
        document_path: str,
        primitive: str,
        name: str,
        label: Optional[str] = None,
        dimensions: Optional[Mapping[str, float]] = None,
        translation: Optional[Sequence[float]] = None,
        rotation_axis: Optional[Sequence[float]] = None,
        rotation_degrees: float = 0,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.add_primitive",
            document_path,
            {
                "primitive": primitive,
                "name": self._object_name(name),
                "label": label,
                "dimensions": dict(dimensions or {}),
                "translation": list(translation or (0, 0, 0)),
                "rotation_axis": list(rotation_axis or (0, 0, 1)),
                "rotation_degrees": rotation_degrees,
            },
            timeout_secs,
        )

    def update_primitive(
        self,
        document_path: str,
        object_name: str,
        dimensions: Mapping[str, float],
        label: Optional[str] = None,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        if not dimensions:
            raise BridgeError("dimensions must contain at least one property")
        return self._mutate_document(
            "model.update_primitive",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "dimensions": dict(dimensions),
                "label": label,
            },
            timeout_secs,
        )

    def transform_object(
        self,
        document_path: str,
        object_name: str,
        translation: Sequence[float],
        rotation_axis: Sequence[float] = (0, 0, 1),
        rotation_degrees: float = 0,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.transform_object",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "translation": list(translation),
                "rotation_axis": list(rotation_axis),
                "rotation_degrees": rotation_degrees,
            },
            timeout_secs,
        )

    def boolean_operation(
        self,
        document_path: str,
        operation: str,
        base_object: str,
        tool_object: str,
        result_name: str,
        result_label: Optional[str] = None,
        timeout_secs: float = 300,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.boolean_operation",
            document_path,
            {
                "operation": operation,
                "base_object": self._object_name(base_object),
                "tool_object": self._object_name(tool_object),
                "result_name": self._object_name(result_name),
                "result_label": result_label,
            },
            timeout_secs,
        )

    def remove_object(
        self,
        document_path: str,
        object_name: str,
        cascade: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "document.remove_object",
            document_path,
            {"object_name": self._object_name(object_name), "cascade": bool(cascade)},
            timeout_secs,
        )

    def import_geometry(
        self,
        document_path: str,
        input_path: str,
        object_name: str,
        label: Optional[str] = None,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        source = self._input_path(input_path, _IMPORT_SUFFIXES)
        return self._mutate_document(
            "model.import_geometry",
            document_path,
            {
                "input_path": str(source),
                "object_name": self._object_name(object_name),
                "label": label,
            },
            timeout_secs,
        )

    def parts_library_roots(self) -> list[Path]:
        """The configured local parts-library roots, or a refusal with a code."""
        return parts_library.resolve_library_roots()

    def list_parts(
        self,
        category: Optional[str] = None,
        query: Optional[str] = None,
        limit: Any = parts_library.DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """List the offline library. Needs no FreeCAD host, so it starts none."""
        return parts_library.list_parts(
            category=category, query=query, limit=limit, roots=self.parts_library_roots()
        )

    def insert_part(
        self,
        document_path: str,
        part_ref: str,
        object_name: str,
        label: Optional[str] = None,
        translation: Optional[Sequence[float]] = None,
        rotation_axis: Optional[Sequence[float]] = None,
        rotation_degrees: float = 0,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        """Insert a standard part from the configured offline library.

        The reference is resolved and containment-checked *before* the document
        is staged, so a refused ``part_ref`` costs nothing and can never leave
        a half-applied mutation behind. A caller never names a file: only the
        relative paths ``list_parts`` returned are accepted.
        """
        path, root = parts_library.resolve_part_ref(part_ref, self.parts_library_roots())
        return self._mutate_document(
            "model.insert_part",
            document_path,
            {
                "part_path": str(path),
                "part_ref": path.relative_to(root).as_posix(),
                "part_root": str(root),
                "object_name": self._object_name(object_name),
                "label": label,
                "translation": list(translation or (0, 0, 0)),
                "rotation_axis": list(rotation_axis or (0, 0, 1)),
                "rotation_degrees": rotation_degrees,
            },
            timeout_secs,
        )

    def export_geometry(
        self,
        document_path: str,
        object_names: Sequence[str],
        output_path: str,
        linear_deflection: float = 0.1,
        angular_deflection_degrees: float = 15,
        overwrite: bool = False,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        if not object_names or len(object_names) > 100:
            raise BridgeError("object_names must contain between 1 and 100 names")
        names = [self._object_name(name) for name in object_names]
        output = self._output_path(output_path, _EXPORT_SUFFIXES)
        replaced_existing = output.exists()
        if replaced_existing and not overwrite:
            raise BridgeError("Output already exists; set overwrite=true to replace it")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output.stem, suffix=output.suffix, dir=str(output.parent)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        try:
            try:
                result = self._invoke(
                    "model.export_geometry",
                    {
                        "document_path": str(document),
                        "object_names": names,
                        "output_path": str(staged),
                        "linear_deflection": linear_deflection,
                        "angular_deflection_degrees": angular_deflection_degrees,
                    },
                    timeout_secs,
                )
            except BaseException as exc:
                raise _unstage_failure(exc, staged, output) from None
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD did not create a durable export")
            os.replace(str(staged), str(output))
            result = _replace_staged_path(result, staged, output)
            result.update(
                {
                    "document_path": str(document),
                    "output_path": str(output),
                    "bytes": output.stat().st_size,
                    "sha256": sha256_file(output),
                    "overwritten": replaced_existing,
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()
            _remove_staged_backups(staged)

    def fillet_edges(
        self,
        document_path: str,
        object_name: str,
        edge_refs: Sequence[int],
        radius: float,
        result_name: str,
        result_label: Optional[str] = None,
        timeout_secs: float = 300,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.fillet_edges",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "edge_refs": list(edge_refs),
                "radius": radius,
                "result_name": self._object_name(result_name),
                "result_label": result_label,
            },
            timeout_secs,
        )

    def chamfer_edges(
        self,
        document_path: str,
        object_name: str,
        edge_refs: Sequence[int],
        result_name: str,
        distance: Optional[float] = None,
        distance1: Optional[float] = None,
        distance2: Optional[float] = None,
        result_label: Optional[str] = None,
        timeout_secs: float = 300,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.chamfer_edges",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "edge_refs": list(edge_refs),
                "distance": distance,
                "distance1": distance1,
                "distance2": distance2,
                "result_name": self._object_name(result_name),
                "result_label": result_label,
            },
            timeout_secs,
        )

    def linear_pattern(
        self,
        document_path: str,
        object_name: str,
        direction: Sequence[float],
        spacing: float,
        count: int,
        result_name: str,
        result_label: Optional[str] = None,
        timeout_secs: float = 300,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.linear_pattern",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "direction": list(direction),
                "spacing": spacing,
                "count": count,
                "result_name": self._object_name(result_name),
                "result_label": result_label,
            },
            timeout_secs,
        )

    def polar_pattern(
        self,
        document_path: str,
        object_name: str,
        axis: Sequence[float],
        count: int,
        result_name: str,
        angle_step_degrees: Optional[float] = None,
        total_angle_degrees: Optional[float] = None,
        center: Optional[Sequence[float]] = None,
        result_label: Optional[str] = None,
        timeout_secs: float = 300,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.polar_pattern",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "axis": list(axis),
                "center": list(center) if center is not None else None,
                "count": count,
                "angle_step_degrees": angle_step_degrees,
                "total_angle_degrees": total_angle_degrees,
                "result_name": self._object_name(result_name),
                "result_label": result_label,
            },
            timeout_secs,
        )

    def mirror_feature(
        self,
        document_path: str,
        object_name: str,
        plane: str,
        result_name: str,
        offset: float = 0,
        result_label: Optional[str] = None,
        timeout_secs: float = 300,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.mirror_feature",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "plane": plane,
                "offset": offset,
                "result_name": self._object_name(result_name),
                "result_label": result_label,
            },
            timeout_secs,
        )

    # -- recoverability -------------------------------------------------
    #
    # These four never spawn FreeCAD: an .FCStd is a self-contained archive, so
    # a snapshot is a byte copy and a restore is a byte replacement. See
    # snapshots.py for why that is the right call and for the error taxonomy.

    def create_snapshot(
        self,
        document_path: str,
        label: Optional[str] = None,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        """Copy a document's bytes into the store so a later call can restore them.

        The returned ``document_sha256`` is the identity the caller compares
        against later: it is both the optimistic-concurrency token for
        ``restore_snapshot`` and the proof that the snapshot matches the source
        at the moment it was taken.
        """
        document = self._document_path(document_path)
        deadline = time.monotonic() + self._timeout(timeout_secs)
        store = self._snapshot_store()
        snapshots, orphans = store.scan()
        store.ensure_capacity(snapshots, orphans, document.stat().st_size)
        entry = store.write(document, deadline=deadline, label=label)
        # Read-back: the published file must hold as many bytes as the copy
        # streamed. The two are independent -- ``entry["bytes"]`` is the copy's
        # own count, not a stat of the destination -- so a truncated write is
        # caught here rather than reported as a snapshot that restores short.
        # The full content hash is not recomputed: the copy already hashed the
        # exact bytes it wrote, and re-reading would double the I/O of the only
        # operation a caller might reasonably run before every mutation. A
        # restore proves content by hashing the document afterwards.
        stored = Path(entry["snapshot_path"])
        if not stored.is_file() or stored.stat().st_size != entry["bytes"]:
            store.discard(entry["snapshot_id"])
            raise verification_failure(
                "document.create_snapshot",
                "snapshot_bytes",
                entry["bytes"],
                stored.stat().st_size if stored.is_file() else None,
                "The incomplete snapshot was discarded. No document was modified; "
                "retry create_snapshot.",
            )
        result = dict(entry)
        result.update(
            {
                "document_path": str(document),
                "snapshot_store": str(store.directory),
                "snapshot_count": len(snapshots) + 1,
                "max_snapshots": store.max_snapshots,
                "max_snapshot_bytes": store.max_snapshot_bytes,
                "verified": ["snapshot_file_size", "snapshot_content_sha256"],
            }
        )
        return result

    def list_snapshots(self, document_path: Optional[str] = None) -> dict[str, Any]:
        """List restorable snapshots, together with the limits they count against.

        ``document_path`` is validated against allowed roots but deliberately
        *not* required to exist: the moment a user most needs this list is
        right after a document was damaged or deleted.
        """
        store = self._snapshot_store()
        snapshots, orphans = store.scan()
        if document_path is not None:
            wanted = Path(document_path).expanduser().resolve()
            if not _within(wanted, self.allowed_roots):
                raise BridgeError("Document is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
            snapshots = [item for item in snapshots if item.get("document_path") == str(wanted)]
        count, snapshot_bytes = store.usage(snapshots)
        _, total = store.usage(snapshots, orphans)
        return {
            "snapshot_store": str(store.directory),
            "snapshots": snapshots,
            "count": count,
            # Two byte figures on purpose: ``snapshot_bytes`` is the snapshots
            # themselves, ``total_bytes`` is what the byte limit is actually
            # charged against -- orphan bytes included. Reporting only the
            # former would show a total under the limit while the next call is
            # still refused for exceeding it.
            "snapshot_bytes": snapshot_bytes,
            "total_bytes": total,
            "max_snapshots": store.max_snapshots,
            "max_snapshot_bytes": store.max_snapshot_bytes,
            "orphans": {
                "count": len(orphans),
                "bytes": sum(item.stat().st_size for item in orphans),
                "files": [item.name for item in orphans][:50],
            },
        }

    def restore_snapshot(
        self,
        document_path: str,
        snapshot_id: str,
        expected_sha256: Optional[str] = None,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        """Replace a document with a snapshot, keeping the state it replaces.

        Three properties are enforced in this order, and every refusal leaves
        the document byte-for-byte untouched:

        1. ``expected_sha256`` must match the document as it stands now. A
           stale token means the user did new work after reading the document;
           overwriting it would destroy that work, so the call is refused with
           both hashes reported.
        2. The state being replaced is snapshotted first and its id returned as
           ``undo_snapshot_id``, so a mistaken restore is just another restore.
           Restore therefore needs capacity for one more snapshot, and refuses
           rather than perform a restore the caller could not undo.
        3. The document is re-read after replacement and must hash to the
           snapshot's recorded digest.

        Step 1 is checked twice -- once before the pre-restore copy and once
        after it. There is no cross-process file lock here, so the second check
        is what turns "we refused a stale restore" from a claim into a check:
        a writer that landed during the copy is detected instead of silently
        overwritten. A writer that lands in the final instant before
        ``os.replace`` is still lost; that residual window is inherent to a
        process-per-call adapter with no lock server, and is reported rather
        than pretended away.

        A missing document is accepted, not rejected. Recovering a deleted
        document is the case snapshots exist for, so it would be perverse to
        let ``list_snapshots`` find the snapshot and then refuse to apply it.
        That path skips the pre-restore snapshot -- there is no state to
        preserve -- and reports ``created_document: true`` with a null
        ``undo_snapshot_id``, so the caller knows this restore has no undo.
        """
        document = self._restorable_document_path(document_path)
        deadline = time.monotonic() + self._timeout(timeout_secs)
        store = self._snapshot_store()
        snapshot = store.read(snapshot_id)
        snapshot_sha = snapshot["document_sha256"]
        source = Path(snapshot["snapshot_path"])

        # Stage the snapshot bytes beside the document first: everything past
        # this point either commits them or leaves the document untouched.
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % document.stem, suffix=document.suffix, dir=str(document.parent)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        # A deleted document is the case snapshots exist for, so restoring onto
        # an absent path recreates it. There is then no state to preserve and
        # nothing to be undoable about: undo_snapshot_id comes back null.
        recreating = not document.exists()
        undo = None
        before_sha = None
        # One try/finally around the whole commit phase, because every refusal
        # below -- capacity, cancellation, an interrupted undo snapshot, a
        # concurrent write -- would otherwise leave the stage behind. The stage
        # sits in the user's document directory and holds a full copy of the
        # document, so leaking it there is not a cosmetic mess: repeated
        # refusals would fill the user's workspace with hidden copies.
        try:
            staged_sha, staged_bytes = copy_and_hash(source, staged, deadline)
            if staged_sha != snapshot_sha:
                raise SnapshotError(
                    ERROR_CONTENT_MISMATCH,
                    "Snapshot %s no longer holds the bytes it was created with "
                    "(recorded %s, read %s)" % (snapshot_id, snapshot_sha, staged_sha),
                    remediation=[
                        "Restore a different snapshot; this one cannot be trusted.",
                        "Call delete_snapshot to remove the damaged entry from %s."
                        % store.directory,
                    ],
                    snapshot_id=snapshot_id,
                    expected_sha256=snapshot_sha,
                    document_sha256=staged_sha,
                )

            if recreating:
                # A caller holding an expected hash believes bytes are there.
                # Finding the path empty is that same class of surprise and is
                # refused with the same code rather than silently recreated.
                if expected_sha256 is not None:
                    raise SnapshotError(
                        ERROR_CONFLICT,
                        "Document does not exist, but the caller passed expected_sha256 "
                        "%s; restore_snapshot refused to create it over that expectation"
                        % expected_sha256.lower(),
                        remediation=[
                            "Retry without expected_sha256 to recreate the deleted "
                            "document from this snapshot.",
                            "Or restore onto a different path to keep the expectation.",
                        ],
                        snapshot_id=snapshot_id,
                        expected_sha256=expected_sha256.lower(),
                        document_sha256=None,
                        conflict="document_missing",
                    )
            else:
                before_sha = sha256_file(document)
                if expected_sha256 is not None and not sha256_equal(expected_sha256, before_sha):
                    raise SnapshotError(
                        ERROR_CONFLICT,
                        "Document changed after the caller read it, so restore_snapshot "
                        "refused to overwrite it (expected %s, found %s)"
                        % (expected_sha256.lower(), before_sha),
                        remediation=[
                            "Re-read the document, then retry with its current document_sha256.",
                            "To discard the new state anyway, retry without expected_sha256.",
                            "To keep both, save_copy the current document before restoring.",
                        ],
                        snapshot_id=snapshot_id,
                        expected_sha256=expected_sha256.lower(),
                        document_sha256=before_sha,
                        conflict="expected_sha256",
                    )

            if not recreating:
                snapshots, orphans = store.scan()
                store.ensure_capacity(snapshots, orphans, document.stat().st_size)
                check_dcc_cancelled()
                undo = store.write(
                    document,
                    deadline=deadline,
                    label="Before restoring %s" % snapshot_id,
                    origin=PRE_RESTORE_ORIGIN,
                    restored_snapshot_id=snapshot_id,
                )
                confirm_sha = sha256_file(document)
                if confirm_sha != before_sha:
                    # The pre-restore copy was overtaken mid-flight, so it does
                    # not describe any single state of the document. Discard it
                    # and refuse: a restore with no trustworthy undo is worse
                    # than no restore.
                    store.discard(undo["snapshot_id"])
                    raise SnapshotError(
                        ERROR_CONFLICT,
                        "Document changed while its pre-restore snapshot was being "
                        "taken, so restore_snapshot replaced nothing (read %s, then %s)"
                        % (before_sha, confirm_sha),
                        remediation=[
                            "Retry restore_snapshot; nothing was changed.",
                            "If another process is writing the document, stop it and retry.",
                        ],
                        snapshot_id=snapshot_id,
                        expected_sha256=(
                            expected_sha256.lower() if expected_sha256 else before_sha
                        ),
                        document_sha256=confirm_sha,
                        conflict="concurrent_write",
                    )

            os.replace(str(staged), str(document))
        finally:
            # os.replace consumed the stage on the success path.
            if staged.exists():
                staged.unlink()
        after_sha = sha256_file(document)
        if after_sha != snapshot_sha:
            raise verification_failure(
                "document.restore_snapshot",
                "document_sha256",
                snapshot_sha,
                after_sha,
                "The document was replaced but does not match the snapshot. Re-run "
                "restore_snapshot, or restore %s to return to the pre-restore state."
                % (
                    undo["snapshot_id"]
                    if undo
                    else "(no undo snapshot: the document did not exist)"
                ),
            )
        return {
            "document_path": str(document),
            "bytes": staged_bytes,
            "document_bytes": document.stat().st_size,
            "document_sha256": after_sha,
            "restored_sha256": after_sha,
            "snapshot_id": snapshot_id,
            "replaced_document_sha256": before_sha,
            # Null only when the document had been deleted: there was no state
            # to preserve, so this restore has nothing to undo.
            "undo_snapshot_id": undo["snapshot_id"] if undo else None,
            "undo_snapshot_label": undo.get("label") if undo else None,
            "created_document": recreating,
            "snapshot_document_path": snapshot.get("document_path"),
            # Restoring a snapshot taken from a different document is allowed --
            # "make B look like A" is a real workflow -- but never silently.
            "cross_document": snapshot.get("document_path") != str(document),
            "snapshot_store": str(store.directory),
            "verified": [
                "snapshot_bytes_before_replace",
                "pre_restore_snapshot" if undo else "no_state_to_preserve",
                "document_sha256_after_restore",
            ],
        }

    def delete_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Free one slot. Without this, a full store would be a dead end."""
        store = self._snapshot_store()
        entry = store.delete(snapshot_id)
        snapshots, orphans = store.scan()
        count, total = store.usage(snapshots, orphans)
        return {
            "snapshot_id": entry["snapshot_id"],
            "deleted": True,
            "document_path": entry.get("document_path"),
            "document_sha256": entry.get("document_sha256"),
            "bytes": entry.get("bytes"),
            "snapshot_store": str(store.directory),
            "snapshot_count": count,
            "total_bytes": total,
            "max_snapshots": store.max_snapshots,
            "max_snapshot_bytes": store.max_snapshot_bytes,
        }

    def create_sketch(
        self,
        document_path: str,
        name: str,
        plane: str = "xy",
        body: Optional[str] = None,
        label: Optional[str] = None,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        if plane not in sketch_rules.PLANES:
            raise BridgeError("plane must be one of %s" % ", ".join(sketch_rules.PLANES))
        request = {
            "name": self._object_name(name),
            "plane": plane,
            "label": label,
        }
        if body is not None:
            request["body"] = self._object_name(body)
        return self._mutate_document("sketch.create", document_path, request, timeout_secs)

    def add_sketch_geometry(
        self,
        document_path: str,
        sketch_name: str,
        geometry: Sequence[Mapping[str, Any]],
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "sketch.add_geometry",
            document_path,
            {"sketch_name": self._object_name(sketch_name), "geometry": list(geometry)},
            timeout_secs,
        )

    def add_sketch_constraint(
        self,
        document_path: str,
        sketch_name: str,
        constraints: Sequence[Mapping[str, Any]],
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "sketch.add_constraint",
            document_path,
            {"sketch_name": self._object_name(sketch_name), "constraints": list(constraints)},
            timeout_secs,
        )

    def get_sketch_info(
        self,
        document_path: str,
        sketch_name: str,
        require_fully_constrained: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        return self._invoke(
            "sketch.info",
            {
                "document_path": str(document),
                "sketch_name": self._object_name(sketch_name),
                "require_fully_constrained": bool(require_fully_constrained),
            },
            timeout_secs,
        )


def get_bridge() -> FreecadBridge:
    return FreecadBridge.from_env()
