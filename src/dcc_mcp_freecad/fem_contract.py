"""Typed FEM analysis contract: units, references, and structured solver errors.

This module exists because the dominant failure mode of a scripted FEM tool is
**a right-looking number in the wrong unit**. The reference implementation this
adapter is measured against silently applies ``ConstraintForce.Force`` a factor
of 1000 off (N read as kN): the solver converges, the stress field looks
plausible, and the answer is wrong by three orders of magnitude. Nothing in a
bare float can catch that.

The contract is one sentence:

    Every quantity crossing this boundary carries its unit, is converted
    through a declared table, and is read back in the solver's own unit before
    the result is returned.

FreeCAD's FEM workbench writes its solver input in a configurable unit schema
(mm-N-s by default, SI or imperial on other installations). A result of
``0.19`` is therefore meaningless without knowing the schema that produced it.
Rather than trusting the preference, this module identifies the schema from the
generated solver input itself: node coordinates are compared against the
target's measured bounding box, and the schema is only accepted when exactly one
length unit explains the observed coordinates.

Design notes for reusing this in another adapter
------------------------------------------------

Everything here is plain stdlib and knows nothing about FreeCAD, so another
adapter can copy this module unchanged. The FreeCAD-specific half lives in
``freecad_driver.py``, which calls in for:

* :func:`to_canonical` / :func:`quantity_payload` — unit-carrying conversions;
* :func:`match_length_scale` — schema detection from solver input;
* :func:`parse_reference` — sub-element reference parsing;
* :class:`FemError` — the structured failure vocabulary.

``jsonable`` is duplicated from ``write_contract`` on purpose: the driver runs
inside FreeCAD's interpreter, where the adapter package is not importable and
sibling modules are loaded by path, so a cross-import would break the very
boundary this module is designed to cross.
"""

from __future__ import annotations

import math
import re

SCHEMA_VERSION = 1

# Canonical unit per dimension. Anything reported by this adapter is converted
# to these before it leaves the driver, so a caller never sees a bare float.
CANONICAL_UNITS = {
    "force": "N",
    "length": "mm",
    "stress": "MPa",
    "density": "t/mm^3",
}

# Conversion factor to the canonical unit: `canonical = value * factor`.
_UNIT_TABLES = {
    "force": {
        "N": 1.0,
        "mN": 1.0e-3,
        "kN": 1.0e3,
        "MN": 1.0e6,
        "GN": 1.0e9,
        "kgf": 9.80665,
        "gf": 9.80665e-3,
        "tf": 9806.65,
        "lbf": 4.4482216152605,
        "dyn": 1.0e-5,
    },
    "length": {
        "mm": 1.0,
        "um": 1.0e-3,
        "nm": 1.0e-6,
        "cm": 10.0,
        "m": 1000.0,
        "km": 1.0e6,
        "in": 25.4,
        "ft": 304.8,
        "mil": 0.0254,
    },
    "stress": {
        "Pa": 1.0e-6,
        "kPa": 1.0e-3,
        "MPa": 1.0,
        "GPa": 1.0e3,
        "bar": 0.1,
        "psi": 0.006894757293168361,
        "ksi": 6.894757293168361,
        "N/mm^2": 1.0,
        "N/mm2": 1.0,
        "N/m^2": 1.0e-6,
        "N/m2": 1.0e-6,
        "kN/mm^2": 1.0e3,
        "kN/mm2": 1.0e3,
    },
    "density": {
        "t/mm^3": 1.0,
        "kg/mm^3": 1.0e-3,
        "g/mm^3": 1.0e-6,
        "kg/m^3": 1.0e-12,
        "t/m^3": 1.0e-9,
        "g/cm^3": 1.0e-9,
        "kg/cm^3": 1.0e-6,
    },
}

# A solver input's length unit identifies the rest of its schema: CalculiX is
# unit-agnostic and echoes whatever consistent set it was given, so the force
# and stress units follow from the length unit. A length unit with no declared
# schema is refused rather than guessed.
SCHEMA_BY_LENGTH_UNIT = {
    "mm": ("N", "MPa"),
    "m": ("N", "Pa"),
    "in": ("lbf", "psi"),
}

