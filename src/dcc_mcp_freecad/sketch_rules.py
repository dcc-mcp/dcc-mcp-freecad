"""Typed sketch domain rules shared by the FreeCAD driver and the adapter.

This module is pure Python and imports nothing from FreeCAD, for two reasons:

* ``freecad_driver.py`` loads it by path (see ``_load_sibling_module``) because
  the adapter package is not importable inside ``FreeCADCmd``;
* the rules are then testable without a FreeCAD host, which is where a dangling
  constraint reference or a quietly dropped dimension gets caught.

Sketcher constraints are why this module exists at all. The adapter accepts no
arbitrary Python, so a caller cannot repair a sketch the tools built: a
constraint that names an element which does not exist, or a dimension the host
swallowed, leaves a sketch that looks finished and solves differently on the
next host version. Every rule here therefore refuses instead of reinterpreting,
and every rejection carries a machine-readable ``error_code``.

Nothing here ever returns a plausible number it cannot defend. A degrees of
freedom count the host will not report stays ``None``, and a sketch whose DOF
is unknown is never reported as usable for a feature.
"""

from __future__ import annotations

import math

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Machine-readable error codes
# ---------------------------------------------------------------------------

ERROR_UNDERCONSTRAINED = "E_SKETCH_UNDERCONSTRAINED"
ERROR_OVERCONSTRAINED = "E_SKETCH_OVERCONSTRAINED"
ERROR_DOF_UNAVAILABLE = "E_SKETCH_DOF_UNAVAILABLE"
ERROR_SOLVER_FAILED = "E_SKETCH_SOLVER_FAILED"
ERROR_ELEMENT_NOT_FOUND = "E_SKETCH_ELEMENT_NOT_FOUND"
ERROR_GEOMETRY_UNKNOWN = "E_SKETCH_GEOMETRY_UNKNOWN"
ERROR_SPEC_INVALID = "E_SKETCH_SPEC_INVALID"

# Codes that must stop a sketch from being consumed as a feature profile. Every
# one of them means the solve is not reproducible, which is the failure the
# typed-only surface exists to make impossible.
FEATURE_BLOCKING_CODES = (
    ERROR_UNDERCONSTRAINED,
    ERROR_OVERCONSTRAINED,
    ERROR_DOF_UNAVAILABLE,
    ERROR_SOLVER_FAILED,
)

# ---------------------------------------------------------------------------
# Planes
# ---------------------------------------------------------------------------

PLANES = ("xy", "xz", "yz")

# The rotation that maps the sketch's local +Z onto the requested plane normal.
# Kept as axis/angle so the adapter never hardcodes a placement matrix: the
# normal is derived from the same table the driver builds the placement from.
PLANE_ROTATIONS = {
    "xy": ((0.0, 0.0, 1.0), 0.0),
    "xz": ((1.0, 0.0, 0.0), 90.0),
    "yz": ((0.0, 1.0, 0.0), 90.0),
}


def rotate_vector(vector, axis, degrees):
    """Rotate ``vector`` about the unit ``axis`` by ``degrees`` (Rodrigues)."""
    angle = math.radians(float(degrees))
    cosine = math.cos(angle)
    sine = math.sin(angle)
    norm = math.sqrt(sum(item * item for item in axis))
    if norm <= 0:
        raise SketchSpecError("a plane rotation axis may not be the zero vector")
    kx, ky, kz = (item / norm for item in axis)
    vx, vy, vz = (float(item) for item in vector)
    dot = kx * vx + ky * vy + kz * vz
    cross = (ky * vz - kz * vy, kz * vx - kx * vz, kx * vy - ky * vx)
    return (
        vx * cosine + cross[0] * sine + kx * dot * (1.0 - cosine),
        vy * cosine + cross[1] * sine + ky * dot * (1.0 - cosine),
        vz * cosine + cross[2] * sine + kz * dot * (1.0 - cosine),
    )


