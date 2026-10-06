"""Machine-readable FreeCAD host compatibility matrix.

The matrix itself lives in ``compat_matrix.json`` next to this module so that
the service-side checks (this module) and the checks that run inside FreeCAD's
own interpreter (``freecad_driver.py``) read one single source of truth.

A host version that is not covered by the matrix is reported as unsupported
with the covered ranges and a concrete remediation. Nothing here degrades
silently: an unrecognised or out-of-range version never becomes "good enough".
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

MATRIX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compat_matrix.json")

SUPPORTED = "supported"
TOO_OLD = "too_old"
TOO_NEW = "too_new"
UNLISTED = "unlisted"
UNKNOWN = "unknown"

STATUS_MESSAGES = {
    SUPPORTED: "supported",
    TOO_OLD: "below the supported range",
    TOO_NEW: "above the supported range",
    UNLISTED: "inside the covered span but not in any declared range",
    UNKNOWN: "not recognised as a FreeCAD version",
}

_RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

Version = Tuple[int, int, int]


def parse_version(value: str) -> Optional[Version]:
    """Parse the leading ``major.minor[.patch]`` of a FreeCAD version string."""
    if not value:
        return None
    match = _RELEASE.match(str(value).strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def format_version(version: Version) -> str:
    return "%d.%d.%d" % version


def load_matrix(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or MATRIX_PATH, "r", encoding="utf-8") as stream:
        return json.load(stream)


def supported_ranges(matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return list((matrix or load_matrix()).get("supported_ranges") or ())


def supported_range_labels(matrix: Optional[Dict[str, Any]] = None) -> List[str]:
    labels = []
    for entry in supported_ranges(matrix):
        minimum = parse_version(str(entry.get("min_version", "")))
        maximum = parse_version(str(entry.get("max_version", "")))
        if minimum is None or maximum is None:
            continue
        labels.append("%d.%d.x" % (minimum[0], minimum[1]))
    return labels


def breaking_changes_for(
    version: str, matrix: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Return the declared host API breaks that apply to ``version``."""
    matrix = matrix or load_matrix()
    parsed = parse_version(version)
    if parsed is None:
        return []
    applied = []
    for entry in matrix.get("breaking_changes") or ():
        since = parse_version(str(entry.get("applies_from", "")))
        if since is None or parsed < since:
            continue
        applied.append(entry)
    return applied


def classify_host(version: str, matrix: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Classify a discovered FreeCAD version against the matrix.

    The result is machine readable and is embedded verbatim in the doctor and
    verify reports, so callers can gate on ``status`` instead of parsing prose.
    """
    matrix = matrix or load_matrix()
    parsed = parse_version(version)
    verdict: Dict[str, Any] = {
        "version": version,
        "parsed_version": format_version(parsed) if parsed is not None else None,
        "status": UNKNOWN,
        "matrix_version": matrix.get("matrix_version"),
        "supported_ranges": supported_range_labels(matrix),
        "range": None,
        "breaking_changes": [
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "changed_in": entry.get("changed_in"),
                "kind": entry.get("kind"),
                "adapter_usage": entry.get("adapter_usage"),
                "enforcement": entry.get("enforcement"),
                "remediation": entry.get("remediation"),
                "replacement": entry.get("replacement"),
            }
            for entry in breaking_changes_for(version, matrix)
        ],
    }
    if parsed is None:
        return verdict

    for entry in supported_ranges(matrix):
        minimum = parse_version(str(entry.get("min_version", "")))
        maximum = parse_version(str(entry.get("max_version", "")))
        if minimum is None or maximum is None:
            continue
        if minimum <= parsed <= maximum:
            verdict["status"] = SUPPORTED
            verdict["range"] = {
                "id": entry.get("id"),
                "min_version": entry.get("min_version"),
                "max_version": entry.get("max_version"),
                "evidence": entry.get("evidence"),
            }
            return verdict

    minimums = [
        parse_version(str(entry.get("min_version", ""))) for entry in supported_ranges(matrix)
    ]
    maximums = [
        parse_version(str(entry.get("max_version", ""))) for entry in supported_ranges(matrix)
    ]
    low = min([item for item in minimums if item is not None], default=None)
    high = max([item for item in maximums if item is not None], default=None)
    if low is not None and parsed < low:
        status = TOO_OLD
    elif high is not None and parsed > high:
        status = TOO_NEW
    else:
        # Inside the covered span but in a gap between declared ranges (for
        # example 1.0.x and 1.2.x with 1.1.x unverified). That is still outside
        # the matrix and must not be treated as supported.
        status = UNLISTED
    verdict["status"] = status
    return verdict


def is_supported(version: str, matrix: Optional[Dict[str, Any]] = None) -> bool:
    return classify_host(version, matrix)["status"] == SUPPORTED


def unsupported_reason(verdict: Dict[str, Any]) -> str:
    """Build the human- and agent-readable rejection sentence for a verdict."""
    ranges = verdict.get("supported_ranges") or ()
    covered = ", ".join(ranges) if ranges else "no declared range"
    version = verdict.get("version") or "unknown"
    status = verdict.get("status")
    if status == UNKNOWN:
        return "FreeCAD reported an unrecognised version %r; supported range: %s" % (
            version,
            covered,
        )
    if status == TOO_NEW:
        return (
            "FreeCAD %s is newer than the verified compatibility matrix (supported: %s); "
            "the host API may have moved, so the adapter refuses to run unverified"
            % (version, covered)
        )
    if status == UNLISTED:
        return (
            "FreeCAD %s is not listed in the verified compatibility matrix (supported: %s); "
            "the adapter refuses to run unverified" % (version, covered)
        )
    return "FreeCAD %s is unsupported; supported range: %s" % (version, covered)