# The material used when the caller does not name one. A silent default is the
# same bug class as a silent unit, so the resolved material is always echoed
# back in the result.
DEFAULT_MATERIAL = {
    "name": "DccMcpStructuralSteel",
    "youngs_modulus": {"value": 210000.0, "unit": "MPa"},
    "poisson_ratio": 0.3,
    "density": {"value": 7.85e-09, "unit": "t/mm^3"},
}

MAX_POISSON_RATIO = 0.499999

_REFERENCE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,63}):((?:Face|Edge|Vertex)\d+)$")


class UnitError(ValueError):
    """A quantity was supplied without a unit, with an unknown unit, or out of range."""


class FemError(RuntimeError):
    """A structured FEM failure safe to return to a local caller.

    The failure is identified by :attr:`error_code` rather than prose, because
    every code here has a different remedy: a missing solver, a timed-out run,
    and a unit that could not be verified need three different next actions from
    the agent, and re-reading a sentence to decide which is how a caller ends up
    retrying a permanent failure.
    """

    ERROR_SOLVER_MISSING = "fem_solver_missing"
    ERROR_MESHER_MISSING = "fem_mesher_missing"
    ERROR_WORKBENCH_MISSING = "fem_workbench_missing"
    ERROR_HOST_LIMITED = "fem_host_limited"
    ERROR_NO_ANALYSIS = "fem_analysis_not_found"
    ERROR_INVALID_REFERENCE = "fem_invalid_reference"
    ERROR_INVALID_MATERIAL = "fem_invalid_material"
    ERROR_INVALID_LOAD = "fem_invalid_load"
    ERROR_INVALID_MESH_SIZE = "fem_invalid_mesh_size"
    ERROR_PREREQUISITES = "fem_prerequisites_failed"
    ERROR_SOLVER_API = "fem_solver_api_missing"
    ERROR_WRITE_FAILED = "fem_input_write_failed"
    ERROR_SOLVER_FAILED = "fem_solver_failed"
    ERROR_SOLVER_TIMEOUT = "fem_solver_timeout"
    ERROR_RESULTS_MISSING = "fem_results_missing"
    ERROR_UNIT_SCHEMA_UNKNOWN = "fem_unit_schema_unverified"
    ERROR_UNIT_READBACK = "fem_unit_readback_mismatch"

    def __init__(
        self,
        error_code,
        message,
        remediation=None,
        details=None,
        host_version=None,
        params=None,
    ):
        self.payload = {
            "schema_version": SCHEMA_VERSION,
            "error_code": error_code,
            "message": message,
            "remediation": remediation,
            "details": jsonable(details),
            "host_version": host_version,
            "params": jsonable(params),
        }
        super().__init__(format_message(self.payload))

    @property
    def error_code(self):
        return self.payload.get("error_code")

    @property
    def remediation(self):
        return self.payload.get("remediation")

    @property
    def details(self):
        return self.payload.get("details")

    @classmethod
    def from_payload(cls, payload):
        """Rebuild the error on the caller's side of a process boundary."""
        return cls(
            error_code=payload.get("error_code"),
            message=payload.get("message") or "FEM analysis failed",
            remediation=payload.get("remediation"),
            details=payload.get("details"),
            host_version=payload.get("host_version"),
            params=payload.get("params"),
        )