def plane_normal(plane):
    """The world-space normal of a sketch attached to ``plane``."""
    if plane not in PLANE_ROTATIONS:
        raise SketchSpecError("unsupported plane: %s" % (plane,))
    axis, degrees = PLANE_ROTATIONS[plane]
    return rotate_vector((0.0, 0.0, 1.0), axis, degrees)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

GEOMETRY_TYPES = ("arc", "circle", "line", "point", "rectangle")

# ``rectangle`` is a convenience the adapter expands; these are the four kinds
# FreeCAD stores in ``Sketch.Geometry``.
KINDS = ("arc", "circle", "line", "point")

# FreeCAD addresses a geometry element's vertices positionally: 1 is the start
# of a line, 2 its end, 3 the centre of a circle or an arc. Which positions
# exist is a property of the kind, so a reference to a position the kind does
# not have is a request that cannot be honoured -- never one to reinterpret.
VERTEX_POSITIONS = {
    "point": (1,),
    "line": (1, 2),
    "circle": (3,),
    "arc": (1, 2, 3),
}

# Geometry index -1 is the sketch's own origin (root point). It is the only
# negative index, and it is how a sketch gets pinned in space.
ROOT_ELEMENT = -1
ROOT_VERTEX_POSITIONS = (1,)

_GEOMETRY_FIELDS = {
    "point": ("x", "y"),
    "line": ("x1", "y1", "x2", "y2"),
    "circle": ("cx", "cy", "radius"),
    "arc": ("cx", "cy", "radius", "start_angle_degrees", "end_angle_degrees"),
    "rectangle": ("x", "y", "width", "height"),
}

# Fields that must be strictly positive; every other numeric field is a
# coordinate and may be zero or negative.
_POSITIVE_FIELDS = ("radius", "width", "height")


def expand_geometry(spec, tool="sketch.add_geometry"):
    """Normalise one caller geometry spec into the primitives FreeCAD stores.

    A rectangle expands to four lines with an explicit ``role``, because FreeCAD
    has no rectangle element: reporting one id for four edges would leave the
    caller with no way to constrain a corner.
    """
    if not isinstance(spec, dict):
        raise SketchSpecError("%s: each geometry item must be an object" % tool)
    kind = spec.get("type")
    if kind not in _GEOMETRY_FIELDS:
        raise SketchSpecError(
            "%s: unsupported geometry type %r (supported: %s)"
            % (tool, kind, ", ".join(GEOMETRY_TYPES))
        )
    unknown = sorted(set(spec) - set(_GEOMETRY_FIELDS[kind]) - {"type"})
    if unknown:
        raise SketchSpecError(
            "%s: unsupported fields for %s: %s" % (tool, kind, ", ".join(unknown))
        )
    missing = [name for name in _GEOMETRY_FIELDS[kind] if spec.get(name) is None]
    if missing:
        raise SketchSpecError("%s: %s requires %s" % (tool, kind, ", ".join(missing)))
    values = {name: _number(spec[name], name, tool) for name in _GEOMETRY_FIELDS[kind]}
    for name in _POSITIVE_FIELDS:
        if name in values and values[name] <= 0:
            raise SketchSpecError("%s: %s.%s must be positive" % (tool, kind, name))
    if kind == "line":
        length = math.hypot(values["x2"] - values["x1"], values["y2"] - values["y1"])
        if not math.isfinite(length) or length <= 0:
            raise SketchSpecError("%s: line endpoints must be distinct" % tool)
    if kind == "rectangle":
        return _rectangle_lines(values)
    if kind == "line":
        return [
            _primitive("line", None, [[values["x1"], values["y1"]], [values["x2"], values["y2"]]])
        ]
    if kind == "point":
        return [_primitive("point", None, [[values["x"], values["y"]]])]
    if kind == "circle":
        return [
            _primitive(
                "circle",
                None,
                [[values["cx"], values["cy"]]],
                center=[values["cx"], values["cy"]],
                radius=values["radius"],
            )
        ]
    return [_arc(values, tool)]


