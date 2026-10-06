"""Post-write read-back contract for mutating tools.

The failure mode this module exists to eliminate is **"reported success, model
unchanged"**. In an agent loop that is the most expensive class of bug there is:
the caller receives an affirmative answer, keeps building on it, and the error
only surfaces steps later as an unrelated symptom.

The contract is one sentence:

    A mutating tool returns only after it has read the target back and proven
    that this call's change is actually there.

Anything else is a bug. Specifically, a mutating tool must never:

* return ``None`` because a parameter was not understood;
* return a ``valid: true`` summary computed from inputs instead of state;
* report success when part of the change applied.

Design notes for reusing this in another adapter
------------------------------------------------

Everything here is plain stdlib and knows nothing about FreeCAD, so another
adapter can copy this module unchanged and only supply:

1. ``MUTATING_TOOLS`` / ``READ_ONLY_TOOLS`` — the classification of its own
   method table. A tool that is in neither list fails the classification test,
   so adding a method forces the author to decide whether it owes a read-back.
2. A read-back helper per tool that calls :func:`numbers_match` /
   :func:`sequences_match` and raises :class:`WriteVerificationError`.

Two properties matter more than the exact checks:

* **Expected and actual are always both reported.** A mismatch that only says
  "failed" makes the caller guess; the pair is what makes it actionable.
* **The host version is always attached.** A read-back that disagrees is the
  classic signature of host API drift, and without the version the report is
  unreproducible.
"""

from __future__ import annotations

import math

SCHEMA_VERSION = 1

# Floating point read-back tolerance. FreeCAD stores lengths in millimetres as
# doubles and round-trips them verbatim, so the tolerance exists to absorb
# unit/serialisation noise, not to excuse a real difference. It is deliberately
# far tighter than any modelling-relevant delta.
DEFAULT_REL_TOLERANCE = 1e-6
DEFAULT_ABS_TOLERANCE = 1e-9

# Tools that change a document or write a file. Every entry owes a read-back.
MUTATING_TOOLS = (
    "document.create",
    "document.save_copy",
    "document.remove_object",
    "model.add_primitive",
    "model.update_primitive",
    "model.transform_object",
    "model.boolean_operation",
    "model.import_geometry",
    "model.export_geometry",
    "model.fillet_edges",
    "model.chamfer_edges",
    "model.linear_pattern",
    "model.polar_pattern",
    "model.mirror_feature",
)

# Tools that observe state and change nothing. Kept here so the classification
# test can prove no method is left unclassified.
READ_ONLY_TOOLS = (
    "system.status",
    "document.inspect",
    "document.validate",
)

TOOL_CLASSIFICATION_ERROR = (
    "every driver method must be listed in write_contract.MUTATING_TOOLS or "
    "write_contract.READ_ONLY_TOOLS; an unclassified method has no answer to "
    "'does this owe a post-write read-back?'"
)


def jsonable(value):
    """Coerce ``value`` into something ``json.dump`` accepts.

    Read-back evidence crosses a process boundary, so anything that cannot be
    represented in JSON is rendered as text rather than dropped: a dropped
    field is how a report ends up saying "expected something, got something".
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not valid JSON; keep them visible as text.
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return repr(value)


def _describe(value):
    """Render one side of an expected/actual pair compactly."""
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(_describe(item) for item in value)
    if isinstance(value, dict):
        return "{%s}" % ", ".join("%s: %s" % (key, _describe(item)) for key, item in value.items())
    if isinstance(value, float):
        return repr(value)
    return str(value)


def numbers_match(expected, actual, rel_tolerance=None, abs_tolerance=None):
    """Compare two scalars with the contract's default tolerance."""
    try:
        return math.isclose(
            float(expected),
            float(actual),
            rel_tol=DEFAULT_REL_TOLERANCE if rel_tolerance is None else rel_tolerance,
            abs_tol=DEFAULT_ABS_TOLERANCE if abs_tolerance is None else abs_tolerance,
        )
    except (TypeError, ValueError):
        return False


def sequences_match(expected, actual, rel_tolerance=None, abs_tolerance=None):
    """Compare two numeric sequences element by element.

    A length mismatch is a mismatch, not a truncated comparison: reporting
    three coordinates against two would hide the difference.
    """
    try:
        expected = [float(item) for item in expected]
        actual = [float(item) for item in actual]
    except (TypeError, ValueError):
        return False
    if len(expected) != len(actual):
        return False
    return all(
        numbers_match(item, other, rel_tolerance, abs_tolerance)
        for item, other in zip(expected, actual)
    )


def format_message(payload):
    """Render the human- and agent-readable sentence for a mismatch.

    Deliberately states the tool, the check, both values, and the host version
    in that order: the reader should never have to re-run the call to find out
    what differed.
    """
    tool = payload.get("tool") or "unknown tool"
    check = payload.get("check") or "unknown check"
    message = (
        "%s did not take effect: the post-write read-back disagreed on %s "
        "(expected %s, read back %s)"
        % (
            tool,
            check,
            _describe(payload.get("expected")),
            _describe(payload.get("actual")),
        )
    )
    version = payload.get("host_version")
    if version:
        message += "; host FreeCAD %s" % version
    matrix = payload.get("host_matrix") or {}
    if matrix.get("status"):
        message += " (matrix status: %s)" % matrix["status"]
    remediation = payload.get("remediation")
    if remediation:
        message += ". %s" % remediation
    return message


class WriteVerificationError(RuntimeError):
    """A mutating tool reported success but the read-back disagreed.

    The structured :attr:`payload` travels with the exception so the boundary
    that serialises the error (the driver's ``main``) can forward it verbatim
    and the caller can branch on ``check``/``expected``/``actual`` instead of
    parsing prose.
    """

    def __init__(
        self,
        tool,
        check,
        expected=None,
        actual=None,
        host_version=None,
        host_matrix=None,
        params=None,
        remediation=None,
        message=None,
    ):
        self.payload = {
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "check": check,
            "expected": jsonable(expected),
            "actual": jsonable(actual),
            "host_version": host_version,
            "host_matrix": jsonable(host_matrix),
            "params": jsonable(params),
            "remediation": remediation,
        }
        super().__init__(message or format_message(self.payload))

    # Field accessors, so a caller can branch on the structured mismatch
    # without reaching into the payload dictionary.
    @property
    def tool(self):
        return self.payload.get("tool")

    @property
    def check(self):
        return self.payload.get("check")

    @property
    def expected(self):
        return self.payload.get("expected")

    @property
    def actual(self):
        return self.payload.get("actual")

    @property
    def host_version(self):
        return self.payload.get("host_version")

    @classmethod
    def from_payload(cls, payload):
        """Rebuild the error on the caller's side of a process boundary."""
        return cls(
            tool=payload.get("tool"),
            check=payload.get("check"),
            expected=payload.get("expected"),
            actual=payload.get("actual"),
            host_version=payload.get("host_version"),
            host_matrix=payload.get("host_matrix"),
            params=payload.get("params"),
            remediation=payload.get("remediation"),
            message=format_message(payload),
        )