def jsonable(value):
    """Coerce ``value`` into something ``json.dump`` accepts.

    Structured evidence crosses a process boundary, so anything that cannot be
    represented in JSON is rendered as text rather than dropped: a dropped field
    is how a report ends up saying "expected something, got something".
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


_SOLVER_OUTPUT_KEYS = (
    "solver_workdir",
    "ccx_stderr_from_disk",
    "ccx_stdout_from_disk",
    "stderr_tail",
    "stdout_tail",
)


def format_message(payload):
    """Render the human- and agent-readable sentence for a structured failure.

    Solver output is appended to the message as well as kept in the details.
    The details are what a program branches on, but a log line is often the only
    thing a human ever sees -- and a failure that reports "no results" without
    the solver's own output cannot be acted on without re-running the analysis.
    """
    message = "%s: %s" % (payload.get("error_code") or "fem_error", payload.get("message") or "")
    if payload.get("host_version"):
        message += "; host FreeCAD %s" % payload["host_version"]
    remediation = payload.get("remediation")
    if remediation:
        message += ". %s" % remediation
    details = payload.get("details")
    if isinstance(details, dict):
        for key in _SOLVER_OUTPUT_KEYS:
            value = details.get(key)
            if not value or not isinstance(value, str):
                continue
            text = value.strip()
            if not text:
                continue
            if len(text) > 400:
                text = "..." + text[-400:]
            message += " | %s: %s" % (key, text)
    return message


def unit_names(dimension):
    """Every accepted unit spelling for ``dimension``."""
    return sorted(_UNIT_TABLES[dimension])


def _dimensions():
    return sorted(_UNIT_TABLES)


def conversion_factor(unit, dimension):
    table = _UNIT_TABLES.get(dimension)
    if table is None:
        raise UnitError(
            "unknown quantity dimension %r; supported: %s" % (dimension, ", ".join(_dimensions()))
        )
    factor = table.get(unit)
    if factor is None:
        raise UnitError(
            "unsupported unit %r for %s; supported: %s"
            % (unit, dimension, ", ".join(unit_names(dimension)))
        )
    return factor


def to_canonical(value, unit, dimension):
    """Convert ``value`` expressed in ``unit`` into the canonical unit.

    A bare number is refused: without a unit there is nothing to check the
    caller's intent against, which is exactly how a kN arrives as an N.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnitError("%s must be a number, got %r" % (dimension, value))
    if not unit or not isinstance(unit, str):
        raise UnitError(
            "%s requires an explicit unit string, got %r; supported: %s"
            % (dimension, unit, ", ".join(unit_names(dimension)))
        )
    number = float(value)
    if not math.isfinite(number):
        raise UnitError("%s must be finite, got %r" % (dimension, value))
    return number * conversion_factor(unit, dimension)


def quantity_payload(value, unit, dimension):
    """Validate a quantity and return it in canonical form with both spellings.

    The requested value and unit are kept next to the canonical pair so a caller
    can see what was asked for and what was applied without re-reading the call.
    """
    canonical = to_canonical(value, unit, dimension)
    return {
        "value": canonical,
        "unit": CANONICAL_UNITS[dimension],
        "requested": {"value": value, "unit": unit},
    }


def material_payload(spec):
    """Validate a material specification and return it with explicit units."""
    spec = spec or {}
    if not isinstance(spec, dict):
        raise UnitError("material must be an object mapping material fields to values")
    youngs = spec.get("youngs_modulus") or DEFAULT_MATERIAL["youngs_modulus"]
    density = spec.get("density") or DEFAULT_MATERIAL["density"]
    if not isinstance(youngs, dict) or not isinstance(density, dict):
        raise UnitError("material youngs_modulus and density must be {value, unit} objects")
    modulus = quantity_payload(youngs.get("value"), youngs.get("unit"), "stress")
    if modulus["value"] <= 0:
        raise UnitError("youngs_modulus must be positive, got %s MPa" % modulus["value"])
    rho = quantity_payload(density.get("value"), density.get("unit"), "density")
    if rho["value"] <= 0:
        raise UnitError("density must be positive, got %s t/mm^3" % rho["value"])
    ratio = spec.get("poisson_ratio")
    if ratio is None:
        ratio = DEFAULT_MATERIAL["poisson_ratio"]
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio):
        raise UnitError("poisson_ratio must be a finite number")
    ratio = float(ratio)
    if not -1.0 < ratio < MAX_POISSON_RATIO:
        raise UnitError("poisson_ratio must be greater than -1 and less than 0.5, got %s" % ratio)
    return {
        "name": str(spec.get("name") or DEFAULT_MATERIAL["name"]),
        "youngs_modulus": modulus,
        "poisson_ratio": ratio,
        "density": rho,
        "defaulted": sorted(
            key
            for key in ("name", "youngs_modulus", "poisson_ratio", "density")
            if spec.get(key) is None
        ),
    }