def _rectangle_lines(values):
    x, y = values["x"], values["y"]
    right = x + values["width"]
    top = y + values["height"]
    corners = (
        ("bottom", [x, y], [right, y]),
        ("right", [right, y], [right, top]),
        ("top", [right, top], [x, top]),
        ("left", [x, top], [x, y]),
    )
    return [_primitive("line", role, [start, end]) for role, start, end in corners]


def _arc(values, tool):
    radius = values["radius"]
    start = values["start_angle_degrees"]
    end = values["end_angle_degrees"]
    if not all(math.isfinite(value) for value in (start, end, radius)):
        raise SketchSpecError("%s: arc angles and radius must be finite" % tool)
    sweep = end - start
    # Reject the sweep by comparing the endpoints it produces, not the sweep
    # itself. A 360 degree sweep passes a "sweep must be non-zero" check while
    # landing its end exactly on its start, which the host then reports as a
    # degenerate arc -- so the endpoint comparison is the one that catches it.
    if _same_angle(start, end):
        raise SketchSpecError(
            "%s: arc start and end angles describe the same point "
            "(start %s, end %s); use a circle for a full turn" % (tool, start, end)
        )
    if abs(sweep) > 360:
        raise SketchSpecError("%s: arc sweep may not exceed 360 degrees" % tool)
    cx, cy = values["cx"], values["cy"]
    points = [_polar(cx, cy, radius, angle) for angle in (start, start + sweep / 2.0, end)]
    return _primitive(
        "arc",
        None,
        points,
        center=[cx, cy],
        radius=radius,
        angles_degrees=[start, end],
        # The direction the adapter modelled the arc in. The host's own default
        # is the reverse of this for a negative sweep, so it is passed
        # explicitly rather than inherited. See _build_geometry.
        clockwise=sweep < 0,
    )


def _same_angle(first, second):
    """True when two angles in degrees land on the same point of a circle."""
    return abs((first - second) % 360.0) < 1e-9


def _primitive(kind, role, points, center=None, radius=None, angles_degrees=None, clockwise=None):
    return {
        "kind": kind,
        "type": kind,
        "role": role,
        "points": [list(item) for item in points],
        "center": list(center) if center is not None else None,
        "radius": radius,
        "angles_degrees": list(angles_degrees) if angles_degrees is not None else None,
        "clockwise": clockwise,
    }


def _polar(cx, cy, radius, degrees):
    angle = math.radians(degrees)
    return [cx + radius * math.cos(angle), cy + radius * math.sin(angle)]


def key_points(primitive):
    """The flat coordinate list a read-back compares against the host.

    The order matches ``freecad_driver._geometry_read_back`` element for
    element: a point reports its position, a line its two ends, a circle its
    centre and radius, an arc its two ends, its midpoint, centre and radius.

    The arc midpoint is compared even though it is derived from the same angles.
    It used to be skipped as redundant, and it is not: the two ends, the centre
    and the radius are all identical for an arc and its complement, so without
    the midpoint a host that reinterpreted the direction returned a byte-for-byte
    match on every compared value while the profile was in the opposite quadrant.
    The midpoint is the only one of the four that differs between the two.
    """
    kind = primitive["kind"]
    points = primitive["points"]
    if kind == "point":
        return list(points[0])
    if kind == "line":
        return list(points[0]) + list(points[1])
    values = list(primitive["center"]) + [float(primitive["radius"])]
    if kind == "arc":
        return list(points[0]) + list(points[1]) + list(points[2]) + values
    return values


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

