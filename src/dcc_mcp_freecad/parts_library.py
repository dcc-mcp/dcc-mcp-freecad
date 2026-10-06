"""Offline standard-parts library: listing and containment-checked resolution.

Why this module exists
----------------------

The comparable FreeCAD MCP project shipped an ``insert_part_from_library`` tool
whose ``relative_path`` was joined onto the library root with no containment
check (their issue #121). That is the same bug class as an unrestricted
``input_path``: any caller could reach a file outside the library by asking for
``../../<anything>``. This adapter already refuses paths outside
``DCC_MCP_FREECAD_ALLOWED_ROOTS`` for every other tool, and the entire point of
this module is that adding a parts library does not reopen that hole.

The rule is one sentence:

    A ``part_ref`` is a POSIX-relative path with no traversal segment and no
    absolute prefix, and it is only ever resolved to a file proven to live
    under one of the configured library roots.

Anything else is refused with a stable error code before FreeCAD is started.

Why it is offline-only
----------------------

The library is a set of local directories the operator configures. Nothing here
downloads, extracts or caches anything, and nothing is written outside the
directories the operator named: a remote source would turn a modelling call
into a network fetch whose result the caller cannot audit, and a cache in the
user home would leave binaries the user never asked for. A URL-shaped root is
therefore refused outright rather than silently fetched.

Design notes for reusing this in another adapter
------------------------------------------------

Everything here is plain stdlib and knows nothing about FreeCAD, so another
adapter can copy this module unchanged and only supply its own suffix set. Two
properties matter more than the exact layout convention:

* **Rejection precedes resolution.** ``..``, absolute paths and URL schemes are
  refused from the string itself, so the check never depends on how the host
  platform happens to normalise a path.
* **Containment is proven, not assumed.** The final file is compared against
  the root after ``realpath``, so a symlink that points out of the library is
  caught even though the textual path looked harmless.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

#: Operator-configured, ``os.pathsep``-separated local library directories.
ENV_LIBRARY_ROOTS = "DCC_MCP_FREECAD_PARTS_LIBRARY"

#: The same bounded exchange set ``import_geometry`` accepts, so a library
#: entry is never a format the adapter cannot already prove it read back.
PART_SUFFIXES = frozenset({".brep", ".brp", ".iges", ".igs", ".obj", ".step", ".stl", ".stp"})

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: Bound on the directory walk. A standard-parts library on a shared volume can
#: hold far more files than one response should ever carry; the scan stops here
#: and says so instead of walking to the end.
MAX_WALK_FILES = 20_000

# Stable error codes. Callers branch on these instead of parsing prose.
LIBRARY_UNAVAILABLE = "parts_library_unavailable"
ROOT_MISSING = "parts_library_root_missing"
REMOTE_UNSUPPORTED = "remote_library_unsupported"
INVALID_PART_REF = "invalid_part_ref"
ESCAPES_LIBRARY = "part_ref_escapes_library"
UNSUPPORTED_FORMAT = "unsupported_part_format"
PART_NOT_FOUND = "part_not_found"
INVALID_LIMIT = "invalid_limit"
INVALID_FILTER = "invalid_filter"

_REMOTE_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class PartLibraryError(RuntimeError):
    """A bounded parts-library rejection with a machine-readable code.

    Raised instead of a plain error so a caller can branch on :attr:`code`
    instead of matching a sentence. It is deliberately *not* a
    :class:`~dcc_mcp_freecad.bridge.BridgeError`: a parts-library rejection
    happens before any FreeCAD process is started, and a caller that catches
    ``BridgeError`` to mean "the host failed" must not silently absorb "the
    reference was refused".
    """

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = dict(details)

    def payload(self) -> Dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "error_code": self.code,
            "message": str(self),
        }
        payload.update(self.details)
        return payload

    def context(self) -> Dict[str, Any]:
        """The structured context to attach to a result envelope.

        Deliberately without ``message``: the caller passes the message as the
        envelope's own ``message`` argument, and a second copy under ``context``
        collides with that keyword instead of adding information.
        """
        context = {"error_code": self.code}
        context.update(self.details)
        return context


def _realpath(value: str) -> str:
    """Collapse ``value`` to its real path, following symlinks.

    Uses :meth:`pathlib.Path.resolve` rather than :func:`os.path.realpath`
    because on Windows the latter is only an alias for :func:`os.path.abspath`
    until Python 3.8 -- CPython 3.7's ``ntpath`` has no real ``realpath``, so it
    leaves a symlink pointing out of the library unresolved and the containment
    check passes on the link's own path. ``Path.resolve()`` goes through
    ``_getfinalpathname`` on 3.7 and behaves the same as ``realpath`` on POSIX
    and on later Windows versions.

    A module-level indirection so the symlink-escape test can simulate a link
    pointing out of the library on platforms where creating one needs
    privileges the test runner does not have.
    """
    return str(Path(value).resolve())


def _contains(root: str, candidate: str) -> bool:
    """True when ``candidate`` is ``root`` or lies under it.

    Compared on normalised absolute paths, because ``commonpath`` raises on
    mixed drive letters instead of answering the question.
    """
    root_value = os.path.normcase(os.path.abspath(root))
    target = os.path.normcase(os.path.abspath(candidate))
    if root_value == target:
        return True
    try:
        return os.path.commonpath((root_value, target)) == root_value
    except ValueError:
        return False


def _split_roots(value: Optional[str]) -> List[str]:
    """Split an ``os.pathsep``-joined root list without cutting URLs in half.

    POSIX separates entries with ``:``, which is also the character that
    introduces a URL scheme, so a naive split turns ``https://example.com``
    into ``https`` and ``//example.com``. Neither fragment matches the scheme
    pattern, so a remote root was reported as a missing local directory instead
    of being refused as remote. The URL alternative is matched first so a
    remote entry survives the split intact and is refused with the right code.
    """
    separator = re.escape(os.pathsep)
    scheme = r"[A-Za-z][A-Za-z0-9+.\-]*://"
    drive = r"[A-Za-z]:[\\/]"
    # A URL root is one token so ':' (the POSIX separator) does not cut
    # 'https://example.com' into two scheme-less fragments, and a Windows drive
    # root keeps its own ':' because a drive letter is not a URL scheme.
    url = scheme + r"[^" + separator + r"]*"
    rooted = drive + r"[^" + separator + r"]*"
    plain = r"(?!" + scheme + r")(?!" + drive + r")[^" + separator + r"]+"
    pattern = re.compile(url + r"|" + rooted + r"|" + plain)
    return [item.strip() for item in pattern.findall(str(value or "")) if item.strip()]


def _unique_roots(roots: Sequence[Path]) -> List[Path]:
    seen = set()
    ordered = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            ordered.append(root)
    return ordered


def parse_library_roots(value: Optional[str]) -> List[Path]:
    """Resolve a configured root list, refusing anything that is not local.

    A URL-shaped entry is refused with :data:`REMOTE_UNSUPPORTED` rather than
    fetched: making it a network call would put an unauditable download behind
    a modelling tool and leave binaries the operator never reviewed.
    """
    entries = _split_roots(value)
    if not entries:
        raise PartLibraryError(
            LIBRARY_UNAVAILABLE,
            "No parts library is configured; set %s to one or more local directories "
            "(%s-separated) that already hold the standard parts. The adapter is offline "
            "only and never downloads a library." % (ENV_LIBRARY_ROOTS, os.pathsep),
            env_var=ENV_LIBRARY_ROOTS,
        )
    roots = []
    for entry in entries:
        scheme = _REMOTE_SCHEME.match(entry)
        if scheme is not None:
            raise PartLibraryError(
                REMOTE_UNSUPPORTED,
                "The parts library is offline-only; %r is a remote source (%s). Point %s at "
                "a local directory that already holds the parts; the adapter never downloads "
                "or caches external binaries." % (entry, scheme.group(0), ENV_LIBRARY_ROOTS),
                entry=entry,
                scheme=scheme.group(0),
            )
        root = Path(entry).expanduser().resolve()
        if not root.is_dir():
            raise PartLibraryError(
                ROOT_MISSING,
                "Parts library root is not an existing directory: %s" % root,
                entry=entry,
                root=str(root),
            )
        roots.append(root)
    return _unique_roots(roots)


def resolve_library_roots(env: Optional[Any] = None) -> List[Path]:
    """Read the configured library roots from ``env`` (default ``os.environ``)."""
    environ = os.environ if env is None else env
    getter = environ.get if hasattr(environ, "get") else None
    value = getter(ENV_LIBRARY_ROOTS, "") if getter else ""
    return parse_library_roots(value)


def _is_hidden(name: str) -> bool:
    return name.startswith(".") or name == "__pycache__"


def _scan(
    roots: Sequence[Path], max_files: Optional[int] = None
) -> Tuple[List[Tuple[Path, Path]], bool]:
    """Walk every root and collect ``(root, path)`` pairs for part files.

    Symlinked directories are not followed (``os.walk`` default): a link inside
    the library is exactly how a scan would start reporting files that are not
    in the library at all. The walk is bounded and reports when it stopped.
    """
    # Read the bound at call time so it stays a module-level knob.
    if max_files is None:
        max_files = MAX_WALK_FILES
    entries: List[Tuple[Path, Path]] = []
    seen: set = set()
    examined = 0
    limited = False
    for root in roots:
        for current, directories, files in os.walk(str(root)):
            directories[:] = sorted(name for name in directories if not _is_hidden(name))
            for name in sorted(files):
                if _is_hidden(name):
                    continue
                examined += 1
                if examined > max_files:
                    limited = True
                    break
                path = Path(current) / name
                if path.suffix.lower() not in PART_SUFFIXES:
                    continue
                key = os.path.normcase(str(path.resolve()))
                if key in seen:
                    continue
                seen.add(key)
                entries.append((root, path))
            if limited:
                break
        if limited:
            break
    return entries, limited


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _category_of(relative: str) -> str:
    """The top-level directory of a relative path, or ``""`` at a root."""
    segments = relative.split("/")
    return segments[0] if len(segments) > 1 else ""


def _matches_category(relative: str, category: str) -> bool:
    """Match a category against a part's directory, not just its first segment.

    ``category="Fasteners"`` selects ``Fasteners/Bolts/Hex/M8.step`` as well as
    ``Fasteners/M8.step``: the top-level grouping is how these libraries are
    browsed, and requiring the caller to know the full depth would make the
    filter useless for discovery.
    """
    wanted = category.strip().strip("/").lower()
    if not wanted:
        return True
    directory = "/".join(relative.split("/")[:-1]).lower()
    return directory == wanted or directory.startswith(wanted + "/")


def _coerce_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    limit = value
    # A JSON number can arrive as 50.0; a bool is never a limit.
    if isinstance(limit, bool):
        limit = None
    elif isinstance(limit, float) and float(limit).is_integer():
        limit = int(limit)
    if not isinstance(limit, int):
        raise PartLibraryError(
            INVALID_LIMIT,
            "limit must be an integer from 1 through %d" % MAX_LIMIT,
            limit=value,
        )
    if not 1 <= limit <= MAX_LIMIT:
        raise PartLibraryError(
            INVALID_LIMIT,
            "limit must be an integer from 1 through %d" % MAX_LIMIT,
            limit=value,
        )
    return limit


def _coerce_filter(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PartLibraryError(
            INVALID_FILTER, "%s must be a string" % name, field=name, value=value
        )
    return value.strip()


def list_parts(
    category: Optional[str] = None,
    query: Optional[str] = None,
    limit: Any = DEFAULT_LIMIT,
    roots: Optional[Sequence[Path]] = None,
    env: Optional[Any] = None,
) -> Dict[str, Any]:
    """List the configured library's parts as bounded, ref-safe entries.

    Every returned ``path`` is a ``part_ref``: the POSIX-relative string
    :func:`resolve_part_ref` accepts, and nothing else.
    """
    roots = list(roots) if roots is not None else resolve_library_roots(env)
    limit = _coerce_limit(limit)
    category = _coerce_filter(category, "category")
    query = _coerce_filter(query, "query")

    entries, scan_limited = _scan(roots)
    categories = sorted({_category_of(_relative_posix(path, root)) for root, path in entries})

    matched = []
    for root, path in entries:
        relative = _relative_posix(path, root)
        if not _matches_category(relative, category):
            continue
        if query and query.lower() not in relative.lower():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            # A file that vanished mid-scan is not a part the caller can use.
            continue
        matched.append(
            {
                "name": path.stem,
                "category": _category_of(relative),
                "path": relative,
                "format": path.suffix.lower().lstrip("."),
                "size_bytes": size,
                "root": str(root),
            }
        )

    # One stable order across every root, so a bounded page is the same page
    # twice in a row: without this, the root directory's own files would always
    # come before everything nested under it.
    matched.sort(key=lambda part: (part["path"], part["root"]))
    total = len(matched)
    return {
        "schema_version": SCHEMA_VERSION,
        "offline_only": True,
        "remote_sources": False,
        "roots": [str(root) for root in roots],
        "categories": categories,
        "filters": {"category": category, "query": query, "limit": limit},
        "total_matched": total,
        "returned": min(total, limit),
        "truncated": total > limit,
        "scan_limited": scan_limited,
        "parts": matched[:limit],
    }


def resolve_part_ref(part_ref: Any, roots: Sequence[Path]) -> Tuple[Path, Path]:
    """Resolve a ``part_ref`` to a file proven to live under a library root.

    Returns ``(path, root)``. Every rejection happens before a file is opened
    and before FreeCAD is started, and each one carries a stable
    :class:`PartLibraryError` code.
    """
    if not isinstance(part_ref, str) or not part_ref.strip():
        raise PartLibraryError(
            INVALID_PART_REF,
            "part_ref must be a non-empty relative path taken from list_parts",
            part_ref=part_ref,
        )
    reference = part_ref.strip()
    scheme = _REMOTE_SCHEME.match(reference)
    if scheme is not None or reference.startswith("//"):
        raise PartLibraryError(
            REMOTE_UNSUPPORTED,
            "The parts library is offline-only; %r is a remote source. Pass a relative "
            "path from list_parts instead." % part_ref,
            part_ref=part_ref,
            scheme=scheme.group(0) if scheme else "//",
        )
    if "\x00" in reference:
        raise PartLibraryError(
            INVALID_PART_REF, "part_ref may not contain a null byte", part_ref=part_ref
        )
    # Absolute paths of every platform spelling, including UNC and drive roots.
    if reference.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(reference):
        raise PartLibraryError(
            INVALID_PART_REF,
            "part_ref must be relative to a library root, not an absolute path: %r" % part_ref,
            part_ref=part_ref,
        )
    if "\\" in reference:
        raise PartLibraryError(
            INVALID_PART_REF,
            "part_ref uses '/' as its separator; a backslash is not accepted: %r" % part_ref,
            part_ref=part_ref,
        )
    segments = reference.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise PartLibraryError(
            INVALID_PART_REF,
            "part_ref may not contain an empty, '.' or '..' segment: %r" % part_ref,
            part_ref=part_ref,
        )
    suffix = Path(reference).suffix.lower()
    if suffix not in PART_SUFFIXES:
        raise PartLibraryError(
            UNSUPPORTED_FORMAT,
            "Unsupported part format %r; supported formats: %s"
            % (suffix or reference.rsplit(".", 1)[-1], ", ".join(sorted(PART_SUFFIXES))),
            part_ref=part_ref,
            suffix=suffix,
        )

    escaped = []
    for root in roots:
        root_value = str(root)
        candidate = os.path.normpath(os.path.join(root_value, *segments))
        if not _contains(root_value, candidate):
            escaped.append(candidate)
            continue
        # The textual path is inside the root; prove the file is too, so a
        # symlink that leaves the library is refused rather than followed.
        resolved = _realpath(candidate)
        if not _contains(root_value, resolved):
            escaped.append(resolved)
            continue
        if os.path.isfile(resolved):
            return Path(resolved), root

    if escaped:
        raise PartLibraryError(
            ESCAPES_LIBRARY,
            "part_ref resolves outside every configured parts library root and was refused "
            "instead of being read: %r" % part_ref,
            part_ref=part_ref,
            roots=[str(root) for root in roots],
            resolved=sorted({os.path.normcase(item) for item in escaped}),
        )
    raise PartLibraryError(
        PART_NOT_FOUND,
        "No such part in the configured parts library: %r" % part_ref,
        part_ref=part_ref,
        roots=[str(root) for root in roots],
    )