def load_payload(spec):
    """Validate a load case: a force with a unit, and a direction."""
    if not isinstance(spec, dict):
        raise UnitError("load must be an object")
    force = spec.get("force")
    if not isinstance(force, dict):
        raise UnitError("load.force must be a {value, unit} object; a bare float has no unit")
    magnitude = quantity_payload(force.get("value"), force.get("unit"), "force")
    if magnitude["value"] <= 0:
        raise UnitError("load.force must be positive, got %s N" % magnitude["value"])
    direction = spec.get("direction")
    if not isinstance(direction, (list, tuple)) or len(direction) != 3:
        raise UnitError("load.direction must contain exactly three numbers")
    try:
        vector = [float(item) for item in direction]
    except (TypeError, ValueError):
        raise UnitError("load.direction must contain numbers") from None
    if not all(math.isfinite(item) for item in vector):
        raise UnitError("load.direction values must be finite")
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        raise UnitError("load.direction may not be the zero vector")
    return {
        "force": magnitude,
        "direction": vector,
        "unit_direction": [item / norm for item in vector],
        "faces": [str(item) for item in (spec.get("faces") or ())],
    }


def parse_reference(text):
    """Split ``"Object:Face1"`` into ``("Object", "Face1")``.

    A reference that does not name both an object and a sub-element is refused:
    an unqualified object name silently constrains the whole body, which is a
    different analysis that still converges.
    """
    if not isinstance(text, str):
        raise UnitError('a geometry reference must be a string like "Object:Face1"')
    match = _REFERENCE.match(text.strip())
    if match is None:
        raise UnitError(
            'invalid geometry reference %r; expected "<object_name>:<FaceN|EdgeN|VertexN>" '
            "with an object name of at most 64 letters, digits or underscores" % text
        )
    return match.group(1), match.group(2)


def match_length_scale(observed_extents, expected_extents_mm, tolerance=0.05):
    """Identify the length unit of a solver input from its node coordinates.

    ``observed_extents`` are the axis extents measured in the solver's own unit;
    ``expected_extents_mm`` are the same extents measured on the target shape in
    millimetres. Exactly one length unit may explain the ratio: two candidates
    inside the tolerance is reported as ``None``, because an ambiguous schema is
    not a schema.
    """
    if len(observed_extents) != 3 or len(expected_extents_mm) != 3:
        return None
    matches = []
    for name, factor in sorted(_UNIT_TABLES["length"].items()):
        usable = 0
        agrees = 0
        for observed, expected in zip(observed_extents, expected_extents_mm):
            expected_in_unit = expected / factor
            if expected_in_unit <= 0:
                # A flat axis carries no scale information; skipping it is not
                # an assumption, it is the absence of one.
                continue
            usable += 1
            if math.isclose(observed, expected_in_unit, rel_tol=tolerance):
                agrees += 1
        if usable and agrees == usable:
            matches.append(name)
    return matches[0] if len(matches) == 1 else None


def unit_schema_for_length(length_unit):
    """The force and stress units implied by a solver input's length unit."""
    return SCHEMA_BY_LENGTH_UNIT.get(length_unit)


def cantilever_second_moment(width_mm, height_mm):
    """Second moment of area of a rectangular section about its bending axis."""
    return float(width_mm) * float(height_mm) ** 3 / 12.0


def cantilever_tip_deflection(force_n, length_mm, youngs_mpa, second_moment_mm4):
    """Euler-Bernoulli tip deflection of an end-loaded cantilever, in mm."""
    return (
        float(force_n)
        * float(length_mm) ** 3
        / (3.0 * float(youngs_mpa) * float(second_moment_mm4))
    )


def cantilever_root_stress(force_n, length_mm, width_mm, height_mm):
    """Extreme-fibre bending stress at a cantilever's root, in MPa."""
    return (
        float(force_n)
        * float(length_mm)
        * (float(height_mm) / 2.0)
        / cantilever_second_moment(width_mm, height_mm)
    )