# ``kind`` is how the adapter names the constraint; ``free_cad_type`` is the
# Sketcher::Constraint constructor it maps to; ``first``/``second`` say whether
# the reference needs a vertex position; ``value`` names the magnitude field.
_CONSTRAINT_SPECS = {
    "coincident": {
        "free_cad_type": "Coincident",
        "first": "vertex",
        "second": "vertex",
        "value": None,
    },
    "concentric": {
        "free_cad_type": "Coincident",
        "first": "element",
        "second": "element",
        "value": None,
        # Concentricity is centre coincidence: both references are pinned to
        # vertex 3 so the caller never has to know the positional convention.
        "vertex": 3,
    },
    "point_on_object": {
        "free_cad_type": "PointOnObject",
        "first": "vertex",
        "second": "element",
        "value": None,
    },
    "horizontal": {
        "free_cad_type": "Horizontal",
        "first": "element",
        "second": None,
        "value": None,
    },
    "vertical": {
        "free_cad_type": "Vertical",
        "first": "element",
        "second": None,
        "value": None,
    },
    "parallel": {
        "free_cad_type": "Parallel",
        "first": "element",
        "second": "element",
        "value": None,
    },
    "perpendicular": {
        "free_cad_type": "Perpendicular",
        "first": "element",
        "second": "element",
        "value": None,
    },
    "tangent": {
        "free_cad_type": "Tangent",
        "first": "element",
        "second": "element",
        "value": None,
    },
    "equal": {"free_cad_type": "Equal", "first": "element", "second": "element", "value": None},
    "distance": {
        "free_cad_type": "Distance",
        "first": "vertex",
        "second": "vertex",
        "value": "value",
    },
    "distance_x": {
        "free_cad_type": "DistanceX",
        "first": "vertex",
        "second": "vertex",
        "value": "value",
    },
    "distance_y": {
        "free_cad_type": "DistanceY",
        "first": "vertex",
        "second": "vertex",
        "value": "value",
    },
    # A line length is the one-dimensional form of ``distance``. Kept as its
    # own type so a dimension can never be silently read as the other one.
    "length": {"free_cad_type": "Distance", "first": "element", "second": None, "value": "value"},
    "radius": {"free_cad_type": "Radius", "first": "element", "second": None, "value": "value"},
    "angle": {
        "free_cad_type": "Angle",
        "first": "element",
        "second": "element",
        "value": "value_degrees",
    },
}

CONSTRAINT_TYPES = tuple(sorted(_CONSTRAINT_SPECS))

# Geometry kinds a constraint may be applied to. Anything absent means "any
# kind", so a new kind is never accepted by accident.
_KIND_REQUIREMENTS = {
    "horizontal": {"first": ("line",)},
    "vertical": {"first": ("line",)},
    "length": {"first": ("line",)},
    "parallel": {"first": ("line",), "second": ("line",)},
    "perpendicular": {"first": ("line",), "second": ("line",)},
    "angle": {"first": ("line",), "second": ("line",)},
    "radius": {"first": ("circle", "arc")},
    "concentric": {"first": ("circle", "arc"), "second": ("circle", "arc")},
    "tangent": {"first": ("line", "circle", "arc"), "second": ("line", "circle", "arc")},
    "equal": {"first": ("line", "circle", "arc"), "second": ("line", "circle", "arc")},
    "point_on_object": {"second": ("line", "circle", "arc")},
}


def validate_constraint(spec, tool="sketch.add_constraint"):
    """Normalise one caller constraint spec, refusing anything ambiguous."""
    if not isinstance(spec, dict):
        raise SketchSpecError("%s: each constraint must be an object" % tool)
    name = spec.get("type")
    if name not in _CONSTRAINT_SPECS:
        raise SketchSpecError(
            "%s: unsupported constraint type %r (supported: %s)"
            % (tool, name, ", ".join(CONSTRAINT_TYPES))
        )
    definition = _CONSTRAINT_SPECS[name]
    allowed = {"type"}
    for role in ("first", "second"):
        if definition[role]:
            allowed.add(role)
    if definition["value"]:
        allowed.add(definition["value"])
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise SketchSpecError(
            "%s: unsupported fields for %s: %s" % (tool, name, ", ".join(unknown))
        )
    normalized = {
        "type": name,
        "free_cad_type": definition["free_cad_type"],
        "first": _reference(spec, "first", definition, tool),
        "second": _reference(spec, "second", definition, tool),
        "value": None,
        "value_degrees": None,
    }
    if definition["value"]:
        field = definition["value"]
        if spec.get(field) is None:
            raise SketchSpecError("%s: %s requires %s" % (tool, name, field))
        normalized[field] = _dimension(spec[field], name, tool)
    return normalized


def _reference(spec, role, definition, tool):
    needed = definition[role]
    if needed is None:
        return None
    value = spec.get(role)
    if value is None:
        raise SketchSpecError("%s: %s requires %s" % (tool, definition["free_cad_type"], role))
    if not isinstance(value, dict):
        raise SketchSpecError(
            "%s: %s.%s must be an object" % (tool, definition["free_cad_type"], role)
        )
    unknown = sorted(set(value) - {"element", "vertex"})
    if unknown:
        raise SketchSpecError(
            "%s: %s.%s has unsupported fields: %s"
            % (tool, definition["free_cad_type"], role, ", ".join(unknown))
        )
    element = value.get("element")
    if isinstance(element, bool) or not isinstance(element, int):
        raise SketchSpecError(
            "%s: %s.%s.element must be an integer element index"
            % (tool, definition["free_cad_type"], role)
        )
    if element < ROOT_ELEMENT:
        raise SketchSpecError(
            "%s: %s.%s.element %d is below the sketch root point (%d)"
            % (tool, definition["free_cad_type"], role, element, ROOT_ELEMENT)
        )
    vertex = value.get("vertex")
    if needed == "vertex":
        if isinstance(vertex, bool) or not isinstance(vertex, int):
            raise SketchSpecError(
                "%s: %s.%s requires a vertex position" % (tool, definition["free_cad_type"], role)
            )
    elif vertex is not None:
        raise SketchSpecError(
            "%s: %s.%s takes no vertex position" % (tool, definition["free_cad_type"], role)
        )
    forced = definition.get("vertex")
    if forced is not None:
        vertex = forced
    reference = {"element": element}
    if vertex is not None:
        reference["vertex"] = vertex
    return reference


def _dimension(value, name, tool):
    number = _number(value, "value", tool)
    if name == "radius":
        if number <= 0:
            raise SketchSpecError("%s: radius must be positive" % tool)
        return number
    if name == "length":
        if number <= 0:
            raise SketchSpecError("%s: length must be positive" % tool)
        return number
    if number < 0:
        raise SketchSpecError("%s: %s must not be negative" % (tool, name))
    return number


def check_kinds(constraint, first_kind, second_kind):
    """Refuse a constraint applied to geometry that cannot carry it."""
    name = constraint["type"]
    requirements = _KIND_REQUIREMENTS.get(name) or {}
    for role, kinds in requirements.items():
        kind = first_kind if role == "first" else second_kind
        if kind is not None and kind not in kinds:
            raise SketchStateError(
                ERROR_SPEC_INVALID,
                "%s on element %d: %s geometry cannot carry a %s constraint"
                % (name, constraint[role]["element"], kind, name),
                constraint_type=name,
                role=role,
                kind=kind,
                allowed=list(kinds),
            )
    if name == "equal" and first_kind is not None and first_kind != second_kind:
        raise SketchStateError(
            ERROR_SPEC_INVALID,
            "equal requires two elements of the same kind, got %s and %s"
            % (first_kind, second_kind),
            constraint_type=name,
            first_kind=first_kind,
            second_kind=second_kind,
        )
    for role, reference in (("first", constraint["first"]), ("second", constraint["second"])):
        if reference is None or "vertex" not in reference:
            continue
        kind = first_kind if role == "first" else second_kind
        allowed = (
            ROOT_VERTEX_POSITIONS
            if reference["element"] == ROOT_ELEMENT
            else (VERTEX_POSITIONS.get(kind) or ())
        )
        if reference["vertex"] not in allowed:
            raise SketchStateError(
                ERROR_SPEC_INVALID,
                "vertex %d does not exist on %s geometry (available: %s)"
                % (reference["vertex"], kind, ", ".join(str(item) for item in allowed) or "none"),
                constraint_type=name,
                role=role,
                kind=kind,
                vertex=reference["vertex"],
            )


# ---------------------------------------------------------------------------
# Degrees of freedom
# ---------------------------------------------------------------------------


def feature_state(dof, geometry_count=0):
    """The feature-readiness verdict for a measured sketch.

    ``dof is None`` means the host would not report it. That is reported as
    unusable rather than as zero: a sketch that cannot be shown to be fully
    constrained must not reach a feature, and inventing a number is exactly the
    "a plausible wrong number gets trusted" failure this module exists to avoid.
    """
    state = {
        "dof": dof,
        "dof_available": dof is not None,
        "geometry_count": int(geometry_count),
        "fully_constrained": False,
        "feature_ready": False,
        "blocking_error_code": None,
        "blocking_reason": None,
    }
    if dof is None:
        state["blocking_error_code"] = ERROR_DOF_UNAVAILABLE
        state["blocking_reason"] = (
            "The host did not report degrees of freedom, so the sketch cannot be shown "
            "to be fully constrained."
        )
        return state
    if dof < 0:
        state["blocking_error_code"] = ERROR_OVERCONSTRAINED
        state["blocking_reason"] = (
            "The solver reported %d degrees of freedom: the sketch is over-constrained "
            "(redundant or conflicting constraints)." % dof
        )
        return state
    if dof > 0:
        state["blocking_error_code"] = ERROR_UNDERCONSTRAINED
        state["blocking_reason"] = (
            "The sketch has %d unconstrained degree(s) of freedom, so a feature built on "
            "it is not reproducible across hosts." % dof
        )
        return state
    state["fully_constrained"] = True
    if geometry_count <= 0:
        # An empty sketch solves with zero DOF and is still not a profile.
        state["blocking_error_code"] = ERROR_UNDERCONSTRAINED
        state["blocking_reason"] = "The sketch contains no geometry, so it cannot be a profile."
        return state
    state["feature_ready"] = True
    return state


def assert_feature_ready(state, sketch_name, tool):
    """Refuse to consume a sketch that is not provably fully constrained."""
    if state.get("feature_ready"):
        return state
    raise SketchStateError(
        state.get("blocking_error_code") or ERROR_UNDERCONSTRAINED,
        "%s: sketch %r is not usable as a feature profile. %s"
        % (tool, sketch_name, state.get("blocking_reason") or ""),
        sketch_name=sketch_name,
        dof=state.get("dof"),
        geometry_count=state.get("geometry_count"),
        fully_constrained=state.get("fully_constrained"),
    )


# ---------------------------------------------------------------------------
# Errors and helpers
# ---------------------------------------------------------------------------


def _jsonable(value):
    """Render a detail for the structured payload that crosses the process boundary."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return repr(value)


class SketchSpecError(ValueError):
    """A geometry or constraint spec the adapter refuses instead of reinterpreting."""


class SketchStateError(RuntimeError):
    """A sketch is in a state that must stop the caller.

    Carries ``code`` and a ``state_payload`` the driver forwards verbatim across
    the process boundary. ``code`` is the same attribute every typed refusal in
    the adapter uses, so one caller-side branch handles all of them instead of
    two parallel vocabularies.
    """

    def __init__(self, code, message, **details):
        self.code = code
        self.state_payload = {
            "schema_version": SCHEMA_VERSION,
            "code": code,
            "details": _jsonable(details),
        }
        super().__init__(message)


def _number(value, name, tool):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SketchSpecError("%s: %s must be a number" % (tool, name))
    number = float(value)
    if not math.isfinite(number):
        raise SketchSpecError("%s: %s must be finite" % (tool, name))
    return number
