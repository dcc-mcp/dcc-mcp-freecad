"""Package-owned FreeCADCmd entry point. This module runs inside FreeCAD's Python."""

import json
import math
import os
import shutil
import subprocess
import sys
import time

_MATRIX_FILENAME = "compat_matrix.json"
_CONTRACT_FILENAME = "write_contract.py"
_COMPAT_MODULE = None
_SIBLING_MODULES = {}

# Exchange formats the Mesh module owns. 3MF joined the set because it is the
# mainstream 3D-printing container and the host's MeshCore writer has shipped a
# Reader3MF/Writer3MF pair on both supported release lines; it tessellates from
# the same mesh as STL/OBJ and is therefore guarded by the same assertions.
_MESH_SUFFIXES = ("stl", "obj", "3mf")

# Slack allowed when comparing an imported mesh against the envelope of the file
# it was read from. A mesh format stores vertices, so the round-trip is exact up
# to the reader's own floating-point noise; the slack only absorbs that, not a
# missing or substituted body of geometry.
_MESH_IMPORT_SLACK = 1e-6

# The unit the host's 3MF writer declares, and the unit FreeCAD models in. A 3MF
# consumer scales the model by this declaration, so a wrong or missing value
# silently changes the printed size - it is asserted on every export.
_3MF_MODEL_PART = "3D/3dmodel.model"
_3MF_REQUIRED_UNIT = "millimeter"


class IncompatibleHostError(RuntimeError):
    """The running FreeCAD host exposes an API the matrix declares as broken."""


def _driver_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _matrix_path():
    return os.path.join(_driver_dir(), _MATRIX_FILENAME)


def _load_sibling_module(filename, module_name):
    """Load a module that ships next to this driver, by path.

    The driver is executed directly by FreeCADCmd, so the adapter package is not
    importable. Loading sibling modules by path keeps one single source of truth
    for the compatibility matrix and the write contract instead of duplicating
    either one inside this file.
    """
    if module_name in _SIBLING_MODULES:
        return _SIBLING_MODULES[module_name]
    import importlib.util

    path = os.path.join(_driver_dir(), filename)
    if not os.path.isfile(path):
        raise RuntimeError(
            "The packaged FreeCAD driver is missing %s next to it (%s); reinstall "
            "dcc-mcp-freecad" % (filename, path)
        )
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SIBLING_MODULES[module_name] = module
    return module


def _compat_module():
    """Load the shared compatibility module from this driver's own directory."""
    global _COMPAT_MODULE
    if _COMPAT_MODULE is None:
        if not os.path.isfile(_matrix_path()):
            raise RuntimeError(
                "The FreeCAD compatibility matrix is missing next to the packaged driver "
                "(%s); reinstall dcc-mcp-freecad" % _matrix_path()
            )
        _COMPAT_MODULE = _load_sibling_module("compat.py", "dcc_mcp_freecad_compat")
    return _COMPAT_MODULE


def _contract_module():
    """Load the post-write read-back contract that ships next to this driver."""
    return _load_sibling_module(_CONTRACT_FILENAME, "dcc_mcp_freecad_write_contract")


def _sketch_module():
    """Load the typed sketch rules that ship next to this driver."""
    return _load_sibling_module("sketch_rules.py", "dcc_mcp_freecad_sketch_rules")


def _host_version():
    import FreeCAD as App

    return ".".join(list(App.Version())[:3])


def _applied_breaking_changes(version):
    return _compat_module().breaking_changes_for(version)


def _breaking_change_for_symbol(symbol, version):
    """Return the declared break covering ``symbol`` on this host version."""
    for entry in _applied_breaking_changes(version):
        probe = entry.get("probe") or {}
        covered = {entry.get("removed_symbol"), probe.get("attribute")}
        if symbol in {item for item in covered if item}:
            return entry
    return None


def host_matrix(version):
    """Classify ``version`` against the shipped matrix."""
    return _compat_module().classify_host(version)


def _require_supported_host(version):
    """Refuse to mutate or export geometry on a host outside the matrix.

    ``system.status`` stays available on every host: it is the instrument that
    measures the version, so it must never be the thing that cannot report it.
    """
    verdict = host_matrix(version)
    if verdict["status"] == _compat_module().SUPPORTED:
        return verdict
    raise IncompatibleHostError(
        "%s Install or pin a supported FreeCAD and re-run "
        "`dcc-mcp-freecad doctor --json`." % _compat_module().unsupported_reason(verdict)
    )


def _resolve_property(obj, name, version):
    """Resolve a host property, failing loudly instead of silently no-op'ing.

    Writing to a renamed or removed property is the classic silent-success bug:
    the call returns and the geometry never changes. Every dimension write goes
    through here so a moved API raises with the replacement spelled out.
    """
    entry = _breaking_change_for_symbol(name, version)
    if entry is not None and name == entry.get("removed_symbol"):
        kind = entry.get("kind")
        replacement = entry.get("replacement") or "no drop-in replacement"
        if kind == "renamed":
            raise IncompatibleHostError(
                "FreeCAD %s renamed %s to %s (changed in %s). %s"
                % (version, name, replacement, entry.get("changed_in"), entry.get("remediation"))
            )
        if kind == "removed":
            raise IncompatibleHostError(
                "FreeCAD %s removed %s (changed in %s). Use %s instead. %s"
                % (version, name, entry.get("changed_in"), replacement, entry.get("remediation"))
            )
    if not hasattr(obj, name):
        raise IncompatibleHostError(
            "FreeCAD %s does not expose property %s on %s; the host API moved outside the "
            "verified compatibility matrix, so the write was refused instead of silently "
            "doing nothing" % (version, name, getattr(obj, "TypeId", type(obj).__name__))
        )
    return name


def _probe_instance_property(type_id, attribute):
    """Probe a property on a live object of ``type_id``.

    FreeCAD exposes many properties only on instances, so ``dir()`` on the type
    cannot answer whether a property still exists. The object is created in a
    throwaway document that is never saved or touched on disk.
    """
    import FreeCAD as App

    document = App.newDocument("DccMcpCompatProbe")
    try:
        obj = document.addObject(type_id, "DccMcpProbeObject")
        return "present" if hasattr(obj, attribute) else "absent"
    finally:
        App.closeDocument(document.Name)


def _probe_owner(module, probe):
    """Resolve the object a probe should inspect, or None when unavailable."""
    owner_name = probe.get("owner")
    if owner_name:
        return getattr(module, str(owner_name).split("::")[-1], None)
    return module


def _probe_breaking_changes(version):
    """Introspect this host for every declared break and compare with the matrix.

    Every declared break is probed, not only the ones that apply to this host: on
    a pre-change host the old symbol is expected to still be present, and seeing
    it is positive evidence that the matrix is right. The probe is evidence, not
    a gate: a mismatch is reported so the matrix can be corrected before a wrong
    assumption reaches a user.
    """
    compat = _compat_module()
    parsed = compat.parse_version(version)
    probes = []
    for entry in compat.load_matrix().get("breaking_changes") or ():
        probe = entry.get("probe") or {}
        module_name = probe.get("module")
        attribute = probe.get("attribute")
        changed_in = compat.parse_version(str(entry.get("changed_in", "")))
        applies = bool(parsed and changed_in and parsed >= changed_in)
        expected = (
            probe.get("expected_on_or_after_changed_in")
            if applies
            else probe.get("expected_before_changed_in", "present")
        )
        result = {
            "id": entry.get("id"),
            "attribute": attribute,
            "applies_to_host": applies,
            "expected": expected,
            "observed": "unavailable",
            "replacement_observed": None,
            "matches_expected": None,
        }
        if attribute:
            try:
                __import__(module_name)
                if probe.get("owner_type_id"):
                    result["observed"] = _probe_instance_property(probe["owner_type_id"], attribute)
                    replacement = probe.get("replacement_attribute")
                    if replacement:
                        result["replacement_observed"] = _probe_instance_property(
                            probe["owner_type_id"], replacement
                        )
                else:
                    owner = _probe_owner(sys.modules[module_name], probe)
                    if owner is not None:
                        result["observed"] = "present" if hasattr(owner, attribute) else "absent"
            except Exception:
                result["observed"] = "unavailable"
        # "unverified" means the matrix states no expectation for this host, so
        # the observation is recorded as evidence without claiming a match.
        if result["observed"] != "unavailable" and expected != "unverified":
            result["matches_expected"] = result["observed"] == expected
        probes.append(result)
    return probes


def _required(params, key, tool):
    """Reject a missing or null parameter instead of letting it travel on.

    A parameter the tool does not understand is never treated as absent: a
    silent ``None`` is how a request ends up half-applied and reported as done.
    """
    if key not in params or params[key] is None:
        raise ValueError("%s requires parameter %r" % (tool, key))
    return params[key]


def _dimensions(params, tool):
    """Reject a dimensions argument that is not a property mapping."""
    value = params.get("dimensions")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            "%s.dimensions must be an object mapping property names to numbers, got %s"
            % (tool, type(value).__name__)
        )
    return value


def _size_or_missing(path):
    """Describe an artefact for a mismatch report."""
    if not os.path.isfile(path):
        return "missing: %s" % path
    return "%d bytes" % os.path.getsize(path)


def _vector(value, name):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("%s must contain exactly three numbers" % name)
    vector = [float(item) for item in value]
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("%s values must be finite" % name)
    return vector


def _positive(value, name, allow_zero=False):
    number = float(value)
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError(
            "%s must be a finite %s number" % (name, "non-negative" if allow_zero else "positive")
        )
    return number


def _rotation(app, axis, degrees):
    values = _vector(axis, "rotation_axis")
    if sum(item * item for item in values) <= 0:
        raise ValueError("rotation_axis may not be the zero vector")
    angle = float(degrees)
    if not math.isfinite(angle):
        raise ValueError("rotation_degrees must be finite")
    return app.Rotation(app.Vector(*values), angle)


# A scale factor is bounded the way the other numeric knobs are bounded: it is a
# modelling convenience, not a way to ask the kernel for a degenerate solid.
_MAX_SCALE = 1000.0

# Scale origins. "centroid" keeps the shape's bounding-box centre fixed, which is
# what "scale this part about its own middle" means; "origin" scales about the
# document origin.
_SCALE_ORIGINS = ("centroid", "origin")

# Mirror planes by name, as the normal of the plane they name.
_MIRROR_PLANE_NORMALS = {
    "xy": (0.0, 0.0, 1.0),
    "xz": (0.0, 1.0, 0.0),
    "yz": (1.0, 0.0, 0.0),
}

# The host rejects a helix whose turn count exceeds this in Helix::execute; the
# adapter refuses it up front with a sentence instead of a kernel failure.
_MAX_HELIX_TURNS = 1e4


def _scale_factors(value, tool):
    """Accept a scalar or a three-element vector of positive finite factors.

    A zero or negative factor is refused here rather than left to the kernel: a
    mirror is a different operation with a different read-back, and a zero
    factor collapses the solid to nothing while the object still exists.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, list)):
        raise ValueError("%s.scale must be a number or a list of three numbers" % tool)
    if isinstance(value, list):
        if len(value) != 3:
            raise ValueError("%s.scale must contain exactly three numbers" % tool)
        factors = [float(item) for item in value]
    else:
        factors = [float(value)] * 3
    for item in factors:
        if not math.isfinite(item):
            raise ValueError("%s.scale values must be finite" % tool)
        if item <= 0:
            raise ValueError("%s.scale values must be greater than zero" % tool)
        if item > _MAX_SCALE:
            raise ValueError("%s.scale values must not exceed %g" % (tool, _MAX_SCALE))
    return factors


def _shape_or_error(obj, name, tool):
    """Return a usable Part shape, or refuse with the object that lacked one."""
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError("%s requires an object with a non-empty Part shape: %s" % (tool, name))
    return shape


def _move_to_local(geometry, source):
    """Rebase copied geometry out of the source's placement and return it.

    ``obj.Shape`` and ``obj.Mesh`` are reported in document coordinates, so a
    copy taken straight from the source already carries the source's placement
    baked in. Assigning ``Placement`` on top of it then composes the two, which
    makes ``translation`` mean "offset from the source" for a copied solid but
    "absolute position" for a primitive re-created from its dimensions. The copy
    is moved back by the inverse placement so that every source type then takes
    the same absolute ``Placement``.

    The two kinds of geometry transform differently and neither is in place:
    ``Part.Shape.transformGeometry`` returns a new shape and leaves the original
    alone, while ``Mesh.Mesh.transform`` mutates the mesh it is called on. So
    the shape result must be taken from the call and the mesh must not be.

    The rebase is driven by where the geometry actually sits, not by
    ``source.Placement``. A primitive keeps the two in step, but a boolean
    result carries an identity ``Placement`` while its ``Shape`` is already baked
    into document coordinates, so inverting ``Placement`` there is a no-op that
    leaves the source offset in place. The bounding box minimum is the origin the
    geometry is really expressed from, so it is the same answer for both.
    """
    matrix = _placement_to_local_matrix(geometry, source)
    if matrix is None:
        return geometry
    if hasattr(geometry, "transformGeometry"):
        return geometry.transformGeometry(matrix)
    geometry.transform(matrix)
    return geometry


def _placement_to_local_matrix(geometry, source):
    """Build the matrix that rebases ``geometry`` back onto its own origin.

    Returns ``None`` when there is nothing to undo, so the caller can hand the
    geometry back untouched instead of applying an identity transform.
    """
    import FreeCAD as App

    bounds = getattr(geometry, "BoundBox", None)
    if bounds is None:
        return None
    origin = App.Vector(bounds.XMin, bounds.YMin, bounds.ZMin)
    if origin.Length == 0:
        return None
    # ``Matrix.move`` left-multiplies a pure translation and mutates in place,
    # so it is started from identity and read after the call. There is no
    # ``Matrix.translate``; that name exists only on ``Placement``.
    matrix = App.Matrix()
    matrix.move(origin.negative())
    return matrix


def _scaled_shape(app, shape, factors, around, tool):
    """Scale ``shape`` about ``around`` and return the new shape.

    ``FreeCAD`` reports ``obj.Shape`` already carrying the object's placement, so
    the geometry handled here is in document coordinates. The three steps are
    applied as separate ``transformGeometry`` calls rather than one composed
    matrix because a composed ``App.Matrix`` multiplication order is easy to get
    backwards and impossible to see in the result until a non-uniform factor is
    used; three explicit steps have one obvious order.
    """
    if around not in _SCALE_ORIGINS:
        raise ValueError("%s.around must be one of %s" % (tool, ", ".join(_SCALE_ORIGINS)))
    current = shape
    centre = shape.BoundBox.Center if around == "centroid" else None
    if centre is not None:
        step = app.Matrix()
        step.move(app.Vector(-centre.x, -centre.y, -centre.z))
        current = current.transformGeometry(step)
    scale_matrix = app.Matrix()
    scale_matrix.scale(*factors)
    current = current.transformGeometry(scale_matrix)
    if centre is not None:
        step = app.Matrix()
        step.move(centre)
        current = current.transformGeometry(step)
    return current


def _mirror_plane(params, tool):
    """Resolve a mirror request into a plane normal and a point on the plane.

    Either the named ``plane`` or an explicit ``normal`` may be given; supplying
    both, or neither, is a refusal rather than a silent precedence rule.
    """
    plane = params.get("plane")
    normal = params.get("normal")
    origin = params.get("origin")
    if (plane is None) == (normal is None):
        raise ValueError("%s requires exactly one of plane or normal" % tool)
    if plane is not None:
        if plane not in _MIRROR_PLANE_NORMALS:
            raise ValueError(
                "%s.plane must be one of %s" % (tool, ", ".join(sorted(_MIRROR_PLANE_NORMALS)))
            )
        values = list(_MIRROR_PLANE_NORMALS[plane])
    else:
        values = _vector(normal, "%s.normal" % tool)
        if sum(item * item for item in values) <= 0:
            raise ValueError("%s.normal may not be the zero vector" % tool)
    point = _vector(origin, "%s.origin" % tool) if origin is not None else [0.0, 0.0, 0.0]
    return values, point


def _placement_payload(placement):
    axis = placement.Rotation.Axis
    return {
        "translation": [placement.Base.x, placement.Base.y, placement.Base.z],
        "rotation_axis": [axis.x, axis.y, axis.z],
        "rotation_degrees": placement.Rotation.Angle * 180.0 / math.pi,
    }


# ---------------------------------------------------------------------------
# Post-write read-back contract
#
# Every mutating method proves its own effect before it returns. The helpers
# below exist so that proof reads the same in every tool: name the check, name
# what was asked for, name what came back, and refuse to return anything that
# disagrees.
# ---------------------------------------------------------------------------


def _host_matrix_digest(version):
    """A trimmed matrix verdict to attach to a read-back mismatch.

    The full verdict repeats the whole evidence text for every declared range;
    a mismatch report only needs enough to tell drift from a genuine modelling
    error, so it carries the status and the matched range id.
    """
    verdict = host_matrix(version)
    return {
        "status": verdict.get("status"),
        "range_id": (verdict.get("range") or {}).get("id"),
        "matrix_version": verdict.get("matrix_version"),
    }


class _ReadBack:
    """Post-write assertions for one mutating call.

    Holds the tool name, host version and request parameters so each assertion
    only has to state what it compared. Every mismatch raises the contract's
    ``WriteVerificationError``, which ``main`` forwards as structured JSON.
    """

    def __init__(self, tool, version, params):
        self.tool = tool
        self.version = version
        self.params = params
        # Names of the checks that ran. Returned to the caller so the read-back
        # is evidence in the response instead of an invisible private step.
        self.checks = []

    def check(self, condition, name, expected, actual, remediation=None):
        self.checks.append(name)
        if condition:
            return True
        raise _contract_module().WriteVerificationError(
            tool=self.tool,
            check=name,
            expected=expected,
            actual=actual,
            host_version=self.version,
            host_matrix=_host_matrix_digest(self.version),
            params=self.params,
            remediation=remediation,
        )

    def numbers(self, name, expected, actual, remediation=None, rel_tolerance=None):
        return self.check(
            _contract_module().numbers_match(expected, actual, rel_tolerance),
            name,
            expected,
            actual,
            remediation,
        )

    def sequences(self, name, expected, actual, remediation=None):
        return self.check(
            _contract_module().sequences_match(expected, actual),
            name,
            expected,
            actual,
            remediation,
        )

    def exists(self, name, expected_name, obj):
        return self.check(
            obj is not None,
            "%s.exists" % name,
            expected_name,
            None,
            "The object is absent after the write, so the change was not saved. "
            "Re-run the tool and inspect the document.",
        )

    def placement(self, name, app, expected, actual, remediation=None):
        return self.check(
            _placements_match(app, expected, actual),
            "%s.placement" % name,
            _placement_payload(expected),
            _placement_payload(actual),
            remediation,
        )

    def fail(self, name, expected, actual, remediation=None):
        """Raise unconditionally: the read-back could not even be taken."""
        return self.check(False, name, expected, actual, remediation)

    def shape(self, name, obj):
        """Assert the object carries a usable shape after the write."""
        shape = getattr(obj, "Shape", None)
        self.check(
            shape is not None and not shape.isNull(),
            "%s.shape.not_null" % name,
            "a non-null shape",
            "null" if shape is not None else "missing",
            "FreeCAD accepted the call but produced no geometry; the parameters "
            "were rejected downstream instead of being refused.",
        )
        valid = bool(shape.isValid())
        self.check(
            valid,
            "%s.shape.valid" % name,
            True,
            valid,
            "FreeCAD accepted the parameters but the result is not a valid shape, "
            "so returning success would hide it.",
        )
        return shape


def _verified_checks(read_back):
    """The checks a mutating call passed, as reported evidence."""
    return list(read_back.checks)


def _verify_box(read_back, name, box):
    """Assert a bounding box exists and is finite.

    An absent or non-finite box is the signature of degenerate geometry: the
    tool would otherwise report a shape that cannot be placed in space.
    """
    if box is None:
        read_back.fail(
            "%s.bounding_box.present" % name,
            "a bounding box",
            None,
            "The host reported no bounding box, so the geometry has no extent.",
        )
        return
    valid = getattr(box, "isValid", None) is None or box.isValid()
    read_back.check(
        valid,
        "%s.bounding_box.valid" % name,
        True,
        valid,
        "The bounding box is not valid, so the geometry cannot be measured.",
    )
    for axis in ("XLength", "YLength", "ZLength"):
        value = getattr(box, axis, None)
        finite = value is not None and math.isfinite(float(value)) and float(value) >= 0
        read_back.check(
            finite,
            "%s.bounding_box.%s" % (name, axis),
            "a finite non-negative length",
            value,
            "The geometry extends without bound along %s." % axis,
        )


def _source_box(objects):
    """Union of the exported objects' bounding boxes, or None if none has one."""
    union = None
    for obj in objects:
        shape = getattr(obj, "Shape", None)
        box = None
        if shape is not None and not shape.isNull():
            box = getattr(shape, "BoundBox", None)
        if box is None:
            mesh = getattr(obj, "Mesh", None)
            if mesh is not None and getattr(mesh, "CountPoints", 0):
                box = getattr(mesh, "BoundBox", None)
        if box is None:
            continue
        if union is None:
            union = box
        else:
            union.add(box)
    return union


def _vectors_match(expected, actual, tolerance=1e-6):
    """Compare two 3D points component-wise."""
    return _contract_module().sequences_match(
        (expected.x, expected.y, expected.z), (actual.x, actual.y, actual.z), tolerance
    )


def _rotations_match(app, expected, actual, tolerance=1e-6):
    """Compare two rotations irrespective of how the host normalised them.

    FreeCAD is free to store a rotation as an equivalent axis/angle pair, so
    comparing ``Axis`` and ``Angle`` field by field rejects rotations that are
    the same transform. ``isSame`` answers the question that actually matters
    and is the host's own comparison; where it is unavailable the placement is
    applied to probe vectors, because two rotations that move every probe point
    to the same place are the same rotation whatever their internal spelling.
    """
    is_same = getattr(expected, "isSame", None)
    if callable(is_same):
        try:
            return bool(is_same(actual, tolerance))
        except Exception:
            pass
    if not (hasattr(expected, "multVec") and hasattr(actual, "multVec")):
        return False
    for point in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 2.0, 3.0)):
        probe = app.Vector(*point)
        if not _vectors_match(expected.multVec(probe), actual.multVec(probe), tolerance):
            return False
    return True


def _placements_match(app, expected, actual, tolerance=1e-6):
    """Compare a placement as a transform, not as stored fields."""
    if not _vectors_match(expected.Base, actual.Base, tolerance):
        return False
    return _rotations_match(app, expected.Rotation, actual.Rotation, tolerance)


def _box_contains(outer, inner, slack):
    """True when ``inner`` lies within ``outer`` expanded by ``slack``.

    Used to prove an exported mesh came from the requested object: a tessellation
    only ever places vertices on the surface it was given, so the exported box
    cannot leave the source box. An export of the wrong object, or of nothing,
    shows up here as a box that escaped.
    """
    if inner is None or outer is None:
        return False
    if getattr(inner, "isValid", None) is not None and not inner.isValid():
        return False
    if getattr(outer, "isValid", None) is not None and not outer.isValid():
        return False
    pairs = (
        (outer.XMin - slack, inner.XMin, outer.XMax + slack, inner.XMax),
        (outer.YMin - slack, inner.YMin, outer.YMax + slack, inner.YMax),
        (outer.ZMin - slack, inner.ZMin, outer.ZMax + slack, inner.ZMax),
    )
    for low, inner_low, high, inner_high in pairs:
        if inner_low < low or inner_high > high:
            return False
    return True


def _bound_box_payload(box):
    if box is None or not box.isValid():
        return None
    return {
        "min": [box.XMin, box.YMin, box.ZMin],
        "max": [box.XMax, box.YMax, box.ZMax],
        "size": [box.XLength, box.YLength, box.ZLength],
        "center": [box.Center.x, box.Center.y, box.Center.z],
    }


def _object_payload(obj):
    payload = {
        "name": obj.Name,
        "label": obj.Label,
        "type_id": obj.TypeId,
        "placement": _placement_payload(obj.Placement) if hasattr(obj, "Placement") else None,
        "outgoing_links": sorted(item.Name for item in obj.OutList),
        "incoming_links": sorted(item.Name for item in obj.InList),
    }
    shape = getattr(obj, "Shape", None)
    if shape is not None and not shape.isNull():
        payload["shape"] = {
            "shape_type": shape.ShapeType,
            "valid": bool(shape.isValid()),
            "closed": bool(shape.isClosed()),
            "solids": len(shape.Solids),
            "shells": len(shape.Shells),
            "faces": len(shape.Faces),
            "edges": len(shape.Edges),
            "vertices": len(shape.Vertexes),
            "volume": shape.Volume,
            "area": shape.Area,
            "length": shape.Length,
            "bounding_box": _bound_box_payload(shape.BoundBox),
        }
    mesh = getattr(obj, "Mesh", None)
    if mesh is not None and mesh.CountPoints:
        payload["mesh"] = {
            "points": mesh.CountPoints,
            "facets": mesh.CountFacets,
            "volume": mesh.Volume,
            "area": mesh.Area,
            "bounding_box": _bound_box_payload(mesh.BoundBox),
        }
    return payload


def _document_payload(doc):
    return {
        "name": doc.Name,
        "label": doc.Label,
        "file_name": doc.FileName,
        "object_count": len(doc.Objects),
        "objects": [_object_payload(obj) for obj in doc.Objects],
    }


def _open_document(app, path):
    doc = app.openDocument(path)
    if doc is None:
        raise RuntimeError("FreeCAD could not open the document")
    return doc


def _close_document(app, doc):
    """Close a document, tolerating a handle the host already invalidated.

    This runs in ``finally`` blocks, where an error raised here would replace the
    error that actually matters. Reopening a file FreeCAD already tracks can hand
    back a handle that dies on first access; there is nothing left to close then.
    """
    if doc is None:
        return
    try:
        name = doc.Name
    except Exception:
        return
    app.closeDocument(name)


def _save_document(doc):
    doc.recompute()
    doc.save()


def system_status(_params):
    import FreeCAD as App

    version = list(App.Version())
    reported = ".".join(version[:3])
    return {
        "version": reported,
        "version_details": version,
        "console_mode": True,
        "python_version": sys.version.split()[0],
        "host_matrix": host_matrix(reported),
        "api_probe": _probe_breaking_changes(reported),
        "gui_library": _gui_library_probe(),
    }


def document_create(params):
    import FreeCAD as App

    tool = "document.create"
    version = _host_version()
    path = _required(params, "document_path", tool)
    doc = App.newDocument(_required(params, "name", tool))
    try:
        doc.recompute()
        doc.saveAs(path)
        # Read-back: the durable artefact is the product of this call, so the
        # call is only a success once the bytes exist on disk.
        read_back = _ReadBack(tool, version, params)
        read_back.check(
            os.path.isfile(path) and os.path.getsize(path) > 0,
            "artifact.non_empty",
            "a non-empty .FCStd file",
            _size_or_missing(path),
            "FreeCAD returned without an error but left no document behind.",
        )
        return {
            "document": _document_payload(doc),
            "document_path": path,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def document_inspect(params):
    import FreeCAD as App

    doc = _open_document(App, params["document_path"])
    try:
        doc.recompute()
        return _document_payload(doc)
    finally:
        _close_document(App, doc)


def _invalid_shape_names(doc):
    """Names of the objects whose shape is empty or invalid after a recompute."""
    invalid = []
    empty_shapes = []
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is not None:
            if shape.isNull():
                empty_shapes.append(obj.Name)
            elif not shape.isValid():
                invalid.append(obj.Name)
    return invalid, empty_shapes


def document_validate(params):
    import FreeCAD as App

    doc = _open_document(App, params["document_path"])
    try:
        doc.recompute()
        invalid, empty_shapes = _invalid_shape_names(doc)
        return {
            "valid": not invalid,
            "invalid_objects": invalid,
            "empty_shape_objects": empty_shapes,
            "object_count": len(doc.Objects),
        }
    finally:
        _close_document(App, doc)


def _presentation_geometry(doc):
    result = []
    for obj in doc.Objects:
        value = _object_payload(obj)
        if "shape" in value:
            # A GUI triangulation cache must not change the measured BRep bounds.
            value["shape"]["bounding_box"] = _bound_box_payload(
                obj.Shape.optimalBoundingBox(False, False)
            )
        result.append(value)
    return result


def document_save_copy(params):
    import FreeCAD as App

    tool = "document.save_copy"
    version = _host_version()
    output_path = _required(params, "output_path", tool)
    read_back = _ReadBack(tool, version, params)
    presentation = None
    gui = None
    if params.get("visible_objects") is None and (
        params.get("view", "isometric") != "isometric"
        or params.get("appearances") is not None
        or params.get("frame_margin") is not None
    ):
        raise ValueError("Presentation options require an explicit visible_objects selection")
    if params.get("visible_objects") is not None:
        presentation = _load_sibling_module("presentation.py", "dcc_mcp_freecad_presentation")
        presentation.validate_options(
            params["visible_objects"],
            params.get("view", "isometric"),
            params.get("appearances"),
            params.get("frame_margin"),
        )
        gui = presentation.initialize()
    doc = _open_document(App, params["document_path"])
    try:
        doc.recompute()
        expected = sorted(obj.Name for obj in doc.Objects)
        count = len(doc.Objects)
        expected_objects = _presentation_geometry(doc) if presentation else None
        expected_presentation = None
        if presentation:
            expected_presentation = presentation.apply(
                doc,
                gui,
                params["visible_objects"],
                params.get("view", "isometric"),
                params.get("appearances"),
                params.get("frame_margin"),
            )
            read_back.check(
                presentation.requested_matches(
                    params["visible_objects"],
                    params.get("view", "isometric"),
                    expected_presentation,
                    params.get("appearances"),
                ),
                "copy.presentation_request",
                {
                    "visible_objects": sorted(params["visible_objects"]),
                    "camera_type": "Orthographic",
                    "camera_orientation": presentation.VIEW_ROTATIONS[
                        params.get("view", "isometric")
                    ],
                    "appearances": presentation.validate_options(
                        params["visible_objects"],
                        params.get("view", "isometric"),
                        params.get("appearances"),
                    ),
                },
                expected_presentation,
                "Native visibility, orientation and appearance must match the request.",
            )
        doc.saveAs(output_path)
    finally:
        _close_document(App, doc)
    # Read-back: a copy that cannot be reopened is not a copy. The copy is only
    # reopened once the source is closed, because a save-as target shares
    # document handles with its source on some hosts -- closing the reopened
    # copy invalidates the still-open source, and the other way around.
    read_back.check(
        os.path.isfile(output_path) and os.path.getsize(output_path) > 0,
        "artifact.non_empty",
        "a non-empty .FCStd file",
        _size_or_missing(output_path),
        "FreeCAD reported a successful save but wrote no copy.",
    )
    try:
        copy = App.openDocument(output_path)
    except Exception as exc:
        read_back.fail(
            "copy.readable",
            expected,
            "reopen failed: %s" % exc,
            "The copy was written but cannot be reopened, so it is not a usable copy.",
        )
        raise
    try:
        try:
            actual = sorted(obj.Name for obj in copy.Objects)
        except Exception as exc:
            read_back.fail(
                "copy.readable",
                expected,
                "reopen failed: %s" % exc,
                "The copy was written but cannot be reopened, so it is not a usable copy.",
            )
            raise
        read_back.check(
            actual == expected,
            "copy.objects",
            expected,
            actual,
            "The copy does not contain the source objects; treating it as a "
            "successful copy would lose that silently.",
        )
        if presentation:
            actual_presentation = presentation.inspect(
                copy, gui, [item["object_name"] for item in params.get("appearances") or []]
            )
            read_back.check(
                presentation.matches(expected_presentation, actual_presentation),
                "copy.presentation",
                expected_presentation,
                actual_presentation,
                "The saved native view providers or camera did not survive reopening.",
            )
            actual_objects = _presentation_geometry(copy)
            read_back.check(
                presentation.geometry_matches(expected_objects, actual_objects),
                "copy.geometry",
                expected_objects,
                actual_objects,
                "Recorded object, link, placement, topology counts and geometry must persist.",
            )
    finally:
        _close_document(App, copy)
    return {
        "presentation": expected_presentation,
        "presentation_request": (
            {
                "visible_objects": params["visible_objects"],
                "view": params.get("view", "isometric"),
                "appearances": params.get("appearances"),
                "frame_margin": params.get("frame_margin"),
            }
            if presentation
            else None
        ),
        "object_count": count,
        "object_names": expected,
        "verified": _verified_checks(read_back),
    }


def _gui_library_probe():
    """Report whether this host exposes FreeCADGui, without starting it.

    Importing the module has no side effects; ``showMainWindow`` does. So a
    capability report can say whether rendering is possible at all without paying
    for GUI startup, and ``get_capabilities`` can mark a console-only build as
    limited instead of letting the first ``render_view`` be the discovery.
    """
    try:
        import FreeCADGui  # noqa: F401
    except Exception as exc:
        return {"importable": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"importable": True, "error": None}


def document_render_view(params):
    """Capture the requested view to a PNG, twice, so the frame can be checked.

    The first capture is the requested frame. The second hides every object and
    captures the same camera again, producing what the scene looks like with
    nothing selected. Only the pair lets the caller tell "the model rendered"
    from "the background rendered": FreeCAD's default 3D-view background is a
    linear gradient that is baked into the saved PNG, so an entirely empty
    render already passes any "is it monochrome?" test on variance alone.

    Framing the scene means moving the camera and changing visibility, so the
    native view state is recorded before anything is touched and restored
    afterwards, and that restoration is itself a read-back check. A tool whose
    only job is to look must not leave the view somewhere else.

    The document is opened, recomputed and mutated only in memory. It is never
    saved, so a render cannot change the caller's file.
    """
    import FreeCAD as App

    tool = "document.render_view"
    version = _host_version()
    document_path = _required(params, "document_path", tool)
    subject_path = _required(params, "image_path", tool)
    baseline_path = _required(params, "baseline_path", tool)
    view = params.get("view", "isometric")
    appearances = params.get("appearances")
    frame_margin = params.get("frame_margin")
    read_back = _ReadBack(tool, version, params)
    module = _load_sibling_module("presentation.py", "dcc_mcp_freecad_presentation")
    width, height = module.validate_render_size(
        params["width"] if params.get("width") is not None else module.DEFAULT_RENDER_WIDTH,
        params["height"] if params.get("height") is not None else module.DEFAULT_RENDER_HEIGHT,
    )
    if params.get("visible_objects") is None and (
        appearances is not None or frame_margin is not None
    ):
        raise ValueError("appearances and frame_margin require an explicit visible_objects")
    if not isinstance(view, str) or view not in module.VIEWS:
        raise ValueError("view must be isometric, front, top or right")
    gui = module.initialize()
    doc = _open_document(App, document_path)
    try:
        doc.recompute()
        state = module.view_state(doc, gui)
        restored = None
        try:
            names = params.get("visible_objects")
            if names is None:
                names = module.renderable_names(doc)
                if not names:
                    raise ValueError("The document has no top-level non-container object to render")
            # Validated once, after the selection is resolved, so the default
            # path cannot skip the view and appearance rules the explicit path
            # applies.
            normalized = module.validate_options(names, view, appearances, frame_margin)
            snapshot = module.apply(doc, gui, names, view, appearances, frame_margin)
            read_back.check(
                module.requested_matches(names, view, snapshot, appearances),
                "render.presentation_request",
                {
                    "visible_objects": sorted(names),
                    "camera_type": "Orthographic",
                    "camera_orientation": module.VIEW_ROTATIONS[view],
                    "appearances": normalized,
                },
                snapshot,
                "Native visibility, orientation and appearance must match the request "
                "before a frame is captured from it.",
            )
            active = gui.getDocument(doc.Name).activeView()
            module.capture(active, subject_path, width, height)
            module.hide_all(doc)
            module.capture(active, baseline_path, width, height)
        finally:
            # Runs on every path, including a failed capture: a tool that only
            # looks must give the view back whether or not it succeeded.
            module.restore_view_state(doc, gui, state)
            restored = module.view_state(doc, gui)
        read_back.check(
            module.states_match(state, restored),
            "render.view_state_restored",
            state,
            restored,
            "A read-only render must leave the native camera, visibility and selection "
            "exactly as it found them, compared byte for byte rather than by tolerance.",
        )
        return {
            "view": view,
            "visible_objects": sorted(names),
            "width": width,
            "height": height,
            "presentation": snapshot,
            # Both snapshots are returned as evidence, so the read-only claim is
            # visible to the caller and assertable in a test, not just a comment.
            "view_state_before": state,
            "view_state_after": restored,
            "host_version": version,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def _dependents_recursive(obj, collected):
    for dependent in obj.InList:
        if dependent.Name not in collected:
            collected[dependent.Name] = dependent
            _dependents_recursive(dependent, collected)


def document_remove_object(params):
    import FreeCAD as App

    tool = "document.remove_object"
    version = _host_version()
    doc = _open_document(App, params["document_path"])
    try:
        obj = doc.getObject(params["object_name"])
        if obj is None:
            raise ValueError("Object does not exist: %s" % params["object_name"])
        dependents = {}
        _dependents_recursive(obj, dependents)
        if dependents and not params.get("cascade"):
            raise ValueError(
                "Object has dependents; set cascade=true to remove: %s"
                % ", ".join(sorted(dependents))
            )
        removed = []
        for dependent in reversed(list(dependents.values())):
            dependent_name = dependent.Name
            doc.removeObject(dependent_name)
            removed.append(dependent_name)
        object_name = obj.Name
        doc.removeObject(object_name)
        removed.append(object_name)
        _save_document(doc)
        # Read-back: every name this call claims to have removed must be gone.
        # A removal that silently kept an object leaves the caller modelling on
        # a document that still contains it.
        survivors = [name for name in removed if doc.getObject(name) is not None]
        read_back = _ReadBack(tool, version, params)
        read_back.check(
            not survivors,
            "object.removed",
            "deleted: %s" % ", ".join(removed),
            "still present: %s" % ", ".join(survivors),
            "FreeCAD accepted the removal but the objects survived the save.",
        )
        return {
            "removed_objects": removed,
            "document": _document_payload(doc),
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


_PRIMITIVE_TYPES = {
    "box": "Part::Box",
    "cone": "Part::Cone",
    "cylinder": "Part::Cylinder",
    "sphere": "Part::Sphere",
    "torus": "Part::Torus",
    "wedge": "Part::Wedge",
    "helix": "Part::Helix",
}

_DIMENSION_PROPERTIES = {
    "Part::Box": {"length": "Length", "width": "Width", "height": "Height"},
    "Part::Cylinder": {"radius": "Radius", "height": "Height", "angle": "Angle"},
    "Part::Sphere": {
        "radius": "Radius",
        "angle1": "Angle1",
        "angle2": "Angle2",
        "angle3": "Angle3",
    },
    "Part::Cone": {
        "radius1": "Radius1",
        "radius2": "Radius2",
        "height": "Height",
        "angle": "Angle",
    },
    "Part::Torus": {
        "radius1": "Radius1",
        "radius2": "Radius2",
        "angle1": "Angle1",
        "angle2": "Angle2",
        "angle3": "Angle3",
    },
    # The wedge keeps FreeCAD's own property spelling: it is defined by ten
    # corner coordinates rather than by a length/width/height triple, and
    # inventing a friendlier convention would make the read-back compare a
    # number the host never stored.
    "Part::Wedge": {
        "xmin": "Xmin",
        "ymin": "Ymin",
        "zmin": "Zmin",
        "x2min": "X2min",
        "z2min": "Z2min",
        "xmax": "Xmax",
        "ymax": "Ymax",
        "zmax": "Zmax",
        "x2max": "X2max",
        "z2max": "Z2max",
    },
    "Part::Helix": {
        "pitch": "Pitch",
        "height": "Height",
        "radius": "Radius",
        "angle": "Angle",
        "segment_length": "SegmentLength",
    },
}

_POSITIVE_DIMENSIONS = ("length", "width", "height", "radius", "pitch")
_NON_NEGATIVE_DIMENSIONS = ("radius1", "radius2", "segment_length")

# Helix::execute constrains Angle with the host's apex range, not with the
# 0..360 sweep the revolved primitives use; a cone angle of 90 degrees would put
# the apex at infinity.
_HELIX_ANGLE_LIMIT = 89.9


def _apply_dimensions(obj, dimensions, version):
    allowed = _DIMENSION_PROPERTIES.get(obj.TypeId)
    if allowed is None:
        raise ValueError("Object is not a supported parametric primitive: %s" % obj.TypeId)
    unknown = sorted(set(dimensions) - set(allowed))
    if unknown:
        raise ValueError("Unsupported dimensions for %s: %s" % (obj.TypeId, ", ".join(unknown)))
    for name, value in dimensions.items():
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("%s must be finite" % name)
        if name in _POSITIVE_DIMENSIONS and number <= 0:
            raise ValueError("%s must be positive" % name)
        if name in _NON_NEGATIVE_DIMENSIONS and number < 0:
            raise ValueError("%s must be non-negative" % name)
        if obj.TypeId == "Part::Helix" and name == "angle":
            # The published schema declares ``minimum: 0`` for every dimension,
            # so a negative angle is refused here rather than accepted and
            # silently reaching the host. Refusing keeps the schema honest: a
            # tightening that let a negative through would make the documented
            # bound a lie.
            if not 0 <= number <= _HELIX_ANGLE_LIMIT:
                raise ValueError("%s must be between 0 and %s" % (name, _HELIX_ANGLE_LIMIT))
        elif name in ("angle", "angle3") and not 0 < number <= 360:
            raise ValueError("%s must be greater than 0 and no more than 360" % name)
        if name in ("angle1", "angle2"):
            limit = 90 if obj.TypeId == "Part::Sphere" else 180
            if not -limit <= number <= limit:
                raise ValueError("%s must be between -%s and %s" % (name, limit, limit))
        property_name = _resolve_property(obj, allowed[name], version)
        setattr(obj, property_name, number)
    if obj.TypeId == "Part::Helix":
        # The kernel raises "Number of turns too high" from inside execute();
        # refusing here turns that into a message naming the two dimensions.
        pitch = float(obj.Pitch)
        height = float(obj.Height)
        if pitch > 0 and height / pitch > _MAX_HELIX_TURNS:
            raise ValueError("helix height/pitch must not exceed %d turns" % int(_MAX_HELIX_TURNS))


def _verify_dimensions(read_back, obj, dimensions, prefix="dimension"):
    """Read every requested dimension back off the object.

    This is the check that turns a clamped, ignored or mis-ordered dimension into
    an error: the value is read from the object after the save, not echoed from
    the request.
    """
    allowed = _DIMENSION_PROPERTIES.get(obj.TypeId) or {}
    for name, value in dimensions.items():
        property_name = allowed.get(name, name)
        actual = getattr(obj, property_name, None)
        read_back.numbers(
            "%s.%s" % (prefix, property_name),
            float(value),
            actual,
            "FreeCAD accepted the write but stored a different value, so the "
            "geometry is not the geometry that was asked for.",
        )


def _read_back_primitive(read_back, doc, name, dimensions, label=None):
    """Read a primitive back off the document and prove the write landed."""
    obj = doc.getObject(name)
    read_back.exists("object", name, obj)
    _verify_dimensions(read_back, obj, dimensions)
    if label is not None:
        read_back.check(
            obj.Label == str(label),
            "object.label",
            str(label),
            obj.Label,
            "The label was not persisted.",
        )
    read_back.shape("object", obj)
    return obj


def model_add_primitive(params):
    import FreeCAD as App

    tool = "model.add_primitive"
    version = _host_version()
    primitive = _required(params, "primitive", tool)
    if primitive not in _PRIMITIVE_TYPES:
        raise ValueError("Unsupported primitive: %s" % primitive)
    name = _required(params, "name", tool)
    dimensions = _dimensions(params, tool)
    label = params.get("label")
    doc = _open_document(App, params["document_path"])
    try:
        if doc.getObject(name) is not None:
            raise ValueError("Object already exists: %s" % name)
        obj = doc.addObject(_PRIMITIVE_TYPES[primitive], name)
        if label:
            obj.Label = str(label)
        _apply_dimensions(obj, dimensions, version)
        translation = _vector(params.get("translation") or [0, 0, 0], "translation")
        placement = App.Placement(
            App.Vector(*translation),
            _rotation(
                App,
                params.get("rotation_axis") or [0, 0, 1],
                params.get("rotation_degrees") or 0,
            ),
        )
        obj.Placement = placement
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = _read_back_primitive(read_back, doc, name, dimensions, label)
        read_back.check(
            stored.TypeId == _PRIMITIVE_TYPES[primitive],
            "object.type_id",
            _PRIMITIVE_TYPES[primitive],
            stored.TypeId,
            "The object was created as a different type than requested.",
        )
        read_back.placement("object", App, placement, stored.Placement)
        return {
            "object": _object_payload(stored),
            "document": _document_payload(doc),
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def model_update_primitive(params):
    import FreeCAD as App

    tool = "model.update_primitive"
    version = _host_version()
    object_name = _required(params, "object_name", tool)
    dimensions = _dimensions(params, tool)
    label = params.get("label")
    doc = _open_document(App, params["document_path"])
    try:
        obj = doc.getObject(object_name)
        if obj is None:
            raise ValueError("Object does not exist: %s" % object_name)
        _apply_dimensions(obj, dimensions, version)
        if label is not None:
            obj.Label = str(label)
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = _read_back_primitive(read_back, doc, object_name, dimensions, label)
        return {"object": _object_payload(stored), "verified": _verified_checks(read_back)}
    finally:
        _close_document(App, doc)


def model_transform_object(params):
    import FreeCAD as App

    tool = "model.transform_object"
    version = _host_version()
    object_name = _required(params, "object_name", tool)
    doc = _open_document(App, params["document_path"])
    try:
        obj = doc.getObject(object_name)
        if obj is None or not hasattr(obj, "Placement"):
            raise ValueError("Placeable object does not exist: %s" % object_name)
        placement = App.Placement(
            App.Vector(*_vector(_required(params, "translation", tool), "translation")),
            _rotation(
                App,
                params.get("rotation_axis") or [0, 0, 1],
                params.get("rotation_degrees") or 0,
            ),
        )
        obj.Placement = placement
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(object_name)
        read_back.exists("object", object_name, stored)
        read_back.placement("object", App, placement, stored.Placement)
        return {"object": _object_payload(stored), "verified": _verified_checks(read_back)}
    finally:
        _close_document(App, doc)


def _new_object_name(doc, name, tool):
    """Refuse a name that is taken instead of silently replacing the object.

    A name collision inside a document means replacing whatever holds it, and
    that object may be an operand of something else. The caller's way forward is
    explicit: remove the old object first, or pick another name.
    """
    if doc.getObject(name) is not None:
        raise ValueError("%s: object already exists: %s" % (tool, name))
    return name


def _assert_document_recomputed(read_back, doc, tool):
    """Prove the whole document recomputes into valid shapes after this write.

    A mirror is the operation that can quietly produce degenerate geometry: the
    feature exists, its shape is non-null, and the kernel only reports the
    problem when the document is validated. Validating here means a caller that
    asked for a mirror never has to remember to run ``validate_document`` to
    find out whether the mirror is usable.
    """
    doc.recompute()
    invalid, empty = _invalid_shape_names(doc)
    read_back.check(
        not invalid and not empty,
        "document.recomputed_valid",
        "no invalid or empty shapes",
        {"invalid_objects": invalid, "empty_shape_objects": empty},
        "The write produced geometry the kernel will not accept, so the "
        "document is not in a usable state.",
    )
    return {"invalid_objects": invalid, "empty_shape_objects": empty}


def _bbox_extent(box):
    return [box.XLength, box.YLength, box.ZLength]


def _bbox_centre(box):
    return [box.Center.x, box.Center.y, box.Center.z]


def model_scale_object(params):
    """Create a scaled copy of an object's shape under ``result_name``.

    The result is a plain ``Part::Feature``: a non-uniform scale of a
    ``Part::Box`` is no longer a box, so the parametric definition cannot
    survive, and FreeCAD silently ignores a ``Shape`` assignment on a parametric
    primitive anyway - the write returns and the geometry is unchanged. Producing
    a new object is the only form of this operation that can be proven.
    """
    import FreeCAD as App

    tool = "model.scale_object"
    version = _host_version()
    object_name = _required(params, "object_name", tool)
    result_name = _required(params, "result_name", tool)
    factors = _scale_factors(_required(params, "scale", tool), tool)
    around = params.get("around") or "centroid"
    result_label = params.get("result_label")
    doc = _open_document(App, params["document_path"])
    try:
        source = doc.getObject(object_name)
        if source is None:
            raise ValueError("Object does not exist: %s" % object_name)
        source_shape = _shape_or_error(source, object_name, tool)
        # Measured before the write and kept as plain numbers: the read-back
        # must not depend on a live TopoShape the recompute could still move.
        source_extent = _bbox_extent(source_shape.BoundBox)
        source_centre = _bbox_centre(source_shape.BoundBox)
        expected_extent = [value * factor for value, factor in zip(source_extent, factors)]
        expected_centre = (
            [value * factor for value, factor in zip(source_centre, factors)]
            if around == "origin"
            else source_centre
        )
        _new_object_name(doc, result_name, tool)
        scaled = _scaled_shape(App, source_shape, factors, around, tool)
        result = doc.addObject("Part::Feature", result_name)
        if result_label:
            result.Label = str(result_label)
        result.Shape = scaled
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(result_name)
        read_back.exists("result", result_name, stored)
        read_back.check(
            stored.TypeId == "Part::Feature",
            "result.type_id",
            "Part::Feature",
            stored.TypeId,
            "The scaled result is not the shape holder the tool creates.",
        )
        if result_label:
            read_back.check(
                stored.Label == str(result_label),
                "result.label",
                str(result_label),
                stored.Label,
                "The result label was not persisted.",
            )
        shape = read_back.shape("result", stored)
        _verify_box(read_back, "result", getattr(shape, "BoundBox", None))
        # The extent is the scale: a factor that was dropped, clamped or applied
        # to the wrong axis shows up here as a box of the wrong size.
        read_back.sequences(
            "result.bounding_box.size",
            expected_extent,
            _bbox_extent(shape.BoundBox),
            "The scaled geometry is not the requested multiple of the source, so "
            "the scale did not apply as asked.",
        )
        read_back.sequences(
            "result.bounding_box.center",
            expected_centre,
            _bbox_centre(shape.BoundBox),
            "Scaling about the origin must move the centre by the same factors; "
            "scaling about the centroid must leave it where it was.",
        )
        _assert_document_recomputed(read_back, doc, tool)
        return {
            "object": _object_payload(stored),
            "source_object": object_name,
            "scale": factors,
            "around": around,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def model_copy_object(params):
    """Duplicate an object under ``new_name``, optionally at a new placement.

    A primitive is re-created as the same parametric type with the same
    dimensions, so the copy stays editable. Anything else - an imported solid, a
    boolean result, a mesh - is duplicated as a shape or mesh copy, because
    there is no parametric definition to carry over.

    ``translation`` is absolute for every source type, not relative to the
    source: a copy made without it lands at the document origin, the same as a
    primitive copy does. The shape and mesh branches copy geometry FreeCAD
    reports in document coordinates, so it is moved back onto its own bounding
    box first; only then does the requested translation mean the same thing it
    means for a primitive re-created from its own dimensions.

    ``rotation_degrees`` is absolute only for a primitive, which is re-created
    from its dimensions and so carries no orientation of its own. For a shape or
    mesh copy the request composes on top of the orientation already baked into
    the copied geometry: a boolean result and a mesh hold their orientation in
    their coordinates with an identity ``Placement``, so there is nothing for the
    rebase to invert and the source's rotation cannot be recovered. A caller that
    needs a known orientation for those sources should copy a primitive, or
    rotate the source before copying it.
    """
    import FreeCAD as App

    tool = "model.copy_object"
    version = _host_version()
    object_name = _required(params, "object_name", tool)
    new_name = _required(params, "new_name", tool)
    label = params.get("label")
    doc = _open_document(App, params["document_path"])
    try:
        source = doc.getObject(object_name)
        if source is None:
            raise ValueError("Object does not exist: %s" % object_name)
        _new_object_name(doc, new_name, tool)
        placement = App.Placement(
            App.Vector(*_vector(params.get("translation") or [0, 0, 0], "translation")),
            _rotation(
                App,
                params.get("rotation_axis") or [0, 0, 1],
                params.get("rotation_degrees") or 0,
            ),
        )
        dimensions = _DIMENSION_PROPERTIES.get(source.TypeId)
        mesh = getattr(source, "Mesh", None)
        copied_dimensions = {}
        if dimensions is not None:
            result = doc.addObject(source.TypeId, new_name)
            for name, property_name in dimensions.items():
                if not hasattr(source, property_name):
                    continue
                value = getattr(source, property_name)
                copied_dimensions[name] = float(value)
                setattr(result, property_name, value)
        elif mesh is not None and getattr(mesh, "CountPoints", 0):
            result = doc.addObject("Mesh::Feature", new_name)
            # The mesh is transformed in place, so it is assigned after the
            # call rather than from its return value.
            copied = mesh.copy()
            _move_to_local(copied, source)
            result.Mesh = copied
        else:
            result = doc.addObject("Part::Feature", new_name)
            # transformGeometry returns a new shape, so the rebound copy is
            # what has to be stored - assigning the original would undo the
            # rebase and leave translation relative to the source again.
            result.Shape = _move_to_local(_shape_or_error(source, object_name, tool).copy(), source)
        if label:
            result.Label = str(label)
        if hasattr(result, "Placement"):
            result.Placement = placement
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(new_name)
        read_back.exists("copy", new_name, stored)
        expected_type = source.TypeId if dimensions is not None else result.TypeId
        read_back.check(
            stored.TypeId == expected_type,
            "copy.type_id",
            expected_type,
            stored.TypeId,
            "The copy was created as a different type than the source, so it is "
            "not a copy of this object.",
        )
        if dimensions is not None:
            _verify_dimensions(read_back, stored, copied_dimensions, prefix="copy.dimension")
        if label:
            read_back.check(
                stored.Label == str(label),
                "copy.label",
                str(label),
                stored.Label,
                "The copy label was not persisted.",
            )
        if hasattr(stored, "Placement"):
            read_back.placement("copy", App, placement, stored.Placement)
        copied_mesh = getattr(stored, "Mesh", None)
        if copied_mesh is not None and getattr(copied_mesh, "CountPoints", 0):
            read_back.check(
                copied_mesh.CountPoints == mesh.CountPoints,
                "copy.mesh_points",
                mesh.CountPoints,
                copied_mesh.CountPoints,
                "The copied mesh does not carry the source geometry.",
            )
            _verify_box(read_back, "copy", getattr(copied_mesh, "BoundBox", None))
        else:
            shape = read_back.shape("copy", stored)
            _verify_box(read_back, "copy", getattr(shape, "BoundBox", None))
        # A copy leaves the source alone; a tool that quietly consumed it would
        # make "duplicate to a second mounting position" destructive.
        read_back.check(
            doc.getObject(object_name) is not None,
            "copy.source_preserved",
            object_name,
            None,
            "The source object is gone after the copy, so this was a move.",
        )
        _assert_document_recomputed(read_back, doc, tool)
        return {
            "object": _object_payload(stored),
            "source_object": object_name,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def model_mirror_object(params):
    """Mirror an object's shape about a plane into ``result_name``.

    ``keep_source`` decides the shape of the result, not just whether the source
    survives: a parametric ``Part::Mirroring`` holds a link to its source, so it
    cannot outlive it. Keeping the source therefore yields a live
    ``Part::Mirroring`` that follows later edits; dropping it bakes the mirrored
    geometry into a plain ``Part::Feature`` and removes the source.
    """
    import FreeCAD as App

    tool = "model.mirror_object"
    version = _host_version()
    object_name = _required(params, "object_name", tool)
    result_name = _required(params, "result_name", tool)
    normal, origin = _mirror_plane(params, tool)
    keep_source = bool(params.get("keep_source", True))
    result_label = params.get("result_label")
    doc = _open_document(App, params["document_path"])
    try:
        source = doc.getObject(object_name)
        if source is None:
            raise ValueError("Object does not exist: %s" % object_name)
        source_shape = _shape_or_error(source, object_name, tool)
        source_extent = _bbox_extent(source_shape.BoundBox)
        source_volume = source_shape.Volume
        source_centre = _bbox_centre(source_shape.BoundBox)
        _new_object_name(doc, result_name, tool)
        point = App.Vector(*origin)
        axis = App.Vector(*normal)
        if not keep_source:
            dependents = {}
            _dependents_recursive(source, dependents)
            if dependents:
                raise ValueError(
                    "Object has dependents; keep_source=false cannot remove it: %s"
                    % ", ".join(sorted(dependents))
                )
        # Part::Mirroring is the only mirror the two supported release lines
        # agree on. Measured with a box of length 10 placed at x=100 and the
        # plane through (100, 0, 0) with normal +X: 1.1.4 reflects it to
        # x 90..100 (correct) while 1.0.2 returns x 190..200, so the same
        # Shape.mirror() call mirrors about two different planes depending on
        # the host and both answers look like a success. Building the feature,
        # and baking its shape only when the source is not kept, keeps both
        # branches on one verified code path.
        staging_name = result_name if keep_source else "DccMcpMirrorStage"
        mirror = doc.addObject("Part::Mirroring", staging_name)
        # Internal names are unique per document, so a staging name that is
        # taken makes addObject rename the feature it just created. Reading the
        # name back off the object keeps the removal below pointed at the
        # feature this call made; removing the requested string instead would
        # delete the caller's object of that name and leave this one behind.
        staging_name = mirror.Name
        try:
            mirror.Source = source
            mirror.Normal = axis
            mirror.Base = point
            doc.recompute()
            if not keep_source:
                baked = mirror.Shape.copy()
        finally:
            if not keep_source:
                doc.removeObject(staging_name)
        if keep_source:
            result = mirror
            if result_label:
                result.Label = str(result_label)
        else:
            result = doc.addObject("Part::Feature", result_name)
            if result_label:
                result.Label = str(result_label)
            result.Shape = baked
            # The baked shape is a copy, so the source can go as soon as the
            # result holds its geometry. Removing it before the result exists
            # would leave the document without either if the assignment failed.
            doc.removeObject(object_name)
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(result_name)
        read_back.exists("result", result_name, stored)
        expected_type = "Part::Mirroring" if keep_source else "Part::Feature"
        read_back.check(
            stored.TypeId == expected_type,
            "result.type_id",
            expected_type,
            stored.TypeId,
            "The mirror is not the kind of object this request produces.",
        )
        if keep_source:
            linked = getattr(stored, "Source", None)
            read_back.check(
                linked is not None and linked.Name == object_name,
                "result.source",
                object_name,
                getattr(linked, "Name", None),
                "The mirror is not wired to the object that was mirrored.",
            )
        if result_label:
            read_back.check(
                stored.Label == str(result_label),
                "result.label",
                str(result_label),
                stored.Label,
                "The result label was not persisted.",
            )
        shape = read_back.shape("result", stored)
        _verify_box(read_back, "result", getattr(shape, "BoundBox", None))
        # A mirror is an isometry: same volume, and a bounding box that is the
        # reflection of the source's. Either one alone would pass a mirror that
        # only translated; together they pin the plane.
        read_back.sequences(
            "result.bounding_box.size",
            source_extent,
            _bbox_extent(shape.BoundBox),
            "A mirror preserves the extent of the source geometry.",
        )
        if abs(source_volume) > 1e-9:
            read_back.numbers(
                "result.volume",
                source_volume,
                shape.Volume,
                "A mirror preserves volume; a different volume means the wrong "
                "geometry (or none) was produced.",
                rel_tolerance=1e-6,
            )
        expected_centre = _reflected_point(source_centre, point, axis)
        read_back.sequences(
            "result.bounding_box.center",
            expected_centre,
            _bbox_centre(shape.BoundBox),
            "The mirrored geometry is not the reflection of the source about the requested plane.",
        )
        if not keep_source:
            read_back.check(
                doc.getObject(object_name) is None,
                "result.source_removed",
                "removed: %s" % object_name,
                "still present",
                "keep_source=false was requested but the source survived.",
            )
        # Acceptance: a mirrored Part solid must recompute and validate.
        validation = _assert_document_recomputed(read_back, doc, tool)
        return {
            "object": _object_payload(stored),
            "source_object": object_name,
            "keep_source": keep_source,
            "document_validation": validation,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def _reflected_point(centre, point, axis):
    """Reflect ``centre`` through the plane through ``point`` with normal ``axis``.

    ``p' = p - 2 * ((p - point) . n_hat) * n_hat``
    """
    length = (axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2]) ** 0.5
    unit = [axis[0] / length, axis[1] / length, axis[2] / length]
    offset = sum((centre[i] - point[i]) * unit[i] for i in range(3))
    return [centre[i] - 2.0 * offset * unit[i] for i in range(3)]


_VOLUME_RELATIONS = {
    # A union can never be smaller than its largest operand, a cut can never be
    # larger than the base, and an intersection can never be larger than its
    # smallest operand. A result outside its relation means the operation did
    # not run even though the feature exists.
    "union": (
        "result volume >= max(base volume, tool volume)",
        lambda b, t, r, tol: r >= max(b, t) - tol,
    ),
    "cut": ("result volume <= base volume", lambda b, t, r, tol: r <= b + tol),
    "intersection": (
        "result volume <= min(base volume, tool volume)",
        lambda b, t, r, tol: r <= min(b, t) + tol,
    ),
}


def model_boolean_operation(params):
    import FreeCAD as App

    tool = "model.boolean_operation"
    version = _host_version()
    mapping = {
        "union": "Part::Fuse",
        "cut": "Part::Cut",
        "intersection": "Part::Common",
    }
    operation = _required(params, "operation", tool)
    if operation not in mapping:
        raise ValueError("Unsupported boolean operation: %s" % operation)
    type_id = mapping[operation]
    base_name = _required(params, "base_object", tool)
    tool_name = _required(params, "tool_object", tool)
    result_name = _required(params, "result_name", tool)
    result_label = params.get("result_label")
    doc = _open_document(App, params["document_path"])
    try:
        base = doc.getObject(base_name)
        operand = doc.getObject(tool_name)
        if base is None or operand is None:
            raise ValueError("Both boolean operands must exist")
        if not hasattr(base, "Shape") or not hasattr(operand, "Shape"):
            raise ValueError("Boolean operands must have Part shapes")
        if doc.getObject(result_name) is not None:
            raise ValueError("Result object already exists: %s" % result_name)
        result = doc.addObject(type_id, result_name)
        result.Base = base
        result.Tool = operand
        if result_label:
            result.Label = str(result_label)
        doc.recompute()
        if result.Shape.isNull() or not result.Shape.isValid():
            raise ValueError("Boolean operation produced an invalid or empty shape")
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(result_name)
        read_back.exists("result", result_name, stored)
        read_back.check(
            stored.TypeId == type_id,
            "result.type_id",
            type_id,
            stored.TypeId,
            "The result is not the operation that was requested.",
        )
        # The wiring is the operation: a Fuse whose Base/Tool links did not take
        # recomputes to something else and still looks like a success.
        for link, expected_name in (("Base", base_name), ("Tool", tool_name)):
            linked = getattr(stored, link, None)
            read_back.check(
                linked is not None and linked.Name == expected_name,
                "result.%s" % link.lower(),
                expected_name,
                getattr(linked, "Name", None),
                "The boolean result is not wired to the requested operand, so it "
                "does not represent this operation.",
            )
        if result_label:
            read_back.check(
                stored.Label == str(result_label),
                "result.label",
                str(result_label),
                stored.Label,
                "The result label was not persisted.",
            )
        shape = read_back.shape("result", stored)
        if operation == "union":
            read_back.check(
                len(shape.Solids) >= 1,
                "result.solids",
                "at least one solid",
                len(shape.Solids),
                "A union of two solids cannot be empty.",
            )
        volumes = {
            "base": base.Shape.Volume,
            "tool": operand.Shape.Volume,
            "result": shape.Volume,
        }
        relation_text, holds = _VOLUME_RELATIONS[operation]
        # Tolerance scales with the operands: it absorbs floating point noise in
        # the solid kernel, not a missing operation.
        tolerance = 1e-6 * max(1.0, abs(volumes["base"]), abs(volumes["tool"]))
        read_back.check(
            holds(volumes["base"], volumes["tool"], volumes["result"], tolerance),
            "result.volume_relation",
            relation_text,
            volumes,
            "The boolean feature exists but its volume does not relate to its "
            "operands the way %s must, so the operation did not run." % operation,
        )
        return {
            "object": _object_payload(stored),
            "operation": operation,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def model_import_geometry(params):
    import FreeCAD as App

    tool = "model.import_geometry"
    version = _host_version()
    input_path = _required(params, "input_path", tool)
    object_name = _required(params, "object_name", tool)
    label = params.get("label")
    suffix = input_path.lower().rsplit(".", 1)[-1]
    is_mesh = suffix in _MESH_SUFFIXES
    doc = _open_document(App, params["document_path"])
    try:
        read_back = _ReadBack(tool, version, params)
        if doc.getObject(object_name) is not None:
            raise ValueError("Object already exists: %s" % object_name)
        read_back.check(
            os.path.isfile(input_path) and os.path.getsize(input_path) > 0,
            "input.non_empty",
            "a non-empty geometry file",
            _size_or_missing(input_path),
            "There is nothing to import, so an imported object would be reported "
            "without having read anything.",
        )
        expected_solids = None
        expected_volume = None
        expected_points = None
        expected_facets = None
        source_box = None
        if is_mesh:
            import Mesh

            # Read and reject the source before creating an object for it, so a
            # failed import leaves no half-initialised object behind.
            mesh = Mesh.Mesh(input_path)
            if not getattr(mesh, "CountPoints", 0):
                raise ValueError("Imported mesh is empty")
            source_box = getattr(mesh, "BoundBox", None)
            # A mesh format stores vertices and facets verbatim, so the counts
            # are exact round-trip quantities rather than measurements: losing
            # facets while staying inside the source box is a real import bug
            # that containment alone cannot see.
            expected_points = int(getattr(mesh, "CountPoints", 0) or 0)
            expected_facets = int(getattr(mesh, "CountFacets", 0) or 0)
            obj = doc.addObject("Mesh::Feature", object_name)
            obj.Mesh = mesh
        else:
            import Part

            shape = Part.read(input_path)
            if shape.isNull():
                raise ValueError("Imported Part shape is empty")
            # Measured from the file that was actually read, before the object
            # exists: an import only counts once the landed object carries the
            # geometry the source held. A STEP/BREP reader that drops a solid
            # still returns a non-null shape, so existence alone cannot tell.
            expected_solids = len(shape.Solids)
            expected_volume = shape.Volume
            obj = doc.addObject("Part::Feature", object_name)
            obj.Shape = shape
        if label:
            obj.Label = str(label)
        _save_document(doc)
        stored = doc.getObject(object_name)
        read_back.exists("object", object_name, stored)
        if label:
            read_back.check(
                stored.Label == str(label),
                "object.label",
                str(label),
                stored.Label,
                "The imported object label was not persisted.",
            )
        if is_mesh:
            mesh = getattr(stored, "Mesh", None)
            points = int(getattr(mesh, "CountPoints", 0) or 0)
            facets = int(getattr(mesh, "CountFacets", 0) or 0)
            read_back.check(
                points > 0 and facets > 0,
                "mesh.non_empty",
                {"points": ">0", "facets": ">0"},
                {"points": points, "facets": facets},
                "The mesh object was created but carries no geometry after the save.",
            )
            read_back.check(
                (points, facets) == (expected_points, expected_facets),
                "source.mesh_counts",
                {"points": expected_points, "facets": expected_facets},
                {"points": points, "facets": facets},
                "The imported mesh holds a different point or facet count than "
                "the source file, so part of the import was dropped.",
            )
            landed_box = getattr(mesh, "BoundBox", None)
            if source_box is not None:
                read_back.check(
                    _box_contains(source_box, landed_box, _MESH_IMPORT_SLACK),
                    "source.bounding_box",
                    "inside the source envelope expanded by %r" % _MESH_IMPORT_SLACK,
                    {
                        "source": _bound_box_payload(source_box),
                        "imported": _bound_box_payload(landed_box),
                    },
                    "The imported mesh lies outside the source file's envelope, "
                    "which means the wrong geometry (or only part of it) landed.",
                )
            _verify_box(read_back, "mesh", landed_box)
        else:
            shape = read_back.shape("object", stored)
            read_back.check(
                len(shape.Solids) == expected_solids,
                "source.solids",
                expected_solids,
                len(shape.Solids),
                "The imported object holds a different solid count than the "
                "source file, so part of the import was dropped.",
            )
            read_back.numbers(
                "source.volume",
                expected_volume,
                shape.Volume,
                "The imported object holds a different volume than the source "
                "file, so the import is not the geometry that was read.",
                rel_tolerance=1e-3,
            )
            _verify_box(read_back, "object", getattr(shape, "BoundBox", None))
        return {
            "object": _object_payload(stored),
            "input_path": input_path,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def model_insert_part(params):
    """Insert a standard part from the offline library into a document.

    The path was already containment-checked by the bridge against the
    configured library roots; what this method owes is the same proof every
    other mutating tool owes: the object is there after the save, it carries
    the geometry that was read, and it sits where it was placed. A library
    entry that imports to an empty or invalid shape is refused here rather than
    handed back as a plausible-looking insert.
    """
    import FreeCAD as App

    tool = "model.insert_part"
    version = _host_version()
    part_path = _required(params, "part_path", tool)
    object_name = _required(params, "object_name", tool)
    label = params.get("label")
    suffix = part_path.lower().rsplit(".", 1)[-1]
    is_mesh = suffix in ("stl", "obj")
    doc = _open_document(App, params["document_path"])
    try:
        read_back = _ReadBack(tool, version, params)
        if doc.getObject(object_name) is not None:
            raise ValueError("Object already exists: %s" % object_name)
        read_back.check(
            os.path.isfile(part_path) and os.path.getsize(part_path) > 0,
            "part.non_empty",
            "a non-empty library file",
            _size_or_missing(part_path),
            "There is nothing to insert, so an inserted object would be reported "
            "without having read anything.",
        )
        if is_mesh:
            import Mesh

            mesh = Mesh.Mesh(part_path)
            if not getattr(mesh, "CountPoints", 0):
                raise ValueError("Library part produced an empty mesh")
            obj = doc.addObject("Mesh::Feature", object_name)
            obj.Mesh = mesh
        else:
            import Part

            shape = Part.read(part_path)
            if shape.isNull():
                raise ValueError("Library part produced an empty shape: %s" % part_path)
            obj = doc.addObject("Part::Feature", object_name)
            obj.Shape = shape
        if label:
            obj.Label = str(label)
        placement = App.Placement(
            App.Vector(*_vector(params.get("translation") or [0, 0, 0], "translation")),
            _rotation(
                App,
                params.get("rotation_axis") or [0, 0, 1],
                params.get("rotation_degrees") or 0,
            ),
        )
        obj.Placement = placement
        _save_document(doc)
        stored = doc.getObject(object_name)
        read_back.exists("object", object_name, stored)
        if label:
            read_back.check(
                stored.Label == str(label),
                "object.label",
                str(label),
                stored.Label,
                "The inserted object label was not persisted.",
            )
        if is_mesh:
            mesh = getattr(stored, "Mesh", None)
            points = int(getattr(mesh, "CountPoints", 0) or 0)
            facets = int(getattr(mesh, "CountFacets", 0) or 0)
            read_back.check(
                points > 0 and facets > 0,
                "mesh.non_empty",
                {"points": ">0", "facets": ">0"},
                {"points": points, "facets": facets},
                "The object was created but carries no geometry after the save.",
            )
            _verify_box(read_back, "mesh", getattr(mesh, "BoundBox", None))
        else:
            shape = read_back.shape("object", stored)
            _verify_box(read_back, "object", getattr(shape, "BoundBox", None))
        read_back.placement("object", App, placement, stored.Placement)
        return {
            "object": _object_payload(stored),
            "part_path": part_path,
            "part_ref": params.get("part_ref"),
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def _assert_tessellation(mesh_obj, source_name, version):
    """Assert tessellation produced a real, bounded mesh instead of a silent one.

    MeshPart deflection behaviour moved between FreeCAD 1.0 and 1.1. A changed
    deflection is only visible as a degenerate mesh, so the export asserts the
    result and names the host version instead of writing a plausible file.
    """
    mesh = getattr(mesh_obj, "Mesh", None)
    points = int(getattr(mesh, "CountPoints", 0) or 0)
    facets = int(getattr(mesh, "CountFacets", 0) or 0)
    empty = points <= 0 or facets <= 0
    unbounded = _is_unbounded(mesh)
    if empty or unbounded:
        entry = _breaking_change_for_symbol("MeshPart.meshFromShape", version) or {}
        reason = (
            "an empty mesh (%d points, %d facets)" % (points, facets)
            if empty
            else "an unbounded mesh (no finite bounding box)"
        )
        raise IncompatibleHostError(
            "FreeCAD %s tessellated %s into %s. Refusing to export a degenerate mesh. %s"
            % (
                version,
                source_name,
                reason,
                entry.get("remediation")
                or "Increase the deflection or repair the shape, then retry.",
            )
        )
    return mesh


def _is_unbounded(mesh):
    """True when the mesh exposes a bounding box that is missing or not finite.

    A host that does not report a bounding box is not treated as unbounded: the
    assertion only fails on a box that exists and is demonstrably unusable, so a
    thinner host API cannot turn this guard into a false rejection.
    """
    box = getattr(mesh, "BoundBox", None)
    if box is None:
        return False
    if getattr(box, "isValid", None) is not None and not box.isValid():
        return True
    for axis in ("XLength", "YLength", "ZLength"):
        value = getattr(box, axis, None)
        if value is None:
            return False
        if not math.isfinite(float(value)) or float(value) < 0:
            return True
    return False


def _declared_3mf_unit(path):
    """Read the unit the 3MF model part declares, or None if it declares none.

    3MF is a zip container, so the declaration lives in the model part rather
    than at a fixed byte offset. ``None`` is returned instead of raising so the
    caller reports it as an expected/actual mismatch: "declared nothing" is the
    fact the caller needs, not a parse failure.
    """
    import re
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            model = archive.read(_3MF_MODEL_PART).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - any container problem is "declared nothing"
        return None
    match = re.search(r"<model[^>]*\bunit=\"([^\"]+)\"", model)
    return match.group(1) if match else None


def model_export_geometry(params):
    import FreeCAD as App

    tool = "model.export_geometry"
    version = _host_version()
    output_path = _required(params, "output_path", tool)
    suffix = output_path.lower().rsplit(".", 1)[-1]
    is_mesh = suffix in _MESH_SUFFIXES
    doc = _open_document(App, params["document_path"])
    temp_meshes = []
    try:
        objects = []
        for name in _required(params, "object_names", tool):
            obj = doc.getObject(name)
            if obj is None:
                raise ValueError("Object does not exist: %s" % name)
            objects.append(obj)
        read_back = _ReadBack(tool, version, params)
        # Measured before tessellation: the exported mesh must stay inside the
        # source envelope, so the envelope has to be read from the sources.
        source_box = _source_box(objects) if is_mesh else None
        expected_solids = None
        expected_volume = None
        linear_deflection = None
        if is_mesh:
            import Mesh
            import MeshPart

            # ``or`` would swallow an explicit 0 and substitute the default,
            # which is the silent-parameter behaviour this contract forbids: a
            # deflection of 0 is meaningless, so it is refused rather than
            # quietly replaced by 0.1. Only an absent parameter defaults.
            linear_deflection = _positive(
                params["linear_deflection"] if params.get("linear_deflection") is not None else 0.1,
                "linear_deflection",
            )
            angular_deflection = math.radians(
                _positive(
                    params["angular_deflection_degrees"]
                    if params.get("angular_deflection_degrees") is not None
                    else 15,
                    "angular_deflection_degrees",
                )
            )
            mesh_objects = []
            for index, obj in enumerate(objects):
                if hasattr(obj, "Mesh") and obj.Mesh.CountPoints:
                    mesh_objects.append(obj)
                elif hasattr(obj, "Shape") and not obj.Shape.isNull():
                    mesh_obj = doc.addObject("Mesh::Feature", "DccMcpExportMesh%d" % index)
                    mesh_obj.Mesh = MeshPart.meshFromShape(
                        Shape=obj.Shape,
                        LinearDeflection=linear_deflection,
                        AngularDeflection=angular_deflection,
                        Relative=False,
                    )
                    # Register for cleanup before asserting, so a refused
                    # export still removes the throwaway mesh object.
                    temp_meshes.append(mesh_obj)
                    _assert_tessellation(mesh_obj, obj.Name, version)
                    mesh_objects.append(mesh_obj)
                else:
                    raise ValueError("Object cannot be meshed: %s" % obj.Name)
            Mesh.export(mesh_objects, output_path)
        else:
            import Part

            if any(not hasattr(obj, "Shape") or obj.Shape.isNull() for obj in objects):
                raise ValueError("STEP/IGES/BREP exports require Part shape objects")
            expected_solids = sum(len(obj.Shape.Solids) for obj in objects)
            expected_volume = sum(obj.Shape.Volume for obj in objects)
            Part.export(objects, output_path)
        # Read-back: an export only counts once the artefact exists, is not
        # empty, and can be read back into the very geometry it came from.
        read_back.check(
            os.path.isfile(output_path) and os.path.getsize(output_path) > 0,
            "artifact.non_empty",
            "a non-empty export",
            _size_or_missing(output_path),
            "FreeCAD reported a successful export but wrote no file.",
        )
        if is_mesh:
            import Mesh

            try:
                mesh = Mesh.Mesh(output_path)
            except Exception as exc:
                read_back.fail(
                    "artifact.readable",
                    "an export FreeCAD can read back",
                    "read error: %s" % exc,
                    "The file was written but is not a readable mesh.",
                )
                raise  # unreachable, keeps the control flow explicit
            points = int(mesh.CountPoints)
            facets = int(mesh.CountFacets)
            read_back.check(
                points > 0 and facets > 0,
                "artifact.mesh_non_empty",
                {"points": ">0", "facets": ">0"},
                {"points": points, "facets": facets},
                "The exported mesh has no geometry, so the export wrote a shell.",
            )
            if source_box is not None:
                read_back.check(
                    _box_contains(source_box, mesh.BoundBox, linear_deflection + 1e-6),
                    "artifact.bounding_box",
                    "inside the source envelope expanded by linear_deflection",
                    {
                        "source": _bound_box_payload(source_box),
                        "exported": _bound_box_payload(mesh.BoundBox),
                    },
                    "The exported mesh lies outside the exported objects, which "
                    "means the wrong geometry (or none) was written.",
                )
            if suffix == "3mf":
                unit = _declared_3mf_unit(output_path)
                read_back.check(
                    unit == _3MF_REQUIRED_UNIT,
                    "artifact.unit",
                    _3MF_REQUIRED_UNIT,
                    unit,
                    "A 3MF consumer scales the model by the declared unit, so an "
                    "export without the millimetre declaration prints at the "
                    "wrong size.",
                )
        else:
            import Part

            try:
                shape = Part.read(output_path)
            except Exception as exc:
                read_back.fail(
                    "artifact.readable",
                    "an export FreeCAD can read back",
                    "read error: %s" % exc,
                    "The file was written but is not readable geometry.",
                )
                raise  # unreachable, keeps the control flow explicit
            read_back.check(
                not shape.isNull(),
                "artifact.shape_not_null",
                "a non-null shape",
                "null",
                "The exported file contains no shape.",
            )
            read_back.check(
                len(shape.Solids) == expected_solids,
                "artifact.solids",
                expected_solids,
                len(shape.Solids),
                "The round-trip changed the solid count, so the exported file is "
                "not the geometry that was selected.",
            )
            read_back.numbers(
                "artifact.volume",
                expected_volume,
                shape.Volume,
                "The round-trip changed the volume; the export is not the "
                "geometry that was selected.",
                rel_tolerance=1e-3,
            )
        result = {
            "object_names": [obj.Name for obj in objects],
            "format": suffix,
            "verified": _verified_checks(read_back),
        }
        if suffix == "3mf":
            result["unit"] = _3MF_REQUIRED_UNIT
        return result
    finally:
        for obj in temp_meshes:
            doc.removeObject(obj.Name)
        _close_document(App, doc)


# --------------------------------------------------------------------------
# Dress-up, patterns and mirroring
#
# These five methods cover the two operations every real part needs after the
# base shape exists: finishing an edge (fillet, chamfer) and repeating a
# feature (linear pattern, polar pattern, mirror).
#
# They inherit one rule from the rest of this driver -- a mutating call returns
# only after the host proves the change is there -- and add one of their own:
# a size the geometry cannot absorb is refused *before* the kernel is asked.
# The reason is measured, not theoretical. Asking the kernel for a radius the
# adjacent faces cannot absorb does not reliably fail; it raises an opaque OCC
# error on this host and returns a self-intersecting solid on the next. Neither
# is an answer a caller can act on, so the analytic bound runs first and the
# post-write read-back catches whatever it lets through.
# --------------------------------------------------------------------------

# An edge list is bounded because every reference is validated against the real
# shape before anything is written. An unbounded list would let one call ask the
# kernel to dress an arbitrary number of edges, and the failure mode there is a
# half-applied model reported as a success.
MAX_EDGE_REFS = 200

# A pattern is bounded for a plainer reason: every instance is a transformed
# copy held in memory, so an unbounded count is an unbounded time and memory
# commitment inside a process the caller cannot interrupt.
MAX_PATTERN_INSTANCES = 1000


def _save_failure(exc):
    """A save that failed, reported as a code instead of FreeCAD's own wording."""
    return _coded("E_SAVE_FAILED", "the result could not be saved: %s" % str(exc).strip()[:200])


def _coded(code, message):
    """A refusal that carries a stable machine-readable code.

    The code travels twice: as the ``code`` key of the driver's JSON error and
    as the message prefix. The prefix matters because the bridge guarantees
    only that the message survives, so a caller that reads nothing else can
    still branch on the code.
    """
    error = ValueError("%s: %s" % (code, message))
    error.code = code
    return error


def _document_object(doc, name):
    obj = doc.getObject(name)
    if obj is None:
        raise _coded("E_OBJECT_NOT_FOUND", "the document has no object named %r" % name)
    return obj


def _solid_shape(obj, name):
    """The object's shape, refused when it is not something these tools can use."""
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise _coded(
            "E_NO_SHAPE", "%r has no Part shape, so there is no geometry to operate on" % name
        )
    if not shape.Solids:
        raise _coded(
            "E_NOT_A_SOLID",
            "%r is a %s with %d faces and no solids; fillet, chamfer, pattern and "
            "mirror operate on solids only" % (name, shape.ShapeType, len(shape.Faces)),
        )
    return shape


def _edge_refs(params, tool):
    """A bounded, duplicate-free list of 1-based edge indices."""
    refs = _required(params, "edge_refs", tool)
    if not isinstance(refs, list) or not refs:
        raise _coded(
            "E_EDGE_REFS_REQUIRED",
            "edge_refs must be a non-empty array of 1-based edge indices; inspect "
            "the document to list them",
        )
    if len(refs) > MAX_EDGE_REFS:
        raise _coded(
            "E_EDGE_REF_LIMIT",
            "edge_refs holds %d entries but the limit is %d; split the request into "
            "smaller batches" % (len(refs), MAX_EDGE_REFS),
        )
    resolved = []
    for ref in refs:
        if isinstance(ref, bool) or not isinstance(ref, int):
            raise _coded(
                "E_EDGE_REF_INVALID", "edge ref %r is not a 1-based integer edge index" % (ref,)
            )
        if ref in resolved:
            # A repeat is refused rather than de-duplicated: silently dropping it
            # is how a caller ends up believing two edges were dressed when one was.
            raise _coded("E_EDGE_REF_DUPLICATE", "edge %d is listed more than once" % ref)
        resolved.append(ref)
    return resolved


def _resolve_edge_refs(shape, refs, name):
    count = len(shape.Edges)
    for ref in refs:
        if ref < 1 or ref > count:
            raise _coded(
                "E_EDGE_REF_OUT_OF_RANGE",
                "%r has %d edges, so edge %d does not exist; the valid 1-based "
                "indices are 1..%d" % (name, count, ref, count),
            )
    return refs


def _adjacent_faces(shape, edge):
    """The faces sharing this edge; an edge of a manifold solid has exactly two."""
    return [face for face in shape.Faces if any(item.isSame(edge) for item in face.Edges)]


def _face_normal(face, point):
    """The face's outward normal at ``point``, with orientation applied.

    ``Face.normalAt`` reports the *underlying surface* normal, which points the
    wrong way for a face whose orientation is reversed. Every width or
    convexity calculation built on it silently inverts for those faces, so the
    correction lives here rather than at each call site.
    """
    try:
        u_value, v_value = face.Surface.parameter(point)
        normal = face.normalAt(u_value, v_value)
    except Exception:
        return None
    if str(face.Orientation) == "Reversed":
        normal = normal.negative()
    return normal


def _bound_box_corners(box):
    import FreeCAD as App

    return [
        App.Vector(x, y, z)
        for x in (box.XMin, box.XMax)
        for y in (box.YMin, box.YMax)
        for z in (box.ZMin, box.ZMax)
    ]


def _edge_size_limit(edge, shape):
    """The largest radius/distance this edge's adjacent faces can absorb.

    A fillet or chamfer grows out of the edge into each adjacent face, so the
    face's extent measured perpendicular to the edge caps the size. The extent
    is sampled from the face's vertices *and* its bounding box corners:
    vertices alone are too sparse for a curved face -- a cylinder wall has
    almost none -- and would report a near-zero limit for a face that can
    absorb a great deal.

    Returns ``None`` when the geometry offers nothing to measure, which callers
    treat as "no analytic bound" rather than "no limit".
    """
    point = edge.CenterOfMass
    try:
        tangent = edge.tangentAt(edge.ParameterRange[0])
    except Exception:
        return None
    if tangent.Length <= 1e-12:
        return None
    tangent.normalize()
    widths = []
    for face in _adjacent_faces(shape, edge):
        normal = _face_normal(face, point)
        if normal is None or normal.Length <= 1e-12:
            continue
        axis = normal.cross(tangent)
        if axis.Length <= 1e-12:
            continue
        axis.normalize()
        span = 0.0
        for corner in [vertex.Point for vertex in face.Vertexes] + _bound_box_corners(
            face.BoundBox
        ):
            span = max(span, abs((corner - point).dot(axis)))
        if span > 0:
            widths.append(span)
    if not widths:
        return None
    return min(widths)


def _format_limits(limits):
    """Render the measured per-edge limits for an infeasibility message."""
    known = dict((index, value) for index, value in limits.items() if value is not None)
    if not known:
        return "no analytic limit could be measured on the requested edges"
    return "; ".join("edge %d absorbs below %g" % (index, known[index]) for index in sorted(known))


def _assert_size_feasible(shape, indices, size, code, noun):
    """Refuse a size the geometry cannot absorb, and say what it can absorb.

    The bound is necessary rather than sufficient: it catches the gross case
    where a radius overruns an entire face, which is the case where the kernel
    stops being trustworthy. Anything it lets through is caught by the
    post-write read-back, so the two together mean a self-intersecting solid is
    never returned as a success.
    """
    limits = {}
    for index in indices:
        limits[index] = _edge_size_limit(shape.Edges[index - 1], shape)
        if limits[index] is None or size < limits[index]:
            continue
        raise _coded(
            code,
            "%s %g is not feasible on edge %d: %s" % (noun, size, index, _format_limits(limits)),
        )
    return limits


def _dress_up(params, tool, type_id, sizes, code, noun, size):
    """Shared body for fillet and chamfer: pre-check, build, verify, report.

    ``sizes`` maps an edge index to the ``(first, second)`` pair the host
    ``Edges`` property expects, which is the only part the two tools disagree
    on -- a fillet repeats its radius, a chamfer carries two distances.
    """
    import FreeCAD as App

    version = _host_version()
    object_name = _required(params, "object_name", tool)
    result_name = _required(params, "result_name", tool)
    result_label = params.get("result_label")
    indices = _edge_refs(params, tool)
    doc = _open_document(App, params["document_path"])
    try:
        source = _document_object(doc, object_name)
        shape = _solid_shape(source, object_name)
        _resolve_edge_refs(shape, indices, object_name)
        if doc.getObject(result_name) is not None:
            raise _coded("E_RESULT_EXISTS", "an object named %r already exists" % result_name)
        limits = _assert_size_feasible(shape, indices, size, code, noun)
        requested = dict((index, sizes(index)) for index in indices)
        before = shape.Volume
        feature = doc.addObject(type_id, result_name)
        feature.Base = source
        if result_label:
            feature.Label = str(result_label)
        try:
            feature.Edges = [(index,) + tuple(requested[index]) for index in indices]
            doc.recompute()
        except Exception as exc:
            # The kernel's own refusal is opaque ("BRepCheck_Analyzer::Init()
            # - NULL shape"), so it is re-reported with the measured limit,
            # which is the number the caller can actually act on.
            raise _coded(
                code,
                "%s %g is not feasible on the requested edges: %s (kernel: %s)"
                % (noun, size, _format_limits(limits), str(exc).strip()[:200]),
            ) from None
        try:
            _save_document(doc)
        except Exception as exc:
            raise _save_failure(exc) from None
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(result_name)
        read_back.exists("result", result_name, stored)
        read_back.check(
            stored.TypeId == type_id,
            "result.type_id",
            type_id,
            stored.TypeId,
            "The stored object is not the %s that was requested." % noun,
        )
        # The wiring is the operation: a fillet whose Base link did not take
        # recomputes against nothing and still looks like a success.
        linked = getattr(stored, "Base", None)
        read_back.check(
            linked is not None and linked.Name == object_name,
            "result.base",
            object_name,
            getattr(linked, "Name", None),
            "The result is not wired to the requested source object.",
        )
        applied = {}
        for item in stored.Edges:
            applied[int(item[0])] = (float(item[1]), float(item[2]))
        for index in indices:
            expected = [float(item) for item in requested[index]]
            actual = applied.get(index)
            read_back.check(
                actual is not None
                and all(
                    _contract_module().numbers_match(expected[position], actual[position])
                    for position in range(2)
                ),
                "result.edges[%d]" % index,
                expected,
                list(actual) if actual is not None else None,
                "The stored feature does not carry the requested %s for this edge, "
                "so the call was only partly applied." % noun,
            )
        result_shape = read_back.shape("result", stored)
        # A fillet or chamfer re-shapes one solid; it must not split it, drop it,
        # or silently fuse it with anything else.
        read_back.check(
            len(result_shape.Solids) == len(shape.Solids),
            "result.solids",
            len(shape.Solids),
            len(result_shape.Solids),
            "The operation changed the number of solids, so the result is not the "
            "requested re-shaping of the source.",
        )
        after = result_shape.Volume
        delta = after - before
        # Non-zero, not necessarily negative. A fillet removes material on a
        # convex edge and adds it on a concave one, and the host cannot be made
        # to say which an edge is: two independent classifications -- the
        # outward-normal cross product, and whether the normal bisector leaves
        # the solid -- both reported three of a plain cube's twelve edges as
        # concave, which is false for every one of them. Asserting a sign on a
        # number we cannot derive would fail real calls, so the direction is
        # reported and the magnitude is what is enforced. Tests pin the sign for
        # geometry whose convexity is known.
        tolerance = 1e-6 * max(1.0, abs(before))
        read_back.check(
            abs(delta) > tolerance,
            "result.volume_changed",
            "a volume that differs from the source",
            {"before": before, "after": after, "delta": delta},
            "The feature exists but the volume is unchanged, so the operation did "
            "not run even though the host accepted it.",
        )
        return {
            "object": _object_payload(stored),
            "feature_name": stored.Name,
            "affected_edges": len(indices),
            "edge_refs": list(indices),
            "volume_before": before,
            "volume_after": after,
            "volume_delta": delta,
            "volume_delta_direction": "decreased" if delta < 0 else "increased",
            "edge_size_limits": dict((str(index), limits[index]) for index in sorted(limits)),
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def model_fillet_edges(params):
    """Round a bounded set of edges on a solid."""
    tool = "model.fillet_edges"
    radius = _positive(_required(params, "radius", tool), "radius")
    return _dress_up(
        params,
        tool,
        "Part::Fillet",
        lambda index: (radius, radius),
        "E_RADIUS_NOT_FEASIBLE",
        "radius",
        radius,
    )


def _chamfer_sizes(params, tool):
    """The chamfer's two distances, from either ``distance`` or the explicit pair."""
    single = params.get("distance")
    first = params.get("distance1")
    second = params.get("distance2")
    if single is not None:
        if first is not None or second is not None:
            raise _coded(
                "E_DISTANCE_CONFLICT",
                "pass distance, or distance1 together with distance2, but not both",
            )
        value = _positive(single, "distance")
        return value, value, value
    if first is None or second is None:
        raise _coded(
            "E_DISTANCE_REQUIRED",
            "chamfer_edges needs distance, or both distance1 and distance2",
        )
    first_value = _positive(first, "distance1")
    second_value = _positive(second, "distance2")
    return max(first_value, second_value), first_value, second_value


def model_chamfer_edges(params):
    """Bevel a bounded set of edges on a solid."""
    tool = "model.chamfer_edges"
    size, first, second = _chamfer_sizes(params, tool)
    return _dress_up(
        params,
        tool,
        "Part::Chamfer",
        lambda index: (first, second),
        "E_DISTANCE_NOT_FEASIBLE",
        "chamfer distance",
        size,
    )


def _instance_count(params, tool):
    count = _required(params, "count", tool)
    if isinstance(count, bool) or not isinstance(count, int):
        raise _coded(
            "E_INSTANCE_COUNT_INVALID",
            "count must be an integer between 1 and %d" % MAX_PATTERN_INSTANCES,
        )
    if count < 1:
        raise _coded("E_INSTANCE_COUNT_INVALID", "count must be at least 1, got %d" % count)
    if count > MAX_PATTERN_INSTANCES:
        raise _coded(
            "E_INSTANCE_LIMIT",
            "count %d exceeds the %d instance limit; a larger run is better built as "
            "several smaller patterns" % (count, MAX_PATTERN_INSTANCES),
        )
    return count


def _direction_vector(params, key):
    values = _vector(params.get(key), key)
    if sum(item * item for item in values) <= 0:
        raise _coded("E_ZERO_VECTOR", "%s may not be the zero vector" % key)
    return values


def _boxes_intersect(first, second, tolerance=1e-6):
    """Whether two axis-aligned boxes share interior volume."""
    return (
        first.XMax > second.XMin + tolerance
        and second.XMax > first.XMin + tolerance
        and first.YMax > second.YMin + tolerance
        and second.YMax > first.YMin + tolerance
        and first.ZMax > second.ZMin + tolerance
        and second.ZMax > first.ZMin + tolerance
    )


def _min_instance_gap(boxes, wrap):
    """The smallest clearance between neighbouring instances.

    Two axis-aligned boxes are disjoint when they are separated on at least one
    axis, so the clearance is the largest single-axis separation; a negative
    clearance means the boxes overlap.

    This is a bounding-box test and therefore conservative: interleaved shapes
    (a comb meshing with its neighbour) can report an overlap the solids do not
    have. It never reports clearly separated instances as colliding, which is
    the direction that matters -- the flag exists to stop a caller shipping a
    pattern whose instances sit inside each other.
    """
    pairs = [(index, index + 1) for index in range(len(boxes) - 1)]
    if wrap and len(boxes) > 2:
        pairs.append((len(boxes) - 1, 0))
    gap = None
    for first_index, second_index in pairs:
        first = boxes[first_index]
        second = boxes[second_index]
        clearance = max(
            second.XMin - first.XMax,
            first.XMin - second.XMax,
            second.YMin - first.YMax,
            first.YMin - second.YMax,
            second.ZMin - first.ZMax,
            first.ZMin - second.ZMax,
        )
        gap = clearance if gap is None else min(gap, clearance)
    return gap


def _pattern_result(params, tool, matrices, wrap, detail):
    """Shared body for the two pattern tools: build, verify, report.

    The result is a compound of transformed copies rather than a live
    parametric array. That is a deliberate trade: the stock array objects are
    workbench-level and their constructor signatures moved between host
    versions, whereas ``Shape.transformed`` and ``Part.makeCompound`` are
    kernel primitives that behave identically on 1.0 and 1.1. A compound also
    makes the two things a caller needs to trust measurable -- the solid count
    is exactly ``instances x source solids``, and the volume is exactly
    ``instances x source volume``.
    """
    import FreeCAD as App
    import Part

    version = _host_version()
    object_name = _required(params, "object_name", tool)
    result_name = _required(params, "result_name", tool)
    result_label = params.get("result_label")
    count = len(matrices)
    doc = _open_document(App, params["document_path"])
    try:
        source = _document_object(doc, object_name)
        shape = _solid_shape(source, object_name)
        if doc.getObject(result_name) is not None:
            raise _coded("E_RESULT_EXISTS", "an object named %r already exists" % result_name)
        instance_volume = shape.Volume
        copies = [shape.transformed(matrix) for matrix in matrices]
        compound = Part.makeCompound(copies)
        feature = doc.addObject("Part::Feature", result_name)
        if result_label:
            feature.Label = str(result_label)
        feature.Shape = compound
        doc.recompute()
        try:
            _save_document(doc)
        except Exception as exc:
            raise _save_failure(exc) from None
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(result_name)
        read_back.exists("result", result_name, stored)
        read_back.check(
            stored.TypeId == "Part::Feature",
            "result.type_id",
            "Part::Feature",
            stored.TypeId,
            "The stored object is not the pattern feature that was requested.",
        )
        result_shape = read_back.shape("result", stored)
        # Solid count is the instance count made falsifiable: a host that
        # silently coalesced or dropped copies cannot produce this number.
        read_back.check(
            len(result_shape.Solids) == count * len(shape.Solids),
            "result.instance_count",
            count * len(shape.Solids),
            len(result_shape.Solids),
            "The pattern does not contain one solid group per requested instance, so "
            "some instances were dropped or merged.",
        )
        expected_volume = instance_volume * count
        # The tolerance absorbs floating point noise in the sum of transformed
        # copies, not a missing instance: one dropped copy is a whole
        # instance-volume of difference, six orders of magnitude larger.
        read_back.numbers(
            "result.volume",
            expected_volume,
            result_shape.Volume,
            "The pattern's volume is not the source volume times the instance count, "
            "so the instances are not all there.",
            rel_tolerance=1e-6,
        )
        boxes = [copy.BoundBox for copy in copies]
        gap = _min_instance_gap(boxes, wrap)
        return {
            "object": _object_payload(stored),
            "feature_name": stored.Name,
            "instance_count": count,
            "source_volume": instance_volume,
            "volume": result_shape.Volume,
            "expected_volume": expected_volume,
            "volume_deviation": result_shape.Volume - expected_volume,
            "min_instance_gap": gap,
            "overlap_detected": gap is not None and gap < 1e-6,
            "detail": detail,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def model_linear_pattern(params):
    """Repeat a solid along a direction at a fixed spacing."""
    import FreeCAD as App

    tool = "model.linear_pattern"
    _required(params, "object_name", tool)
    direction = _direction_vector(params, "direction")
    spacing = float(_required(params, "spacing", tool))
    if not math.isfinite(spacing) or abs(spacing) <= 0:
        raise _coded("E_PATTERN_SPACING", "spacing must be a finite, non-zero distance")
    count = _instance_count(params, tool)
    unit = App.Vector(*direction)
    unit.normalize()
    matrices = []
    for index in range(count):
        offset = App.Vector(
            unit.x * spacing * index, unit.y * spacing * index, unit.z * spacing * index
        )
        matrices.append(App.Placement(offset, App.Rotation()).toMatrix())
    return _pattern_result(
        params,
        tool,
        matrices,
        False,
        {"kind": "linear", "direction": direction, "spacing": spacing},
    )


def model_polar_pattern(params):
    """Repeat a solid around an axis at a fixed angular step."""
    import FreeCAD as App

    tool = "model.polar_pattern"
    _required(params, "object_name", tool)
    axis = _direction_vector(params, "axis")
    center = _vector(params.get("center") or [0, 0, 0], "center")
    count = _instance_count(params, tool)
    step_angle = params.get("angle_step_degrees")
    total_angle = params.get("total_angle_degrees")
    if step_angle is None and total_angle is None:
        raise _coded(
            "E_ANGLE_REQUIRED",
            "polar_pattern needs angle_step_degrees or total_angle_degrees",
        )
    if step_angle is not None and total_angle is not None:
        raise _coded(
            "E_ANGLE_CONFLICT",
            "pass angle_step_degrees or total_angle_degrees, but not both",
        )
    if total_angle is not None:
        if count < 2:
            raise _coded(
                "E_ANGLE_CONFLICT",
                "total_angle_degrees spreads the instances across the sweep, so it "
                "needs at least 2 instances, got %d" % count,
            )
        step_angle = float(total_angle) / (count - 1)
    step_angle = float(step_angle)
    if not math.isfinite(step_angle):
        raise _coded("E_ANGLE_INVALID", "the angle must be a finite number of degrees")
    if count > 1 and abs(step_angle) <= 0:
        raise _coded(
            "E_PATTERN_DEGENERATE",
            "a polar pattern of %d instances needs a non-zero angle; a zero step "
            "would stack every instance on the first" % count,
        )
    unit = App.Vector(*axis)
    unit.normalize()
    origin = App.Vector(*center)
    to_origin = App.Placement(origin.negative(), App.Rotation()).toMatrix()
    from_origin = App.Placement(origin, App.Rotation()).toMatrix()
    matrices = []
    for index in range(count):
        rotation = App.Placement(App.Vector(), App.Rotation(unit, step_angle * index)).toMatrix()
        # Matrix products compose left to right, so this reads inside out:
        # move to the origin, rotate there, move back.
        matrices.append(from_origin * rotation * to_origin)
    return _pattern_result(
        params,
        tool,
        matrices,
        True,
        {
            "kind": "polar",
            "axis": axis,
            "center": center,
            "angle_step_degrees": step_angle,
            "total_angle_degrees": step_angle * (count - 1),
        },
    )


# The mirror planes, named by the plane they flip across. Each normal is the
# axis the plane is perpendicular to.
_MIRROR_PLANES = {
    "xy": (0, 0, 1),
    "xz": (0, 1, 0),
    "yz": (1, 0, 0),
}


def model_mirror_feature(params):
    """Reflect a solid across one of the three base planes."""
    import FreeCAD as App

    tool = "model.mirror_feature"
    version = _host_version()
    object_name = _required(params, "object_name", tool)
    result_name = _required(params, "result_name", tool)
    result_label = params.get("result_label")
    plane = _required(params, "plane", tool)
    if plane not in _MIRROR_PLANES:
        raise _coded(
            "E_PLANE_INVALID",
            "plane must be one of %s, got %r" % (sorted(_MIRROR_PLANES), plane),
        )
    offset = float(params.get("offset") or 0)
    if not math.isfinite(offset):
        raise _coded("E_PLANE_OFFSET_INVALID", "offset must be a finite distance")
    doc = _open_document(App, params["document_path"])
    try:
        source = _document_object(doc, object_name)
        shape = _solid_shape(source, object_name)
        if doc.getObject(result_name) is not None:
            raise _coded("E_RESULT_EXISTS", "an object named %r already exists" % result_name)
        normal = _MIRROR_PLANES[plane]
        mirror = doc.addObject("Part::Mirroring", result_name)
        mirror.Source = source
        mirror.Normal = App.Vector(*normal)
        mirror.Base = App.Vector(normal[0] * offset, normal[1] * offset, normal[2] * offset)
        if hasattr(mirror, "MirrorPlane"):
            # Newer hosts add a plane reference that overrides the explicit
            # normal. Leaving it unset keeps the typed plane in charge instead
            # of inheriting whatever the host defaults it to.
            mirror.MirrorPlane = None
        if result_label:
            mirror.Label = str(result_label)
        doc.recompute()
        try:
            _save_document(doc)
        except Exception as exc:
            raise _save_failure(exc) from None
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(result_name)
        read_back.exists("result", result_name, stored)
        read_back.check(
            stored.TypeId == "Part::Mirroring",
            "result.type_id",
            "Part::Mirroring",
            stored.TypeId,
            "The stored object is not the mirror feature that was requested.",
        )
        linked = getattr(stored, "Source", None)
        read_back.check(
            linked is not None and linked.Name == object_name,
            "result.source",
            object_name,
            getattr(linked, "Name", None),
            "The mirror is not wired to the requested source object.",
        )
        result_shape = read_back.shape("result", stored)
        # A mirror is an isometry, which makes it the one operation here whose
        # result is known exactly in advance. Volume cannot move and neither can
        # the topology, so either moving means the host did something else and
        # the call must not be reported as a success.
        read_back.numbers(
            "result.volume",
            shape.Volume,
            result_shape.Volume,
            "A mirror preserves volume, so a different volume means the result is "
            "not a mirror of the source.",
            rel_tolerance=1e-6,
        )
        for attribute in ("Solids", "Faces", "Edges", "Vertexes"):
            read_back.numbers(
                "result.%s" % attribute.lower(),
                len(getattr(shape, attribute)),
                len(getattr(result_shape, attribute)),
                "A mirror preserves topology, so a different %s count means the "
                "result is not a mirror of the source." % attribute.lower(),
            )
        return {
            "object": _object_payload(stored),
            "feature_name": stored.Name,
            "plane": plane,
            "offset": offset,
            "volume_before": shape.Volume,
            "volume": result_shape.Volume,
            "solids": len(result_shape.Solids),
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


# ---------------------------------------------------------------------------
# Sketcher
#
# A sketch is the only way a typed caller can build a parametric profile, and
# the adapter has no script escape hatch to repair one afterwards. So every
# sketch call is stricter than the Part tools above it: a constraint that names
# a missing element is refused rather than ignored, and a sketch whose degrees
# of freedom the host will not report is never handed out as feature-ready.
# ---------------------------------------------------------------------------

_SKETCH_TYPE_ID = "Sketcher::SketchObject"
_BODY_TYPE_ID = "PartDesign::Body"
_DATUM_PLANE_TYPE_ID = "PartDesign::Plane"

# FreeCAD identifies geometry by TypeId, and the id decides which vertex
# positions exist. An unrecognised id is refused: guessing which of its
# positions a constraint may use is how a constraint silently binds to the
# wrong point.
_GEOMETRY_TYPE_IDS = {
    "Part::GeomPoint": "point",
    "Part::GeomLineSegment": "line",
    "Part::GeomCircle": "circle",
    "Part::GeomArcOfCircle": "arc",
}

# Ordered capability probe for a PartDesign feature's reversal property.
# Newest spelling first, oldest last: a host that moved the property is handled
# without the adapter committing to one generation's name, and a future
# generation can be prepended without touching a single call site.
_REVERSAL_PROPERTIES = ("SideType", "Midplane", "Symmetric")

# The same idea for the property that carries a sketch's attachment.
# ``Part::AttachableObject`` renamed ``Support`` to ``AttachmentSupport``, and
# neither spelling is guaranteed across the supported range, so the adapter
# probes for the one the host actually exposes instead of asserting a name. A
# sketch that is never attached is the silent failure this guards: the object
# exists, the call returns, and the geometry is drawn in the wrong plane.
_ATTACHMENT_PROPERTIES = ("AttachmentSupport", "Support")

# The attachment mode that puts the sketch flat on its support plane, and the
# property that selects it. Probed rather than assumed for the same reason.
_ATTACHMENT_MODE_PROPERTY = "MapMode"
_FLAT_FACE_MODE = "FlatFace"


def _attachment_property(sketch):
    for name in _ATTACHMENT_PROPERTIES:
        if _has_property(sketch, name):
            return name
    raise IncompatibleHostError(
        "The sketch exposes none of %s, so it cannot be attached to a plane. The host API "
        "moved outside the verified compatibility matrix, and an unattached sketch would "
        "draw in an arbitrary plane while reporting success." % "/".join(_ATTACHMENT_PROPERTIES)
    )


def _set_attachment(sketch, plane_object):
    """Attach a sketch to ``plane_object`` and return the property that took."""
    if not _has_property(sketch, _ATTACHMENT_MODE_PROPERTY):
        raise IncompatibleHostError(
            "The sketch exposes no %s property, so it cannot be told to lie flat on its "
            "support plane; refusing rather than leaving the attachment unspecified."
            % _ATTACHMENT_MODE_PROPERTY
        )
    name = _attachment_property(sketch)
    setattr(sketch, name, [(plane_object, "")])
    setattr(sketch, _ATTACHMENT_MODE_PROPERTY, _FLAT_FACE_MODE)
    return name


def _attachment_support(sketch):
    for name in _ATTACHMENT_PROPERTIES:
        if _has_property(sketch, name):
            return list(getattr(sketch, name) or ())
    return []


def _has_property(obj, name):
    try:
        return bool(hasattr(obj, name))
    except Exception:
        return False


def _resolve_reversal_property(obj, version):
    """Resolve a PartDesign feature's reversal property by capability probe."""
    for name in _REVERSAL_PROPERTIES:
        if _has_property(obj, name):
            return name
    raise IncompatibleHostError(
        "FreeCAD %s exposes none of %s on %s; the reversal property moved outside the "
        "verified compatibility matrix, so the write was refused instead of silently "
        "doing nothing"
        % (
            version,
            "/".join(_REVERSAL_PROPERTIES),
            getattr(obj, "TypeId", type(obj).__name__),
        )
    )


# ---------------------------------------------------------------------------
# PartDesign sketch features
#
# The failure mode these tools exist to eliminate is upstream FreeCAD issue #99:
# ``pocket_sketch`` reports success and a Valid state while removing no material
# at all. Every tool here therefore refuses to trust the return value of the
# call it made and instead measures the body's volume before and after, then
# asserts the *direction* and the *magnitude* of the change. A pocket that did
# not reduce the volume is an error, not a success with a smaller number.
# ---------------------------------------------------------------------------

# Type ids of the PartDesign features this module creates.
_PAD_TYPE_ID = "PartDesign::Pad"
_POCKET_TYPE_ID = "PartDesign::Pocket"
_REVOLUTION_TYPE_ID = "PartDesign::Revolution"
_GROOVE_TYPE_ID = "PartDesign::Groove"
_LOFT_TYPE_ID = "PartDesign::AdditiveLoft"
_SWEEP_TYPE_ID = "PartDesign::AdditivePipe"
_HOLE_TYPE_ID = "PartDesign::Hole"

# Which side of the profile a feature extrudes towards. ``one_side`` is the
# host default; the other two are expressed through whichever reversal property
# this host exposes, never through a hard-coded attribute name.
_SIDE_TYPES = ("one_side", "two_sides", "symmetric")

# The value a reversal property takes for each side type, per generation.
# ``SideType`` is an enumeration spelled as a string, while ``Midplane`` and
# ``Symmetric`` are booleans, so the mapping cannot be shared. Both are keyed
# the same way so a call site only names the intent.
_SIDE_TYPE_ENUM = {"one_side": "One side", "two_sides": "Two sides", "symmetric": "Symmetric"}

# Hole profiles, as the host spells them.
_HOLE_TYPES = ("none", "counterbore", "countersink")

# A volume change this small is noise: the tolerance exists to absorb the
# floating-point residue of a recompute, not to excuse a feature that did
# nothing. One dropped feature is a whole profile-area of difference, many
# orders of magnitude larger.
_VOLUME_EPSILON = 1e-9

# The fraction of the requested change a feature must deliver. A pad asked for
# 10 mm that produced 4 mm is a silent truncation, not a success. The bound is
# loose enough to survive how a host clips a profile against the body it grows
# from, and tight enough that a feature which did not run cannot pass it.
_VOLUME_DELTA_REL_TOLERANCE = 0.02


def _feature_volume(obj, tool, what):
    """Assert the feature produced a real solid, and return its volume.

    A pad that produced no solid is the upstream bug in its purest form: the
    object exists, the recompute is clean, and there is nothing there.

    A *zero* volume is deliberately not refused here. It is legitimate for the
    first feature in an empty body to be the body's own empty state, and a
    feature that changed nothing but left a valid shape is precisely the case
    the volume-delta comparison reports better -- it can say how much should
    have changed, where this check could only say "nothing". Refusing here
    would replace that better error with a worse one.
    """
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise _coded(
            "E_FEATURE_EMPTY",
            "%s produced no geometry for %s, so the feature did not take effect" % (tool, what),
        )
    if not getattr(shape, "Solids", None):
        raise _coded(
            "E_FEATURE_EMPTY",
            "%s produced a %s with no solids for %s; a sketch feature must yield a solid"
            % (tool, getattr(shape, "ShapeType", "shape"), what),
        )
    if not shape.isValid():
        raise _coded(
            "E_FEATURE_INVALID",
            "%s produced an invalid shape for %s, so the result is not a solid the caller "
            "can build on" % (tool, what),
        )
    volume = float(shape.Volume)
    if not math.isfinite(volume):
        raise _coded(
            "E_FEATURE_INVALID",
            "%s produced a non-finite volume for %s, so the result cannot be measured"
            % (tool, what),
        )
    return volume


def _body_volume(body, tool, name):
    """The current volume of a PartDesign body, or 0.0 when it holds nothing.

    A body before its first feature legitimately has no tip, so an absent one is
    a real zero and not an error -- what matters is that the number is measured
    rather than assumed.
    """
    tip = getattr(body, "Tip", None)
    if tip is None:
        return 0.0
    shape = getattr(tip, "Shape", None)
    if shape is None or shape.isNull() or not getattr(shape, "Solids", None):
        return 0.0
    volume = float(shape.Volume)
    if not math.isfinite(volume) or volume <= 0.0:
        return 0.0
    return volume


def _profile_area(sketch, tool):
    """The area enclosed by a sketch's profile, as the host measures it.

    A feature's volume is its profile area times its extent, so a profile with
    no area cannot produce volume and must be refused before the feature runs:
    an open or self-intersecting profile is exactly what a host turns into an
    empty shell while still reporting success.
    """
    shape = getattr(sketch, "Shape", None)
    area = getattr(shape, "Area", None) if shape is not None else None
    try:
        value = float(area)
    except (TypeError, ValueError):
        raise _coded(
            "E_PROFILE_DEGENERATE",
            "%s: the sketch exposes no measurable profile area, so its profile is degenerate "
            "(open, self-intersecting, or empty)" % tool,
        ) from None
    if not math.isfinite(value) or value <= 0.0:
        raise _coded(
            "E_PROFILE_DEGENERATE",
            "%s: the sketch profile encloses %r area, so it is degenerate (open, "
            "self-intersecting, or zero-thickness) and cannot produce a solid" % (tool, value),
        )
    return value


def _check_volume_delta(read_back, tool, name, expected_delta, actual_delta, direction):
    """Assert a feature changed the body's volume in the direction it must.

    Three things are checked, and each one catches a different failure:

    * **direction** -- a pocket that increased the volume, or a pad that
      decreased it, is the tool having done the opposite of what was asked;
    * **presence** -- a delta of zero is the upstream #99 bug itself, a feature
      that reported success and changed nothing;
    * **magnitude** -- a delta far smaller than the profile and extent require
      is a feature that ran but was clipped, or a profile silently reinterpreted.

    The magnitude bound is a tolerance, not an assertion of exact equality: a
    host clips a profile against the body it grows from, so the delivered volume
    is bounded from above by the request rather than equal to it.
    """
    # The direction check is recorded whether it passes or fails: a caller
    # reading ``verified`` must be able to see that the direction was measured,
    # not only hear about it when it disagreed.
    wrong_way = (
        actual_delta <= _VOLUME_EPSILON if direction == "add" else actual_delta >= -_VOLUME_EPSILON
    )
    if direction == "add":
        read_back.check(
            not wrong_way,
            "%s.volume_delta.direction" % name,
            "an increase of about %r" % expected_delta,
            actual_delta,
            "The body's volume did not increase, so %s added nothing even though the "
            "host reported success. The profile is open, self-intersecting, or was "
            "rejected downstream." % tool,
        )
    else:
        read_back.check(
            not wrong_way,
            "%s.volume_delta.direction" % name,
            "a decrease of about %r" % expected_delta,
            actual_delta,
            "The body's volume did not decrease, so %s removed no material even though "
            "the host reported success. This is the failure upstream FreeCAD issue #99 "
            "reports as a clean success." % tool,
        )
    magnitude = abs(actual_delta)
    expected = abs(expected_delta)
    read_back.check(
        magnitude <= expected * (1.0 + _VOLUME_DELTA_REL_TOLERANCE) + _VOLUME_EPSILON,
        "%s.volume_delta.magnitude" % name,
        "at most %r" % (expected * (1.0 + _VOLUME_DELTA_REL_TOLERANCE)),
        magnitude,
        "The feature changed the volume by more than its profile and extent allow, so the "
        "geometry does not match the request.",
    )
    read_back.check(
        magnitude >= expected * (1.0 - _VOLUME_DELTA_REL_TOLERANCE) - _VOLUME_EPSILON,
        "%s.volume_delta.magnitude" % name,
        "at least %r" % (expected * (1.0 - _VOLUME_DELTA_REL_TOLERANCE)),
        magnitude,
        "The feature changed the volume by far less than its profile and extent require, so "
        "it was clipped or silently reinterpreted instead of running as asked.",
    )


def _sketch_body(sketch, tool, sketch_name):
    """The PartDesign body a sketch belongs to.

    A PartDesign feature is only meaningful inside a body: it is the body that
    accumulates the solid and the body whose volume the read-back measures. A
    sketch with no owning body would otherwise produce a feature whose result
    lands nowhere the caller can measure.
    """
    for owner in getattr(sketch, "InList", ()) or ():
        if getattr(owner, "TypeId", None) == _BODY_TYPE_ID:
            return owner
    raise _coded(
        "E_SKETCH_NOT_IN_BODY",
        "%s: sketch %r does not belong to a PartDesign body, so a feature built on it has "
        "no solid to add to or cut from" % (tool, sketch_name),
    )


def _new_feature(body, type_id, name, tool):
    """Create a feature object inside a body, refusing a name already taken."""
    if getattr(body, "Document", None) is not None:
        if body.Document.getObject(name) is not None:
            raise _coded("E_RESULT_EXISTS", "%s: an object named %r already exists" % (tool, name))
    try:
        return body.newObject(type_id, name)
    except Exception as exc:
        raise _coded(
            "E_FEATURE_UNSUPPORTED",
            "%s: FreeCAD could not create a %s (%s); the feature is not available on this "
            "host" % (tool, type_id, exc),
        ) from None


def _apply_side_type(feature, side_type, version, tool):
    """Express a requested side type through whichever property this host has.

    The ladder is probed, never assumed: ``SideType`` is the current spelling,
    ``Midplane`` and ``Symmetric`` are the two it replaced, and which one a given
    build exposes is exactly what the compatibility matrix has not yet measured.
    Hard-coding any of the three is what makes a two-sided pad come back
    one-sided while still reporting success.

    Revolution and Groove carry no reversal property at all -- a host that grew
    one would be expressing a different concept -- so they are handled by the
    caller instead, and never reach this function.

    Returns the property name that took the write, or None for ``one_side``,
    where there is nothing to set. That name is reported as evidence, because it
    is how a host that moved the property is recognised after the fact.
    """
    if side_type == "one_side":
        return None
    name = _resolve_reversal_property(feature, version)
    if name == "SideType":
        setattr(feature, name, _SIDE_TYPE_ENUM[side_type])
    else:
        # ``Midplane`` and ``Symmetric`` are booleans: both exist only as a
        # two-sided switch, which is what ``symmetric`` asks for. ``two_sides``
        # additionally needs a second length, set by the caller.
        setattr(feature, name, True)
    return name


def _feature_result(
    params,
    tool,
    type_id,
    direction,
    configure,
    extent_for_area,
):
    """Run one PartDesign sketch feature and prove it changed the body.

    ``direction`` is ``"add"`` for pad/loft/sweep/revolution and ``"remove"``
    for pocket/groove/hole: it fixes which way the volume must move, which is
    the assertion that catches upstream #99.

    ``configure(feature)`` sets the feature's own properties and is the only
    part that differs between tools. ``extent_for_area(profile_area)`` returns
    the volume the feature must add or remove given the profile it was handed,
    so the expected magnitude is derived from the request rather than from
    anything the host reported.

    The sequence is deliberately: measure, write, recompute, save, re-measure,
    then assert. The body's volume is read before anything is created, because a
    number read after the feature cannot distinguish "the feature added this"
    from "it was already there".
    """
    import FreeCAD as App

    version = _host_version()
    rules = _sketch_module()
    sketch_name = _required(params, "sketch_name", tool)
    result_name = _required(params, "result_name", tool)
    result_label = params.get("result_label")
    side_type = params.get("side_type") or "one_side"
    if side_type not in _SIDE_TYPES:
        raise _coded(
            "E_SIDE_TYPE_INVALID",
            "%s: unsupported side_type: %s (supported: %s)"
            % (tool, side_type, ", ".join(_SIDE_TYPES)),
        )
    reversed_direction = bool(params.get("reversed"))
    doc = _open_document(App, params["document_path"])
    try:
        sketch = _open_sketch(doc, sketch_name, tool)
        # An under-constrained sketch is refused before it is consumed. Its
        # profile is not a shape the caller chose: it is whatever the solver
        # happened to settle on, and it moves on the next host version.
        dof, dof_source = _sketch_dof(sketch, version, tool)
        state = rules.feature_state(dof, len(getattr(sketch, "Geometry", ()) or ()))
        rules.assert_feature_ready(state, sketch_name, tool)
        body = _sketch_body(sketch, tool, sketch_name)
        profile_area = _profile_area(sketch, tool)
        before = _body_volume(body, tool, body.Name)
        expected_delta = extent_for_area(profile_area, sketch)
        if expected_delta <= 0.0:
            raise _coded(
                "E_EXTENT_DEGENERATE",
                "%s: the requested extent yields %r volume change, so the feature would be "
                "zero-thickness" % (tool, expected_delta),
            )
        feature = _new_feature(body, type_id, result_name, tool)
        if result_label:
            feature.Label = str(result_label)
        feature.Profile = sketch
        side_property = _apply_side_type(feature, side_type, version, tool)
        configure(feature)
        if reversed_direction and _has_property(feature, "Reversed"):
            feature.Reversed = True
        doc.recompute()
        try:
            _save_document(doc)
        except Exception as exc:
            raise _save_failure(exc) from None
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(result_name)
        read_back.exists("feature", result_name, stored)
        read_back.check(
            stored.TypeId == type_id,
            "feature.type_id",
            type_id,
            stored.TypeId,
            "The stored object is not the %s that was requested." % type_id,
        )
        read_back.check(
            getattr(getattr(stored, "Profile", None), "Name", None) == sketch_name,
            "feature.profile",
            sketch_name,
            getattr(getattr(stored, "Profile", None), "Name", None),
            "The feature is not built on the sketch it was asked to use.",
        )
        read_back.check(
            body.Name in [item.Name for item in getattr(stored, "InList", ()) or ()],
            "feature.in_body",
            body.Name,
            [item.Name for item in getattr(stored, "InList", ()) or ()],
            "The feature is not inside the body, so the body's volume cannot have changed "
            "and the result would not belong to the requested body.",
        )
        _feature_volume(stored, tool, result_name)
        after = _body_volume(body, tool, body.Name)
        actual_delta = after - before
        if direction == "remove":
            signed_expected = -expected_delta
        else:
            signed_expected = expected_delta
        _check_volume_delta(read_back, tool, "feature", signed_expected, actual_delta, direction)
        result = {
            "feature": {
                "name": stored.Name,
                "label": stored.Label,
                "type_id": stored.TypeId,
                "profile": sketch_name,
                "body": body.Name,
                "side_type": side_type,
                "reversed": reversed_direction,
                # Which property carried the side type is evidence, not trivia:
                # it is how a host that moved the property is recognised.
                "side_property": side_property,
            },
            "volume": {
                "before": before,
                "after": after,
                "delta": actual_delta,
                "expected_delta": signed_expected,
                "direction": direction,
            },
            "profile_area": profile_area,
            "dof": dof,
            "dof_source": dof_source,
            "feature_state": state,
            "verified": _verified_checks(read_back),
        }
        return result
    finally:
        _close_document(App, doc)


def _open_sketch(doc, sketch_name, tool):
    sketch = doc.getObject(sketch_name)
    if sketch is None:
        raise ValueError("%s: sketch does not exist: %s" % (tool, sketch_name))
    if getattr(sketch, "TypeId", None) != _SKETCH_TYPE_ID:
        raise ValueError(
            "%s: object is not a sketch (%s): %s"
            % (tool, getattr(sketch, "TypeId", "unknown"), sketch_name)
        )
    return sketch


def _geometry_kind(geo):
    type_id = getattr(geo, "TypeId", None)
    kind = _GEOMETRY_TYPE_IDS.get(type_id)
    if kind is None:
        raise IncompatibleHostError(
            "The sketch contains geometry the adapter does not recognise (%s); refusing to "
            "guess which of its vertices a constraint may reference" % (type_id,)
        )
    return kind


def _point_pair(value):
    return [float(value.x), float(value.y)]


def _arc_midpoint(geo):
    """A point on the stored arc's swept side, in ``key_points`` order.

    The midpoint is what distinguishes an arc from its complement: both share
    their endpoints, centre and radius, so those four agree even when the host
    swept the other way round. FreeCAD exposes the midpoint of the *parameter*
    range, which is exactly the arc it actually stored.
    """
    try:
        middle = (float(geo.FirstParameter) + float(geo.LastParameter)) / 2.0
        return _point_pair(geo.value(middle))
    except Exception:
        # Without a midpoint the read-back loses the only value that can catch
        # a reversed sweep, so an unreadable one is a failure rather than a
        # value silently dropped from the comparison.
        raise _sketch_module().SketchSpecError(
            "the stored arc does not expose a midpoint, so its sweep direction "
            "cannot be verified against the request"
        ) from None


def _geometry_read_back(geo, kind):
    """The coordinates of a stored geometry element, in ``key_points`` order."""
    if kind == "point":
        return [float(geo.X), float(geo.Y)]
    if kind == "line":
        return _point_pair(geo.StartPoint) + _point_pair(geo.EndPoint)
    values = _point_pair(geo.Center) + [float(geo.Radius)]
    if kind == "arc":
        return _point_pair(geo.StartPoint) + _arc_midpoint(geo) + _point_pair(geo.EndPoint) + values
    return values


# Ordered capability probe for the host's degrees-of-freedom count. ``DoF`` is
# the spelling FreeCAD 1.0.2 and 1.1.4 both expose, measured on each supported
# host rather than assumed; the others are kept so a host that renames it again
# is recognised instead of silently reported as fully constrained.
_DOF_PROPERTIES = ("DoF", "DOF", "dof")

# What a negative ``Sketch.solve()`` return means, in the host's own terms. Read
# as error codes, never as a degree-of-freedom count.
_SOLVER_STATUS = {
    -1: "the solver did not converge",
    -2: "redundant constraints",
    -3: "conflicting constraints",
    -4: "over-constrained",
    -5: "malformed constraints",
}


def _solver_status_text(code):
    return _SOLVER_STATUS.get(code, "solver reported failure")


def _sketch_dof(sketch, version, tool):
    """Read a sketch's degrees of freedom, or ``None`` when the host hides them.

    An unsolvable sketch is a real failure and is reported as one. A count the
    host will not report is returned as ``None`` so the caller is told the
    sketch is not provably constrained, instead of being handed a number that
    merely looks plausible -- a silently under-constrained sketch is worse than
    a refused one, because the wrong geometry only shows up downstream.

    ``solve()`` is called so a sketch the solver rejects is reported, but its
    return value is deliberately never read as a count: on FreeCAD 1.0.2 and
    1.1.4 it returns ``0`` for any sketch that solves, including one measured at
    four degrees of freedom. Reading it as a count is precisely the trap this
    function exists to avoid -- the number is plausible, and it is wrong.

    It is still read as an *error code*, because that is what it also is. A
    negative return means the host did not commit a solution: it leaves
    ``FullyConstrained`` unset and the geometry un-updated, while the DOF it
    reports can still be zero. The codes that a DOF sign already catches
    (conflicting, over-constrained) are named for the caller's benefit; the
    three it does not catch -- redundant, malformed, not converged -- are the
    reason this check exists at all.
    """
    rules = _sketch_module()
    try:
        solver_status = sketch.solve()
    except Exception as exc:
        raise rules.SketchStateError(
            rules.ERROR_SOLVER_FAILED,
            "%s: the solver rejected the sketch (%s). Correct the conflicting constraints "
            "instead of building on a profile that cannot be solved." % (tool, exc),
            tool=tool,
            sketch_name=getattr(sketch, "Name", None),
            host_version=version,
            solver_error=str(exc),
        ) from None
    if isinstance(solver_status, int) and not isinstance(solver_status, bool):
        if solver_status < 0:
            raise rules.SketchStateError(
                rules.ERROR_SOLVER_FAILED,
                "%s: the solver did not converge (status %d: %s). The host left the "
                "sketch unsolved and did not update its geometry, so its degrees of "
                "freedom cannot be trusted. Correct the %s and retry."
                % (
                    tool,
                    solver_status,
                    _solver_status_text(solver_status),
                    _solver_status_text(solver_status),
                ),
                tool=tool,
                sketch_name=getattr(sketch, "Name", None),
                host_version=version,
                solver_error=str(solver_status),
            )
    for attribute in _DOF_PROPERTIES:
        value = getattr(sketch, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value, "property.%s" % attribute
    method = getattr(sketch, "getDoF", None)
    if callable(method):
        value = method()
        if isinstance(value, int) and not isinstance(value, bool):
            return value, "method.getDoF"
    return None, None


def _build_geometry(app, part, primitive):
    kind = primitive["kind"]
    if kind == "point":
        x, y = primitive["points"][0]
        return part.Point(app.Vector(x, y, 0.0))
    if kind == "line":
        start, end = primitive["points"][0], primitive["points"][1]
        return part.LineSegment(app.Vector(start[0], start[1], 0.0), app.Vector(end[0], end[1], 0))
    center = app.Vector(primitive["center"][0], primitive["center"][1], 0.0)
    circle = part.Circle(center, app.Vector(0.0, 0.0, 1.0), primitive["radius"])
    if kind == "arc":
        start, end = primitive["angles_degrees"]
        # Angles are passed in ascending order and ``sense`` is always True.
        #
        # Measured on FreeCAD 1.0.2 and 1.1.4, Part::GeomArcOfCircle keeps its
        # parameter range ascending, so it cannot record which end the caller
        # called the start: a request for 90 -> 0 stores the 270 degree
        # complement, with the start point where it was asked for and the sweep
        # continuing the other way round to 360. Neither sense value avoids that
        # -- True and False produce the same curve on such a pair, because the
        # periodic branch of Geom_TrimmedCurve::SetTrim takes ``sameSense = Sense``
        # without swapping, and the range is raised to ascending afterwards.
        #
        # So sketch_rules refuses a negative sweep outright rather than let this
        # reinterpret one, and every arc reaching here is ascending.
        return part.ArcOfCircle(circle, math.radians(start), math.radians(end), True)
    return circle


def _build_constraint(sketcher, constraint, tool):
    name = constraint["free_cad_type"]
    first = constraint["first"]
    second = constraint["second"]
    if name == "Coincident":
        return sketcher.Constraint(
            name, first["element"], first["vertex"], second["element"], second["vertex"]
        )
    if name == "PointOnObject":
        return sketcher.Constraint(name, first["element"], first["vertex"], second["element"])
    if name in ("Horizontal", "Vertical"):
        return sketcher.Constraint(name, first["element"])
    if name in ("Parallel", "Perpendicular", "Tangent", "Equal"):
        return sketcher.Constraint(name, first["element"], second["element"])
    if name in ("DistanceX", "DistanceY"):
        return sketcher.Constraint(
            name,
            first["element"],
            first["vertex"],
            second["element"],
            second["vertex"],
            constraint["value"],
        )
    if name == "Distance":
        # A Distance on an element alone is the element's length; with two point
        # references it is the distance between them. The adapter keeps those as
        # two constraint types, so neither can be misread as the other.
        if second is None:
            return sketcher.Constraint(name, first["element"], constraint["value"])
        return sketcher.Constraint(
            name,
            first["element"],
            first["vertex"],
            second["element"],
            second["vertex"],
            constraint["value"],
        )
    if name == "Radius":
        return sketcher.Constraint(name, first["element"], constraint["value"])
    if name == "Angle":
        # FreeCAD stores a sketch angle in radians; the tool takes degrees so a
        # caller never has to guess, and the read-back asserts the stored value.
        return sketcher.Constraint(
            name, first["element"], second["element"], math.radians(constraint["value_degrees"])
        )
    raise ValueError("%s: unsupported constraint: %s" % (tool, name))


def _expected_constraint_value(constraint):
    if constraint["value"] is not None:
        return constraint["value"]
    if constraint["value_degrees"] is not None:
        return math.radians(constraint["value_degrees"])
    return None


def _constraint_payload(index, constraint):
    value = getattr(constraint, "Value", None)
    return {
        "index": index,
        "type": str(getattr(constraint, "Type", "") or ""),
        "first": int(getattr(constraint, "First", 0) or 0),
        "first_pos": int(getattr(constraint, "FirstPos", 0) or 0),
        "second": int(getattr(constraint, "Second", 0) or 0),
        "second_pos": int(getattr(constraint, "SecondPos", 0) or 0),
        "third": int(getattr(constraint, "Third", 0) or 0),
        "value": float(value) if value is not None else None,
    }


def _check_constraint_references(sketch, constraint, tool):
    """Resolve every reference against live geometry before writing anything.

    A constraint on a missing element is the sketching equivalent of a dropped
    write: FreeCAD adds it, the call returns, and the sketch solves as if the
    constraint were never asked for. Refusing before ``addConstraint`` is what
    makes the difference visible at the call that caused it.
    """
    rules = _sketch_module()
    count = len(sketch.Geometry)
    kinds = {}
    for role in ("first", "second"):
        reference = constraint[role]
        if reference is None:
            kinds[role] = None
            continue
        element = reference["element"]
        if element == rules.ROOT_ELEMENT:
            kinds[role] = "point"
            continue
        if element < 0 or element >= count:
            raise rules.SketchStateError(
                rules.ERROR_ELEMENT_NOT_FOUND,
                "%s: %s.%s.element %d does not exist; the sketch has %d geometry element(s). "
                "Reference an element index returned by add_sketch_geometry or "
                "get_sketch_info." % (tool, constraint["type"], role, element, count),
                tool=tool,
                constraint_type=constraint["type"],
                role=role,
                element=element,
                geometry_count=count,
            )
        kinds[role] = _geometry_kind(sketch.Geometry[element])
    rules.check_kinds(constraint, kinds["first"], kinds["second"])


def _sketch_plane(sketch):
    """The named plane whose normal the sketch is attached to, or None."""
    rules = _sketch_module()
    rotation = getattr(getattr(sketch, "Placement", None), "Rotation", None)
    if rotation is None or not hasattr(rotation, "multVec"):
        return None
    normal = rotation.multVec(_sketch_vector(0.0, 0.0, 1.0))
    observed = (normal.x, normal.y, normal.z)
    for name in rules.PLANES:
        expected = rules.plane_normal(name)
        if _contract_module().sequences_match(expected, observed, 1e-6):
            return name
    return None


def _sketch_vector(x, y, z):
    import FreeCAD as App

    return App.Vector(x, y, z)


def _sketch_body_name(sketch):
    for item in getattr(sketch, "InList", ()) or ():
        if getattr(item, "TypeId", None) == _BODY_TYPE_ID:
            return item.Name
    return None


def _construction_flag(sketch, index):
    reader = getattr(sketch, "getConstruction", None)
    if not callable(reader):
        return False
    return bool(reader(index))


def sketch_create(params):
    """Create a PartDesign body, a datum plane and an attached sketch."""
    import FreeCAD as App

    tool = "sketch.create"
    version = _host_version()
    rules = _sketch_module()
    name = _required(params, "name", tool)
    plane = params.get("plane") or "xy"
    if plane not in rules.PLANES:
        raise ValueError(
            "%s: unsupported plane: %s (supported: %s)" % (tool, plane, ", ".join(rules.PLANES))
        )
    body_name = params.get("body") or "Body"
    plane_name = "%sPlane" % name
    label = params.get("label")
    doc = _open_document(App, params["document_path"])
    try:
        for candidate in (name, plane_name):
            if doc.getObject(candidate) is not None:
                raise ValueError("Object already exists: %s" % candidate)
        existing = doc.getObject(body_name)
        created_body = False
        if existing is None:
            body = doc.addObject(_BODY_TYPE_ID, body_name)
            created_body = True
        elif getattr(existing, "TypeId", None) == _BODY_TYPE_ID:
            # Reusing the body is the normal PartDesign flow: several sketches
            # live under one body. A non-body with the same name is refused,
            # because attaching a sketch to it would be a silent reinterpretation.
            body = existing
        else:
            raise ValueError(
                "Existing object is not a PartDesign body (%s): %s"
                % (getattr(existing, "TypeId", "unknown"), body_name)
            )
        try:
            plane_object = body.newObject(_DATUM_PLANE_TYPE_ID, plane_name)
        except Exception as exc:
            raise IncompatibleHostError(
                "FreeCAD %s could not create a %s datum plane for sketch %s (%s)"
                % (version, _DATUM_PLANE_TYPE_ID, name, exc)
            ) from None
        axis, degrees = rules.PLANE_ROTATIONS[plane]
        plane_object.Placement = App.Placement(
            App.Vector(0.0, 0.0, 0.0), App.Rotation(App.Vector(*axis), degrees)
        )
        sketch = body.newObject(_SKETCH_TYPE_ID, name)
        if label:
            sketch.Label = str(label)
        attachment_property = _set_attachment(sketch, plane_object)
        doc.recompute()
        _save_document(doc)
        read_back = _ReadBack(tool, version, params)
        stored = doc.getObject(name)
        read_back.exists("sketch", name, stored)
        read_back.check(
            stored.TypeId == _SKETCH_TYPE_ID,
            "sketch.type_id",
            _SKETCH_TYPE_ID,
            stored.TypeId,
            "The object was created as a different type than requested.",
        )
        support = _attachment_support(stored)
        read_back.check(
            bool(support),
            "sketch.support",
            "one attached support reference",
            len(support),
            "The sketch has no support, so it is not attached to a plane: its placement "
            "would be arbitrary and the sketch would not follow the requested plane.",
        )
        attached = support[0][0] if support else None
        read_back.check(
            getattr(attached, "Name", None) == plane_name,
            "sketch.support_plane",
            plane_name,
            getattr(attached, "Name", None),
            "The sketch is attached to a different object than the datum plane this call created.",
        )
        read_back.check(
            getattr(stored, _ATTACHMENT_MODE_PROPERTY, None) == _FLAT_FACE_MODE,
            "sketch.map_mode",
            _FLAT_FACE_MODE,
            getattr(stored, _ATTACHMENT_MODE_PROPERTY, None),
            "The attachment mode was not stored, so the sketch is not lying flat on its "
            "support plane.",
        )
        normal = stored.Placement.Rotation.multVec(App.Vector(0.0, 0.0, 1.0))
        read_back.sequences(
            "sketch.plane_normal",
            list(rules.plane_normal(plane)),
            [normal.x, normal.y, normal.z],
            "The sketch normal is not the requested plane, so the geometry would be drawn "
            "in a different plane than asked for.",
        )
        read_back.check(
            body.Name in [item.Name for item in getattr(stored, "InList", ()) or ()],
            "sketch.in_body",
            body.Name,
            [item.Name for item in getattr(stored, "InList", ()) or ()],
            "The sketch is not inside the body, so a feature built on it would not belong "
            "to that body.",
        )
        dof, dof_source = _sketch_dof(stored, version, tool)
        state = rules.feature_state(dof, len(stored.Geometry))
        return {
            "sketch": {
                "name": stored.Name,
                "label": stored.Label,
                "type_id": stored.TypeId,
                "plane": plane,
                "normal": [normal.x, normal.y, normal.z],
                "geometry_count": len(stored.Geometry),
                "constraint_count": len(stored.Constraints),
            },
            "body": {"name": body.Name, "type_id": body.TypeId, "created": created_body},
            "attachment": {
                "mode": "datum_plane",
                # Which property took the attachment is evidence, not trivia:
                # it is how a host that renamed the property is recognised.
                "property": attachment_property,
                "plane_object": plane_name,
                "plane": plane,
                "normal": list(rules.plane_normal(plane)),
            },
            "dof": dof,
            "dof_source": dof_source,
            "feature_state": state,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def sketch_add_geometry(params):
    """Add typed geometry to a sketch and prove every element landed."""
    import FreeCAD as App
    import Part

    tool = "sketch.add_geometry"
    version = _host_version()
    rules = _sketch_module()
    sketch_name = _required(params, "sketch_name", tool)
    specs = _required(params, "geometry", tool)
    if not isinstance(specs, list) or not specs:
        raise ValueError("%s: geometry must be a non-empty list of geometry items" % tool)
    if len(specs) > 100:
        raise ValueError("%s: geometry may contain at most 100 items" % tool)
    primitives = []
    for spec in specs:
        primitives.extend(rules.expand_geometry(spec, tool))
    doc = _open_document(App, params["document_path"])
    try:
        sketch = _open_sketch(doc, sketch_name, tool)
        before = len(sketch.Geometry)
        dof_before, _source = _sketch_dof(sketch, version, tool)
        sketch.addGeometry([_build_geometry(App, Part, item) for item in primitives], False)
        doc.recompute()
        _save_document(doc)
        after = len(sketch.Geometry)
        read_back = _ReadBack(tool, version, params)
        read_back.check(
            after == before + len(primitives),
            "geometry.count",
            before + len(primitives),
            after,
            "FreeCAD accepted the call but the geometry count did not grow by the number "
            "of requested elements, so part of the profile is missing.",
        )
        elements = []
        for offset, primitive in enumerate(primitives):
            index = before + offset
            geo = sketch.Geometry[index]
            kind = _geometry_kind(geo)
            read_back.check(
                kind == primitive["kind"],
                "geometry[%d].kind" % index,
                primitive["kind"],
                kind,
                "The stored element is a different kind than requested, so the profile is "
                "not the profile that was asked for.",
            )
            actual = _geometry_read_back(geo, kind)
            read_back.sequences(
                "geometry[%d].points" % index,
                rules.key_points(primitive),
                actual,
                "FreeCAD stored the element but with different coordinates, so the "
                "geometry is not the geometry that was asked for.",
            )
            elements.append(
                {
                    "index": index,
                    "kind": kind,
                    "type": primitive["type"],
                    "role": primitive["role"],
                    "construction": _construction_flag(sketch, index),
                    "key_points": actual,
                }
            )
        dof_after, dof_source = _sketch_dof(sketch, version, tool)
        state = rules.feature_state(dof_after, after)
        return {
            "sketch": {
                "name": sketch.Name,
                "label": sketch.Label,
                "geometry_count": after,
                "constraint_count": len(sketch.Constraints),
            },
            "elements": elements,
            "dof": {"before": dof_before, "after": dof_after, "source": dof_source},
            "feature_state": state,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def sketch_add_constraint(params):
    """Add typed constraints to a sketch and prove every one of them landed."""
    import FreeCAD as App
    import Sketcher

    tool = "sketch.add_constraint"
    version = _host_version()
    rules = _sketch_module()
    sketch_name = _required(params, "sketch_name", tool)
    specs = _required(params, "constraints", tool)
    if not isinstance(specs, list) or not specs:
        raise ValueError("%s: constraints must be a non-empty list of constraints" % tool)
    if len(specs) > 200:
        raise ValueError("%s: constraints may contain at most 200 items" % tool)
    normalized = [rules.validate_constraint(spec, tool) for spec in specs]
    doc = _open_document(App, params["document_path"])
    try:
        sketch = _open_sketch(doc, sketch_name, tool)
        before = len(sketch.Constraints)
        dof_before, _source = _sketch_dof(sketch, version, tool)
        # Validate every reference before the first write: a half-applied
        # constraint batch is a sketch that looks finished and is not.
        for constraint in normalized:
            _check_constraint_references(sketch, constraint, tool)
        for constraint in normalized:
            sketch.addConstraint(_build_constraint(Sketcher, constraint, tool))
        doc.recompute()
        _save_document(doc)
        after = len(sketch.Constraints)
        read_back = _ReadBack(tool, version, params)
        read_back.check(
            after == before + len(normalized),
            "constraint.count",
            before + len(normalized),
            after,
            "FreeCAD accepted the call but stored fewer constraints than requested, so "
            "the sketch is less constrained than the caller believes.",
        )
        added = []
        for offset, constraint in enumerate(normalized):
            index = before + offset
            host_constraint = sketch.Constraints[index]
            read_back.check(
                str(host_constraint.Type) == constraint["free_cad_type"],
                "constraint[%d].type" % index,
                constraint["free_cad_type"],
                str(host_constraint.Type),
                "The stored constraint is a different kind than requested.",
            )
            expected_value = _expected_constraint_value(constraint)
            if expected_value is not None:
                read_back.numbers(
                    "constraint[%d].value" % index,
                    expected_value,
                    getattr(host_constraint, "Value", None),
                    "The constraint was stored with a different magnitude; a silently "
                    "rescaled dimension is how a profile ends up the wrong size.",
                )
            added.append(_constraint_payload(index, host_constraint))
        dof_after, dof_source = _sketch_dof(sketch, version, tool)
        state = rules.feature_state(dof_after, len(sketch.Geometry))
        return {
            "sketch": {
                "name": sketch.Name,
                "label": sketch.Label,
                "geometry_count": len(sketch.Geometry),
                "constraint_count": after,
            },
            "constraints": added,
            "dof": {"before": dof_before, "after": dof_after, "source": dof_source},
            "feature_state": state,
            "verified": _verified_checks(read_back),
        }
    finally:
        _close_document(App, doc)


def sketch_info(params):
    """Report a sketch's geometry, constraints and degrees of freedom."""
    import FreeCAD as App

    tool = "sketch.info"
    version = _host_version()
    rules = _sketch_module()
    name = _required(params, "sketch_name", tool)
    require_ready = bool(params.get("require_fully_constrained"))
    doc = _open_document(App, params["document_path"])
    try:
        doc.recompute()
        sketch = _open_sketch(doc, name, tool)
        geometry = []
        for index, geo in enumerate(sketch.Geometry):
            kind = _geometry_kind(geo)
            geometry.append(
                {
                    "index": index,
                    "kind": kind,
                    "construction": _construction_flag(sketch, index),
                    "key_points": _geometry_read_back(geo, kind),
                }
            )
        constraints = [
            _constraint_payload(index, item) for index, item in enumerate(sketch.Constraints)
        ]
        dof, dof_source = _sketch_dof(sketch, version, tool)
        state = rules.feature_state(dof, len(sketch.Geometry))
        # The matrix records ExternalGeometryCount as removed on 1.1+; the list
        # is the spelling that survives, so the list is what gets counted. A
        # plausible-looking count read off a removed property is precisely the
        # failure the sketch rules exist to prevent.
        external = getattr(sketch, "ExternalGeometry", None)
        external_count = len(external) if external is not None else None
        shape = getattr(sketch, "Shape", None)
        topology = None
        if shape is not None and not shape.isNull():
            topology = {
                "valid": bool(shape.isValid()),
                "vertices": len(shape.Vertexes),
                "edges": len(shape.Edges),
                "wires": len(shape.Wires),
                "faces": len(shape.Faces),
            }
        if require_ready:
            rules.assert_feature_ready(state, name, tool)
        return {
            "sketch": {
                "name": sketch.Name,
                "label": sketch.Label,
                "type_id": sketch.TypeId,
                "body": _sketch_body_name(sketch),
                "plane": _sketch_plane(sketch),
                "support": [
                    [getattr(item, "Name", None), sub] for item, sub in _attachment_support(sketch)
                ],
                "geometry_count": len(geometry),
                "constraint_count": len(constraints),
            },
            "geometry": geometry,
            "constraints": constraints,
            "external_geometry_count": external_count,
            "topology": topology,
            "dof": dof,
            "dof_source": dof_source,
            "feature_state": state,
            "require_fully_constrained": require_ready,
        }
    finally:
        _close_document(App, doc)


def _length_extent(params, tool, key="length"):
    """A positive extrusion or cut length, refused with a code when it is not.

    A zero or negative extent is refused before anything is written: it cannot
    produce the volume the read-back demands, so writing it would only produce
    a feature guaranteed to fail the postcondition.
    """
    value = _required(params, key, tool)
    try:
        return _positive(value, key)
    except ValueError:
        raise _coded(
            "E_EXTENT_DEGENERATE",
            "%s: %s must be a finite number greater than zero, got %r" % (tool, key, value),
        ) from None


def _angle_extent(params, tool, key="angle_degrees"):
    """A revolution or groove sweep angle, in degrees.

    A full turn is 360 and is allowed: it is a legitimate solid of revolution.
    Zero is refused, because a zero sweep removes or adds nothing at all and
    would pass a volume check that only looks at direction.
    """
    angle = float(_required(params, key, tool))
    if not math.isfinite(angle) or angle <= 0.0 or angle > 360.0:
        raise _coded(
            "E_ANGLE_INVALID",
            "%s: %s must be a finite angle greater than 0 and at most 360 degrees, got %r"
            % (tool, key, angle),
        )
    return angle


def _revolution_expected_volume(sketch, profile_area, angle_degrees, axis, tool):
    """The volume a revolution must sweep, by Pappus's centroid theorem.

    A profile revolved about an axis sweeps its area along the circular path its
    centroid travels: volume = area x 2*pi*r x (angle / 360), where r is the
    distance from the profile's centroid to the axis.

    The centroid distance is the whole point. Deriving it from the host rather
    than assuming it is what makes the bound meaningful: an axis that runs
    *through* the profile sweeps almost nothing, while one outside it sweeps a
    toroidal volume -- and a sign or a missing factor here would make the check
    pass on geometry that is wrong.
    """
    shape = getattr(sketch, "Shape", None)
    centre = getattr(shape, "CenterOfMass", None) if shape is not None else None
    if centre is None:
        raise _coded(
            "E_PROFILE_DEGENERATE",
            "%s: the host does not report the profile's centre of mass, so the volume a "
            "revolution must sweep cannot be derived and the result could not be verified" % tool,
        )
    # Distance from the axis, in the sketch's own plane. "vertical" revolves
    # about the sketch's Y axis, so the radius is the X offset of the centroid;
    # "horizontal" is the transpose.
    offset = float(centre.x) if axis == "vertical" else float(centre.y)
    radius = abs(offset)
    fraction = float(angle_degrees) / 360.0
    return float(profile_area) * 2.0 * math.pi * radius * fraction


def partdesign_pad(params):
    """Extrude a sketch into a solid, proving the body gained volume."""
    tool = "partdesign.pad"
    length = _length_extent(params, tool)
    side_type = params.get("side_type") or "one_side"

    def configure(feature):
        if _has_property(feature, "Length"):
            feature.Length = length
        else:
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Length property on %s" % (tool, feature.TypeId),
            )
        if side_type == "two_sides" and _has_property(feature, "Length2"):
            feature.Length2 = length

    return _feature_result(
        params, tool, _PAD_TYPE_ID, "add", configure, lambda area, sketch: area * length
    )


def partdesign_pocket(params):
    """Cut a sketch out of a body, proving the body lost volume.

    This tool exists because of upstream FreeCAD issue #99, where a pocket
    reports success and a Valid state while removing no material. The volume
    read-back is the fix: a pocket that did not reduce the body's volume is an
    error carrying the expected and actual deltas, never a successful result
    with a smaller number in it.
    """
    tool = "partdesign.pocket"
    length = _length_extent(params, tool)
    side_type = params.get("side_type") or "one_side"

    def configure(feature):
        if _has_property(feature, "Length"):
            feature.Length = length
        else:
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Length property on %s" % (tool, feature.TypeId),
            )
        if side_type == "two_sides" and _has_property(feature, "Length2"):
            feature.Length2 = length

    return _feature_result(
        params, tool, _POCKET_TYPE_ID, "remove", configure, lambda area, sketch: area * length
    )


def _revolution_extent(params, tool):
    """The shared extent setup for Revolution and Groove.

    Both sweep a profile about an axis and neither carries a reversal property:
    a host that added ``SideType`` to them would be expressing a different
    concept, so the side-type ladder is deliberately never applied here. Their
    symmetry is a separate ``Symmetric``-to-the-axis question this adapter does
    not guess at, because guessing is what produces a solid of the wrong extent
    while reporting success.
    """
    angle = _angle_extent(params, tool)
    axis_name = params.get("axis") or "vertical"

    def configure(feature):
        if _has_property(feature, "Angle"):
            feature.Angle = angle
        else:
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Angle property on %s" % (tool, feature.TypeId),
            )
        # The axis is expressed as a reference to the sketch's own vertical or
        # horizontal axis, which is what PartDesign's revolution axis expects.
        # Anything the host does not accept is reported rather than coerced.
        if axis_name != "vertical" and _has_property(feature, "Axis"):
            feature.Axis = axis_name

    return angle, configure, axis_name


def partdesign_revolution(params):
    """Revolve a sketch about an axis into a solid.

    Revolution and Groove are handled separately from pad and pocket because
    they carry no side-type property: there is no second side to grow towards,
    only an angle to sweep. Applying the side-type ladder here would write to a
    property the host either ignores or does not have, which is the silent
    no-op this module refuses to perform.
    """
    tool = "partdesign.revolution"
    angle, configure, axis = _revolution_extent(params, tool)

    def extent_for_area(area, sketch):
        return _revolution_expected_volume(sketch, area, angle, axis, tool)

    return _feature_result(params, tool, _REVOLUTION_TYPE_ID, "add", configure, extent_for_area)


def partdesign_groove(params):
    """Cut a revolved profile out of a body, proving the body lost volume."""
    tool = "partdesign.groove"
    angle, configure, axis = _revolution_extent(params, tool)

    def extent_for_area(area, sketch):
        return _revolution_expected_volume(sketch, area, angle, axis, tool)

    return _feature_result(params, tool, _GROOVE_TYPE_ID, "remove", configure, extent_for_area)


def partdesign_loft(params):
    """Loft through a list of sketches, proving the body gained volume."""
    tool = "partdesign.loft"
    sections = _required(params, "sketch_names", tool)
    if not isinstance(sections, list) or len(sections) < 2:
        raise _coded(
            "E_SECTIONS_REQUIRED",
            "%s: sketch_names must list at least two sketches to loft through, got %r"
            % (tool, sections),
        )
    length = _length_extent(params, tool)

    def extent_for_area(area, sketch):
        return area * length

    return _feature_result(
        params,
        tool,
        _LOFT_TYPE_ID,
        "add",
        _sections_configure(sections, tool, length),
        extent_for_area,
    )


def _sections_configure(sections, tool, length=None):
    """Bind a list of profile sketches to a loft or sweep feature."""

    def configure(feature):
        if not _has_property(feature, "Sections"):
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Sections property on %s" % (tool, feature.TypeId),
            )
        feature.Sections = list(sections)
        if _has_property(feature, "Length"):
            feature.Length = length

    return configure


def partdesign_sweep(params):
    """Sweep a profile sketch along a path sketch, proving the body gained volume."""
    tool = "partdesign.sweep"
    path_name = _required(params, "path_sketch_name", tool)
    length = _length_extent(params, tool)

    def configure(feature):
        if not _has_property(feature, "Sections"):
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Sections property on %s" % (tool, feature.TypeId),
            )
        if not _has_property(feature, "Spine"):
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Spine property on %s" % (tool, feature.TypeId),
            )
        feature.Sections = [params["sketch_name"]]
        feature.Spine = path_name
        if _has_property(feature, "Length"):
            feature.Length = length

    return _feature_result(
        params, tool, _SWEEP_TYPE_ID, "add", configure, lambda area, sketch: area * length
    )


def partdesign_hole(params):
    """Cut a hole at a sketch point, proving the body lost volume.

    The hole's own geometry is what determines how much material it removes, so
    the expected volume is a cylinder of the requested diameter and depth rather
    than a function of the profile sketch's area. That number is what makes a
    hole that drilled nothing distinguishable from one that did.
    """
    tool = "partdesign.hole"
    diameter = _positive(_required(params, "diameter", tool), "diameter")
    depth = _positive(_required(params, "depth", tool), "depth")
    hole_type = params.get("hole_type") or "none"
    if hole_type not in _HOLE_TYPES:
        raise _coded(
            "E_HOLE_TYPE_INVALID",
            "%s: unsupported hole_type: %s (supported: %s)"
            % (tool, hole_type, ", ".join(_HOLE_TYPES)),
        )
    expected_area = math.pi * (diameter / 2.0) ** 2

    def configure(feature):
        if not _has_property(feature, "Diameter"):
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Diameter property on %s" % (tool, feature.TypeId),
            )
        if not _has_property(feature, "Depth"):
            raise _coded(
                "E_FEATURE_UNSUPPORTED",
                "%s: the host exposes no Depth property on %s" % (tool, feature.TypeId),
            )
        feature.Diameter = diameter
        feature.Depth = depth
        if hole_type != "none" and _has_property(feature, "HoleCutType"):
            feature.HoleCutType = hole_type

    return _feature_result(
        params,
        tool,
        _HOLE_TYPE_ID,
        "remove",
        configure,
        lambda area, sketch: expected_area * depth,
    )


def main():
    request_path = sys.argv[-2]
    result_path = sys.argv[-1]
    try:
        with open(request_path, "r", encoding="utf-8") as stream:
            request = json.load(stream)
        method = request.get("method")
        if method not in _METHODS:
            raise ValueError("Unknown FreeCAD method: %s" % method)
        if method != "system.status":
            # Pre-flight gate: an unverified host must not reach geometry work.
            _require_supported_host(_host_version())
        result = _METHODS[method](request.get("params") or {})
        payload = {"ok": True, "result": result}
    except Exception as exc:
        payload = {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        # A typed refusal from the geometry tools carries a stable code, so the
        # caller can branch on "why" instead of parsing a sentence.
        code = getattr(exc, "code", None)
        if code:
            payload["error"]["code"] = code
        # A read-back mismatch carries expected/actual across the process
        # boundary verbatim, so the caller can act on the numbers instead of
        # re-reading a sentence. A structured FEM failure carries its error code
        # the same way: the code decides the next action, so it must survive the
        # boundary as a field rather than as prose.
        verification = getattr(exc, "payload", None)
        if isinstance(verification, dict):
            if verification.get("error_code"):
                payload["error"]["error_code"] = verification["error_code"]
                payload["error"]["fem_error"] = verification
            else:
                payload["error"]["write_verification"] = verification
        # A sketch that must not be consumed carries the measured state that
        # explains the refusal, so the caller can act on the numbers instead
        # of re-reading a sentence that differs between host versions.
        state = getattr(exc, "state_payload", None)
        if isinstance(state, dict):
            payload["error"]["sketch_state"] = state
    with open(result_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)


# ---------------------------------------------------------------------------
# FEM structural analysis
#
# A structural solve is the one operation in this adapter whose answer can look
# entirely plausible and still be wrong by three orders of magnitude: the solver
# is unit-agnostic, so it converges just as happily on a load expressed in kN
# when the caller meant N. Two guards make that impossible here.
#
#   * Every quantity entering the analysis carries its unit and is converted
#     through a declared table (``fem_contract``). A bare float is refused
#     rather than assumed.
#   * The generated solver input is read back before any number is reported:
#     node coordinates identify the unit schema the workbench actually wrote,
#     and the summed ``*CLOAD`` block proves the applied force is the force that
#     was asked for. An unverifiable schema is an error, never a guess.
#
# A solve never touches the caller's document: the bridge stages a copy into the
# solver work directory, this driver only ever opens that copy, and the
# resulting document object is never saved.
# ---------------------------------------------------------------------------

_CCX_BINARY_NAMES = ("ccx", "ccx_2.22", "ccx_2.21", "ccx_2.20", "ccx_2.19", "ccx_2.18")
_GMSH_BINARY_NAMES = ("gmsh",)
# FEM ships most of its objects as Python features: the type id you pass to
# addObject is the *base* document type, and the behaviour lives in a Proxy that
# must be attached afterwards. The friendly names ("Fem::SolverCcxTools",
# "Fem::FemMeshGmsh") are only Proxy `Type` markers -- addObject rejects them.
_SOLVER_TYPE_IDS = ("Fem::FemSolverObjectPython",)
_SOLVER_PROXIES = (("femobjects.solver_ccxtools", "SolverCcxTools"),)
_MESH_TYPE_IDS = ("Fem::FemMeshShapeBaseObjectPython",)
_MESH_PROXIES = (("femobjects.mesh_gmsh", "MeshGmsh"),)
_MATERIAL_TYPE_IDS = ("App::MaterialObjectPython",)
_MATERIAL_PROXIES = (("femobjects.material_common", "MaterialCommon"),)
_ANALYSIS_TYPE_ID = "Fem::FemAnalysis"
_MESH_SIZE_PROPERTIES = ("CharacteristicLengthMax", "MeshSize", "MaxSize", "MaxElementSize")
_MESH_GEOMETRY_PROPERTIES = ("Shape", "Part")
_DEFAULT_MESH_SIZE_MM = 2.0
_FEM_LOG_TAIL = 4000
_FEM_ELEMENT_KINDS = {"Face": "Faces", "Edge": "Edges", "Vertex": "Vertexes"}


def _fem_error(code, message, version, remediation=None, details=None, params=None):
    return _fem_module().FemError(
        code,
        message,
        remediation=remediation,
        details=details,
        host_version=version,
        params=params,
    )


def _fem_preference(app, key, group):
    """Read a FEM preference, tolerating a group path that moved across hosts."""
    for base in (
        "User parameter:BaseApp/Preferences/Mod/Fem/%s" % group,
        "User parameter:BaseApp/Preferences/Mod/Fem",
    ):
        try:
            value = (app.ParamGet(base).GetString(key, "") or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return ""


def _set_fem_preference(app, key, group, value):
    """Point the workbench at a solver binary this driver found, best effort.

    A host that ignores the write still runs: the binary is passed to the
    solver explicitly as well, so this is a convenience for the workbench's own
    prerequisite check, not the only channel.
    """
    try:
        app.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/%s" % group).SetString(key, value)
    except Exception:
        pass


def _discover_binary(names, preference_value):
    """Resolve an external binary: an operator-configured path, then PATH."""
    if preference_value:
        candidate = os.path.abspath(os.path.expanduser(preference_value))
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for name in names:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    return None


_USAGE_OUTPUT_MARKERS = ("usage:", "usage ", "options:", "unknown option", "invalid option")


def _binary_version(path):
    """Ask a solver binary to identify itself; unavailability is not fatal.

    These binaries disagree about the flag: ccx takes ``-v``, gmsh only
    understands ``--version``. A version string is a line that carries a digit
    and does not look like a usage message, so a binary that rejects the flag
    yields ``None`` rather than a help banner reported as a version.
    """
    for flag in ("-v", "--version"):
        try:
            completed = subprocess.run(
                [path, flag],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=30,
            )
        except Exception:
            continue
        text = (completed.stdout or "") + (completed.stderr or "")
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            lowered = candidate.lower()
            if any(marker in lowered for marker in _USAGE_OUTPUT_MARKERS):
                continue
            if any(character.isdigit() for character in candidate):
                return candidate[:120]
    return None
    return None


def _fem_objects_constructible(app):
    """Build the FEM objects a solve needs in a throwaway document, then delete it.

    A probe that only imports modules answers "is FEM installed", not "can a
    solve run here". Constructing the objects answers the second question, and
    the document is closed again so the probe leaves no state behind.
    """
    try:
        doc = app.newDocument("DccMcpFemProbe")
    except Exception as exc:
        return {"ok": False, "message": "cannot open a probe document (%s)" % exc, "attempts": []}
    attempts = []
    try:
        for label, type_ids, proxies in (
            ("solver", _SOLVER_TYPE_IDS, _SOLVER_PROXIES),
            ("mesh", _MESH_TYPE_IDS, _MESH_PROXIES),
            ("material", _MATERIAL_TYPE_IDS, _MATERIAL_PROXIES),
        ):
            obj, _type_id, failed = _add_fem_python_object(doc, type_ids, proxies, "Probe" + label)
            if obj is None:
                attempts.extend({"object": label, **item} for item in failed)
        if attempts:
            return {
                "ok": False,
                "message": "cannot construct FEM objects: %s" % _format_attempts(attempts),
                "attempts": attempts,
            }
        return {"ok": True, "message": None, "attempts": []}
    finally:
        try:
            app.closeDocument(doc.Name)
        except Exception:
            pass


def _fem_availability(app):
    """Report what this host can actually do, without attempting a solve."""
    contract = _fem_module()
    version = _host_version()
    blocking = []
    try:
        __import__("Fem")
        fem_module = True
    except Exception as exc:
        fem_module = False
        blocking.append(
            {
                "code": contract.FemError.ERROR_WORKBENCH_MISSING,
                "message": "the FreeCAD FEM workbench is not importable (%s)" % exc,
                "remediation": "Install a FreeCAD build that ships the FEM workbench.",
            }
        )
    try:
        __import__("femtools.ccxtools")
        ccxtools = True
    except Exception as exc:
        ccxtools = False
        blocking.append(
            {
                "code": contract.FemError.ERROR_SOLVER_API,
                "message": "femtools.ccxtools is not importable (%s)" % exc,
                "remediation": "Install a FreeCAD build that ships the FEM CalculiX solver tools.",
            }
        )
    ccx = _discover_binary(_CCX_BINARY_NAMES, _fem_preference(app, "ccxBinaryPath", "Ccx"))
    gmsh = _discover_binary(_GMSH_BINARY_NAMES, _fem_preference(app, "gmsh_binary_path", "Gmsh"))
    if not ccx:
        blocking.append(
            {
                "code": contract.FemError.ERROR_SOLVER_MISSING,
                "message": "the CalculiX solver binary (ccx) was not found on PATH or in the FEM "
                "preference ccxBinaryPath",
                "remediation": "Install CalculiX (Debian/Ubuntu: `apt-get install calculix-ccx`; "
                "conda: `conda install -c conda-forge calculix`) and either put `ccx` on PATH or "
                "set the FreeCAD FEM preference Mod/Fem/Ccx/ccxBinaryPath to it.",
            }
        )
    if not gmsh:
        blocking.append(
            {
                "code": contract.FemError.ERROR_MESHER_MISSING,
                "message": "the Gmsh mesher binary was not found on PATH or in the FEM preference "
                "gmsh_binary_path",
                "remediation": "Install Gmsh (Debian/Ubuntu: `apt-get install gmsh`; conda: "
                "`conda install -c conda-forge gmsh`) and either put `gmsh` on PATH or set the "
                "FreeCAD FEM preference Mod/Fem/Gmsh/gmsh_binary_path to it.",
            }
        )
    # Importability is not the same as usability: a host can import Fem and ship
    # both binaries yet still be unable to construct the objects a solve needs.
    # Build each one in a throwaway document and throw it away, so the probe
    # reports the same verdict a real solve would reach.
    constructible = _fem_objects_constructible(app)
    if not constructible["ok"]:
        blocking.append(
            {
                "code": contract.FemError.ERROR_SOLVER_API,
                "message": constructible["message"],
                "remediation": "Install a FreeCAD build whose FEM workbench ships the "
                "CalculiX solver, Gmsh mesh and solid material objects.",
            }
        )
    return {
        "host_version": version,
        "fem_workbench": fem_module,
        "solver_tools": ccxtools,
        "objects_constructible": constructible,
        "solver": {
            "name": "calculix",
            "binary": ccx,
            "version": _binary_version(ccx) if ccx else None,
        },
        "mesher": {
            "name": "gmsh",
            "binary": gmsh,
            "version": _binary_version(gmsh) if gmsh else None,
        },
        "available": not blocking,
        "status": "ready" if not blocking else "host_limited",
        "blocking": blocking,
        "remediation": [item["remediation"] for item in blocking],
    }


def _is_fem_analysis(obj):
    if obj is None:
        return False
    is_derived = getattr(obj, "isDerivedFrom", None)
    if callable(is_derived):
        try:
            if is_derived(_ANALYSIS_TYPE_ID):
                return True
        except Exception:
            pass
    return str(getattr(obj, "TypeId", "")) == _ANALYSIS_TYPE_ID


def _fem_checks(version, params):
    """Bounded read-back recorder for one solve (see ``_ReadBack``)."""

    class _Checks:
        def __init__(self):
            self.names = []

        def record(self, name):
            """Note a check that already ran, so it appears in the evidence."""
            self.names.append(name)

        def check(self, condition, name, expected, actual, code, remediation, extra=None):
            """Fail with a named code, carrying whatever evidence the caller has.

            ``extra`` is how solver output reaches the error surface. A failure
            here often means the solver exited successfully but produced
            nothing, and without its own output the only thing left to report is
            the name of the check that failed -- which says nothing about why.
            """
            self.names.append(name)
            if condition:
                return True
            details = {
                "check": name,
                "expected": _fem_module().jsonable(expected),
                "actual": _fem_module().jsonable(actual),
                "verified": self.names,
            }
            if extra:
                details.update(extra)
            raise _fem_error(
                code,
                "run_fem_analysis could not verify %s" % name,
                version,
                remediation=remediation,
                details=details,
                params=params,
            )

    return _Checks()


def _element_resolves(shape, name, kind):
    """True when this host resolves ``name`` to a real sub-element of ``shape``.

    FreeCAD 1.0 moved sub-element naming, so a name that was valid on 1.0.x can
    resolve to nothing on 1.1.x. The reference is only accepted when the host
    resolves it to the sub-element the caller picked; otherwise the constraint
    would silently apply to a different face (or none) and the solve would still
    converge.
    """
    try:
        found = shape.getElement(name)
    except Exception:
        return False
    return getattr(found, "ShapeType", "") == kind


def _face_entries(shape):
    """Index-ordered face descriptors carrying the host's own element name."""
    entries = []
    for index, face in enumerate(getattr(shape, "Faces", ()) or (), start=1):
        name = "Face%d" % index
        center = getattr(face, "CenterOfMass", None)
        normal = None
        try:
            u_min, u_max, v_min, v_max = face.ParameterRange
            normal = face.normalAt((u_min + u_max) / 2.0, (v_min + v_max) / 2.0)
        except Exception:
            normal = None
        entries.append(
            {
                "index": index,
                "name": name,
                "area": getattr(face, "Area", None),
                "center": [center.x, center.y, center.z] if center is not None else None,
                "normal": [normal.x, normal.y, normal.z] if normal is not None else None,
                "resolves": _element_resolves(shape, name, "Face"),
            }
        )
    return entries


def _resolve_reference(doc, text, tool, version):
    """Turn ``"Object:Face1"`` into a solved FEM reference tuple."""
    contract = _fem_module()
    object_name, element = contract.parse_reference(text)
    kind = element.rstrip("0123456789")
    collection_name = _FEM_ELEMENT_KINDS.get(kind)
    if collection_name is None:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: unsupported sub-element type in %r" % (tool, text),
            version,
            remediation="Use a Face, Edge or Vertex reference.",
        )
    obj = doc.getObject(object_name)
    if obj is None:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: the document has no object named %s" % (tool, object_name),
            version,
            remediation="Inspect the document and reference an object that exists.",
        )
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: %s has no shape to constrain" % (tool, object_name),
            version,
            remediation="Reference an object with a non-null Part shape.",
        )
    index = int(element[len(kind) :])
    if not 1 <= index <= len(getattr(shape, collection_name, ()) or ()):
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: %s has no %s" % (tool, object_name, element),
            version,
            remediation="Use list_faces to discover the references this host reports.",
        )
    if not _element_resolves(shape, element, kind):
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: this host does not resolve %r on %s" % (tool, text, object_name),
            version,
            remediation="Sub-element naming moved between FreeCAD releases; call list_faces "
            "on this host and use a reference it reports as resolvable.",
        )
    return (obj, (element,)), {
        "object": object_name,
        "element": element,
        "reference": "%s:%s" % (object_name, element),
    }


def _format_attempts(attempts):
    """Render object-construction failures as one diagnostic line.

    The original error text is kept: without it a host that rejects a type id is
    indistinguishable from a host that is missing the workbench entirely.
    """
    if not attempts:
        return "the host offered no candidate type"
    return "; ".join(
        "%s (%s): %s" % (item["type_id"], item["stage"], item["error"]) for item in attempts
    )


def _add_fem_python_object(doc, type_ids, proxies, name):
    """Create a FEM Python-feature object, attaching the Proxy it needs.

    FEM objects are documented by their friendly type name but created from a
    generic base type id; without the Proxy the object has none of the
    properties the solver workbench expects. Returns ``(object, type_id,
    attempts)``; on failure the object is ``None`` and ``attempts`` carries the
    error text for every stage tried, so a host that cannot build the object
    says why instead of only saying that it cannot.
    """
    attempts = []
    for type_id in type_ids:
        obj = None
        try:
            obj = doc.addObject(type_id, name)
        except Exception as exc:
            attempts.append({"type_id": type_id, "stage": "addObject", "error": "%s" % exc})
            continue
        for module_path, class_name in proxies:
            try:
                module = __import__(module_path, fromlist=[class_name])
                getattr(module, class_name)(obj)
            except Exception as exc:
                attempts.append(
                    {
                        "type_id": type_id,
                        "stage": "%s.%s" % (module_path, class_name),
                        "error": "%s" % exc,
                    }
                )
                obj = None
                break
        if obj is not None:
            return obj, type_id, attempts
    return None, None, attempts


def _set_first_property(obj, names, value):
    """Set the first property in ``names`` the host exposes; return its name.

    Host API drift is reported rather than absorbed: the caller decides whether
    an unset property is fatal and names it in the result.
    """
    for name in names:
        if not hasattr(obj, name):
            continue
        try:
            setattr(obj, name, value)
        except Exception:
            continue
        return name
    return None


def _create_fem_analysis(app, doc, params, version):
    """Build a complete typed static analysis: mesh, material, fixed, load."""
    contract = _fem_module()
    tool = "analysis.run_fem"
    target_name = params.get("target_object")
    if not target_name:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s requires target_object when analysis_name is not given" % tool,
            version,
            remediation="Name the object to analyse, or pass analysis_name to reuse an analysis "
            "the document already contains.",
        )
    target = doc.getObject(target_name)
    if target is None:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: the document has no object named %s" % (tool, target_name),
            version,
        )
    shape = getattr(target, "Shape", None)
    if shape is None or shape.isNull() or not shape.isValid():
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: %s does not carry a valid shape" % (tool, target_name),
            version,
            remediation="Validate the document, then analyse an object with a valid solid.",
        )
    if not shape.Solids:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s: %s has no solid, so it cannot be meshed for a structural solve"
            % (tool, target_name),
            version,
            remediation="Analyse a solid body.",
        )
    if params.get("load") is None:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_LOAD,
            "%s requires load when analysis_name is not given" % tool,
            version,
            remediation="Pass load.force as a {value, unit} object plus a direction and faces.",
        )
    load = contract.load_payload(params["load"])
    fixed_texts = [str(item) for item in (params.get("fixed_faces") or ())]
    if not fixed_texts:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_REFERENCE,
            "%s requires at least one entry in fixed_faces when analysis_name is not given" % tool,
            version,
            remediation="A static solve needs a restraint; name the faces to fix.",
        )
    if not load["faces"]:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_LOAD,
            "%s requires load.faces" % tool,
            version,
            remediation="Name the faces the force is applied to.",
        )
    fixed_references = []
    fixed_reported = []
    for text in fixed_texts:
        reference, reported = _resolve_reference(doc, text, tool, version)
        fixed_references.append(reference)
        fixed_reported.append(reported)
    load_references = []
    load_reported = []
    for text in load["faces"]:
        reference, reported = _resolve_reference(doc, text, tool, version)
        load_references.append(reference)
        load_reported.append(reported)
    shared = sorted(
        {item["reference"] for item in fixed_reported}
        & {item["reference"] for item in load_reported}
    )
    if shared:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_LOAD,
            "%s: %s is both fixed and loaded" % (tool, ", ".join(shared)),
            version,
            remediation="A face cannot carry a restraint and the applied load at once.",
        )
    for reference in fixed_references + load_references:
        if reference[0] is not target:
            raise _fem_error(
                contract.FemError.ERROR_INVALID_REFERENCE,
                "%s: every reference must belong to %s" % (tool, target_name),
                version,
            )
    material = contract.material_payload(params.get("material"))
    mesh_size = params.get("mesh_size") or {"value": _DEFAULT_MESH_SIZE_MM, "unit": "mm"}
    if not isinstance(mesh_size, dict):
        raise _fem_error(
            contract.FemError.ERROR_INVALID_MESH_SIZE,
            "%s: mesh_size must be a {value, unit} object" % tool,
            version,
        )
    mesh_quantity = contract.quantity_payload(
        mesh_size.get("value"), mesh_size.get("unit"), "length"
    )
    if mesh_quantity["value"] <= 0:
        raise _fem_error(
            contract.FemError.ERROR_INVALID_MESH_SIZE,
            "%s: mesh_size must be positive" % tool,
            version,
        )

    solver, solver_type_id, solver_attempts = _add_fem_python_object(
        doc,
        _SOLVER_TYPE_IDS,
        _SOLVER_PROXIES,
        "DccMcpSolverCcx",
    )
    if solver is None:
        raise _fem_error(
            contract.FemError.ERROR_SOLVER_API,
            "%s: this host cannot build a CalculiX solver object; the attempts were: %s"
            % (tool, _format_attempts(solver_attempts)),
            version,
            remediation="Install a FreeCAD build that ships the FEM CalculiX solver object.",
            details={"attempts": solver_attempts},
        )
    analysis = doc.addObject(_ANALYSIS_TYPE_ID, "DccMcpAnalysis")
    mesh, _mesh_type_id, mesh_attempts = _add_fem_python_object(
        doc,
        _MESH_TYPE_IDS,
        _MESH_PROXIES,
        "DccMcpMeshGmsh",
    )
    material_object, _material_type_id, material_attempts = _add_fem_python_object(
        doc,
        _MATERIAL_TYPE_IDS,
        _MATERIAL_PROXIES,
        "DccMcpMaterial",
    )
    if mesh is None or material_object is None:
        raise _fem_error(
            contract.FemError.ERROR_WORKBENCH_MISSING,
            "%s: this host cannot build the FEM mesh/material objects; the attempts were: %s"
            % (tool, _format_attempts(mesh_attempts + material_attempts)),
            version,
            remediation="Install a FreeCAD build that ships the FEM mesh and material objects.",
            details={"attempts": mesh_attempts + material_attempts},
        )
    material_object.Category = "Solid"
    fixed = doc.addObject("Fem::ConstraintFixed", "DccMcpConstraintFixed")
    force = doc.addObject("Fem::ConstraintForce", "DccMcpConstraintForce")

    # The mesh's geometry link is `Shape` (PropertyLinkGlobal) since FreeCAD 1.0;
    # `Part` was the pre-1.0 Fem::FemMeshObjectPython name and no longer exists.
    geometry_property = _set_first_property(mesh, _MESH_GEOMETRY_PROPERTIES, target)
    if geometry_property is None:
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: this host's FEM mesh object exposes none of %s, so no geometry can be "
            "attached to the mesh" % (tool, ", ".join(_MESH_GEOMETRY_PROPERTIES)),
            version,
            remediation="Without a geometry link the mesher has nothing to mesh.",
            details={"type_id": mesh.TypeId},
        )
    size_property = _set_first_property(mesh, _MESH_SIZE_PROPERTIES, mesh_quantity["value"])
    if size_property is None:
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: this host's FEM mesh object exposes none of %s, so the mesh size cannot be "
            "controlled" % (tool, ", ".join(_MESH_SIZE_PROPERTIES)),
            version,
            remediation="An uncontrolled mesh size makes the result unverifiable.",
        )
    order_property = None
    if hasattr(mesh, "ElementOrder"):
        mesh.ElementOrder = "2nd"
        order_property = "ElementOrder"
    elif hasattr(mesh, "SecondOrder"):
        mesh.SecondOrder = True
        order_property = "SecondOrder"
    if order_property is None:
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: this host's FEM mesh object exposes no element order property" % tool,
            version,
            remediation="A first-order mesh is too stiff in bending to verify against an "
            "analytic solution.",
        )
    # Building a mesh object only describes a mesh; the nodes exist once Gmsh has
    # actually run. The solver reads the computed FemMesh, so without this step
    # every solve reports "the mesher produced no nodes".
    _run_mesher(app, doc, mesh, tool, version)

    material_object.Material = {
        "Name": material["name"],
        "YoungsModulus": "%r MPa" % material["youngs_modulus"]["value"],
        "PoissonRatio": str(material["poisson_ratio"]),
        "Density": "%r t/mm^3" % material["density"]["value"],
    }
    fixed.References = fixed_references
    force.References = load_references
    force.Force = _force_quantity(app, load["force"]["value"])
    # `Direction` is a PropertyLinkSub, so the axis is given as geometry; the
    # host derives DirectionVector from it. Assigning a Base::Vector here throws
    # `type must be 'DocumentObject' ... not Base.Vector` on both release lines.
    direction_line, direction_sub = _direction_reference(
        app, doc, load["unit_direction"], "DccMcpForceDirection"
    )
    force.Direction = (direction_line, direction_sub)
    if hasattr(force, "Reversed"):
        force.Reversed = False
    if hasattr(solver, "AnalysisType"):
        solver.AnalysisType = "static"

    # Write-after-read: the host resolves the axis from the line we just made, so
    # confirm it equals what was asked for. A silently reversed load converges to
    # the same magnitudes and would pass a magnitude-only check.
    applied_direction = _read_direction(app, force)
    if applied_direction is not None:
        requested = [float(component) for component in load["unit_direction"]]
        if not _vectors_agree(applied_direction, requested):
            raise _fem_error(
                contract.FemError.ERROR_INVALID_LOAD,
                "%s: the host resolved the load direction as %s but %s was requested"
                % (tool, _format_vector(applied_direction), _format_vector(requested)),
                version,
                remediation="The load axis is taken from reference geometry; the direction "
                "the host derived does not match the requested one.",
                details={"requested": requested, "applied": list(applied_direction)},
            )

    for member in (solver, mesh, material_object, fixed, force):
        analysis.addObject(member)
    return {
        "analysis": analysis,
        "solver": solver,
        "mesh": mesh,
        "material_object": material_object,
        "force": force,
        "fixed": fixed,
        "target": target,
        "material": material,
        "load": load,
        "fixed_reported": fixed_reported,
        "load_reported": load_reported,
        "mesh_size": mesh_quantity,
        "size_property": size_property,
        "order_property": order_property,
    }


def _read_direction(app, force):
    """Read the axis the host resolved for a force constraint, or ``None``.

    ``DirectionVector`` is what the solver actually uses and is maintained by the
    host from the geometry reference, so it is the value worth verifying. A host
    that has not resolved it yet reports the zero vector, which is not a
    direction and so is not compared.
    """
    vector = getattr(force, "DirectionVector", None)
    if vector is None:
        return None
    length = getattr(vector, "Length", None)
    if length is None:
        return None
    if length < 1e-9:
        return None
    return (vector.x, vector.y, vector.z)


def _vectors_agree(applied, requested, tolerance=1e-6):
    """Compare two direction triplets, tolerating a host that does not normalize.

    The reference geometry yields a unit vector, but the check should not fail on
    a host that reports a scaled one, so compare after normalizing both.
    """
    applied_norm = math.sqrt(sum(component * component for component in applied))
    requested_norm = math.sqrt(sum(component * component for component in requested))
    if applied_norm < 1e-9 or requested_norm < 1e-9:
        return False
    scaled = [component / applied_norm for component in applied]
    wanted = [component / requested_norm for component in requested]
    return all(abs(a - b) <= tolerance for a, b in zip(scaled, wanted))


def _format_vector(vector):
    return "[%s]" % ", ".join("%.6g" % component for component in vector)


def _direction_reference(app, doc, unit_direction, name="DccMcpForceDirection"):
    """Build a line along ``unit_direction`` and return it as a (object, sub) pair.

    ``Fem::ConstraintForce.Direction`` is an ``App::PropertyLinkSub``: the host
    derives the load axis from referenced geometry, not from a vector, and
    ``DirectionVector`` is read-only so it cannot be written directly. The
    reference is therefore the only way to state a direction, and a line from
    the origin along the requested axis is the simplest geometry that yields it:
    ``Fem::Tools::getDirectionFromShape`` takes a linear edge's own direction,
    which is already a unit vector.

    The line is placed outside the target's bounding box so it can never be
    mistaken for part of the model being meshed.
    """
    origin = app.Vector(0.0, 0.0, 0.0)
    tip = app.Vector(*[float(component) for component in unit_direction])
    if tip.Length < 1e-9:
        raise ValueError("a force direction must not be the zero vector")
    line = doc.addObject("Part::Line", name)
    line.X1, line.Y1, line.Z1 = origin.x, origin.y, origin.z
    line.X2, line.Y2, line.Z2 = tip.x, tip.y, tip.z
    return line, ["Edge1"]


def _force_quantity(app, newtons):
    """Express a force in N the way this host stores it, preferring a Quantity."""
    units = getattr(app, "Units", None)
    if units is not None:
        try:
            return units.Quantity("%.17g N" % newtons)
        except Exception:
            pass
    return float(newtons)


def _read_force_newtons(app, force):
    """Read a constraint force back as newtons, whatever the host stores.

    A host that stores a plain float gives no unit with it, so the value is
    reported as newtons but is not trusted: the summed ``*CLOAD`` block of the
    generated input is what actually proves the magnitude.
    """
    value = getattr(force, "Force", None)
    getter = getattr(value, "getValueAs", None)
    if callable(getter):
        try:
            return getter("N"), True
        except Exception:
            pass
    if hasattr(value, "Value"):
        return float(value.Value), True
    return float(value), False


def _fem_tools(analysis, solver, version):
    """Instantiate the host's CalculiX driver, whatever its constructor takes."""
    contract = _fem_module()
    try:
        __import__("femtools.ccxtools")
        ccxtools = sys.modules["femtools.ccxtools"]
    except Exception as exc:
        raise _fem_error(
            contract.FemError.ERROR_SOLVER_API,
            "femtools.ccxtools is not importable (%s)" % exc,
            version,
            remediation="Install a FreeCAD build that ships the FEM CalculiX solver tools.",
        ) from None
    import inspect

    try:
        parameters = inspect.signature(ccxtools.FemToolsCcx).parameters
    except (TypeError, ValueError):
        parameters = {}
    keyword_arguments = {}
    if "analysis" in parameters:
        keyword_arguments["analysis"] = analysis
    if "solver" in parameters:
        keyword_arguments["solver"] = solver
    try:
        return ccxtools.FemToolsCcx(**keyword_arguments)
    except TypeError as exc:
        raise _fem_error(
            contract.FemError.ERROR_SOLVER_API,
            "FemToolsCcx does not accept this host's constructor signature (%s)" % exc,
            version,
            remediation="The FEM solver API moved outside the verified matrix; pin a supported "
            "FreeCAD.",
        ) from None


def _run_mesher(app, doc, mesh, tool, version):
    """Generate the mesh described by ``mesh`` and verify nodes were produced.

    A Gmsh mesh object is inert until the mesher runs: ``addObject`` plus its
    Proxy only create the object and its properties. ``GmshTools.create_mesh()``
    is the entry point FreeCAD itself uses (see the worked example in
    ``femmesh/gmshtools.py``), and it returns an error string rather than
    raising, so the return value has to be checked explicitly.

    The failure is reported with the mesher's own message: "no nodes" on its own
    does not say whether Gmsh was missing, refused the geometry, or silently
    produced an empty mesh.
    """
    contract = _fem_module()
    try:
        from femmesh.gmshtools import GmshTools  # noqa: PLC0415

        mesher = GmshTools(mesh)
    except Exception as exc:
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: the Gmsh mesher could not be started: %s" % (tool, exc),
            version,
            remediation="This host ships no Gmsh mesher for the FEM workbench.",
        ) from None
    create = getattr(mesher, "create_mesh", None)
    if not callable(create):
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: this host's Gmsh mesher exposes no create_mesh method" % tool,
            version,
            remediation="The mesh cannot be generated, so there is nothing to solve.",
        )
    try:
        error = create()
    except Exception as exc:
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: the Gmsh mesher failed: %s" % (tool, exc),
            version,
            remediation="Check that gmsh is installed and the geometry is meshable.",
        ) from None
    if error:
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: the Gmsh mesher reported: %s" % (tool, error),
            version,
            remediation="The mesh was not generated, so there is nothing to solve.",
        )
    doc.recompute()
    node_count = getattr(getattr(mesh, "FemMesh", None), "NodeCount", 0) or 0
    if node_count <= 0:
        raise _fem_error(
            contract.FemError.ERROR_MESHER_MISSING,
            "%s: the mesher finished but produced no nodes" % tool,
            version,
            remediation="An empty mesh means the solver has nothing to solve; check the "
            "mesh size and the geometry being meshed.",
            details={"size_property": getattr(mesh, "CharacteristicLengthMax", None)},
        )
    return node_count


def _fem_call(fea, names, version, failure_code, what):
    """Call the first method in ``names`` the host's solver driver exposes.

    Every FEM driver call is version-sensitive, and a missing method is a
    different failure from a failing one: the first is named as an API gap, the
    second propagates with the host's own message.
    """
    contract = _fem_module()
    for name in names:
        function = getattr(fea, name, None)
        if not callable(function):
            continue
        try:
            function()
        except TypeError:
            continue
        except Exception as exc:
            raise _fem_error(
                contract.FemError.ERROR_SOLVER_API if failure_code is None else failure_code,
                "%s failed: %s" % (what, exc),
                version,
            ) from None
        return name
    raise _fem_error(
        contract.FemError.ERROR_SOLVER_API,
        "the host's FEM solver driver exposes none of %s, so %s is impossible"
        % (", ".join("%s()" % name for name in names), what),
        version,
        remediation="The FEM solver API moved outside the verified matrix; pin a supported "
        "FreeCAD.",
    )


def _setup_working_dir(fea, workdir):
    """Point the solver driver at our working directory.

    Both routes are used because they are not equivalent across hosts: the
    solver object's ``WorkingDir`` property is what some versions read, while
    ``setup_working_dir()`` is upstream's own entry point and is what
    ``FemToolsCcx.run()`` calls. Relying on only one leaves the solver writing
    to its own default location, where the mesher's output is then not the file
    we go on to read.
    """
    solver = getattr(fea, "solver", None)
    if solver is not None and hasattr(solver, "WorkingDir"):
        try:
            solver.WorkingDir = workdir
        except Exception:
            pass
    function = getattr(fea, "setup_working_dir", None)
    if not callable(function):
        return None
    try:
        function(workdir)
    except TypeError:
        function()
    # Fall back to the attribute when the call form is not accepted, so the
    # driver still resolves its input and results inside our directory.
    if getattr(fea, "working_dir", None) != str(workdir):
        try:
            fea.working_dir = str(workdir)
        except Exception:
            pass
    return "setup_working_dir"


def _latest_input_file(workdir):
    newest = None
    newest_mtime = -1.0
    for name in os.listdir(workdir):
        if not name.lower().endswith(".inp"):
            continue
        path = os.path.join(workdir, name)
        if not os.path.isfile(path):
            continue
        mtime = os.path.getmtime(path)
        if mtime >= newest_mtime:
            newest_mtime = mtime
            newest = path
    return newest


def _parse_cload(path):
    """Sum a CalculiX ``*CLOAD`` block per degree of freedom.

    Returns ``{1: fx, 2: fy, 3: fz}`` in the unit the input was written with, or
    ``None`` when the block is absent. ``None`` and a block of zeros are
    deliberately different results: the first means the load was never written
    and cannot be checked, the second means it was written as nothing, which is a
    real mismatch the caller must see.

    This is the read-back that turns a silent 1000x force error into a named
    mismatch: the workbench distributes the constraint force over the referenced
    face's nodes, so the components sum to the total the analysis applies.
    """
    if not path or not os.path.isfile(path):
        return None
    totals = {}
    inside = False
    seen = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                # CalculiX comments are `**`; keywords are a single `*`. Upstream
                # writes a `** <label>` line after `*CLOAD` and again before each
                # referenced shape's node rows, so a comment line must be skipped
                # without touching `inside` -- treating it as a keyword ends the
                # block and every node row is then discarded.
                if stripped.startswith("**"):
                    continue
                if stripped.startswith("*"):
                    inside = stripped.upper().startswith("*CLOAD")
                    seen = seen or inside
                    continue
                if not inside:
                    continue
                parts = [item.strip() for item in stripped.split(",")]
                parts = [item for item in parts if item]
                if len(parts) < 3:
                    continue
                try:
                    degree = int(float(parts[1]))
                    value = float(parts[2])
                except (TypeError, ValueError):
                    continue
                if degree not in (1, 2, 3):
                    continue
                totals[degree] = totals.get(degree, 0.0) + value
    except OSError:
        return None
    if not seen:
        return None
    return {degree: totals.get(degree, 0.0) for degree in (1, 2, 3)}


def _parse_node_extents(path):
    """Measure the axis extents of a CalculiX ``*NODE`` block."""
    if not path or not os.path.isfile(path):
        return None
    minima = [None, None, None]
    maxima = [None, None, None]
    inside = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                # `**` is a CalculiX comment, not a keyword; it must not end the
                # block. See _parse_cload for why this matters.
                if stripped.startswith("**"):
                    continue
                if stripped.startswith("*"):
                    inside = stripped.upper().startswith("*NODE")
                    continue
                if not inside:
                    continue
                parts = [item.strip() for item in stripped.split(",")]
                parts = [item for item in parts if item]
                if len(parts) < 4:
                    continue
                try:
                    coordinates = [float(item) for item in parts[1:4]]
                except (TypeError, ValueError):
                    continue
                for axis, value in enumerate(coordinates):
                    if minima[axis] is None or value < minima[axis]:
                        minima[axis] = value
                    if maxima[axis] is None or value > maxima[axis]:
                        maxima[axis] = value
    except OSError:
        return None
    if any(item is None for item in minima):
        return None
    return [maxima[axis] - minima[axis] for axis in range(3)]


def _run_ccx(binary, input_path, workdir, timeout, version):
    """Run CalculiX on a written input and return its exit code and logs."""
    contract = _fem_module()
    stem = os.path.basename(input_path)[: -len(".inp")]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [binary, stem],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
        stdout = _decode_stream(completed.stdout)
        stderr = _decode_stream(completed.stderr)
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        # On POSIX the timeout path leaves the captured output as bytes even
        # when universal_newlines is set, so decode rather than discard it --
        # the remediation below promises the partial output, and throwing it
        # away leaves an empty log where the caller was told to look.
        stdout = _decode_stream(exc.stdout)
        stderr = _decode_stream(exc.stderr)
        _write_fem_logs(workdir, stdout, stderr)
        raise _fem_error(
            contract.FemError.ERROR_SOLVER_TIMEOUT,
            "CalculiX exceeded the %.1f second solver budget" % timeout,
            version,
            remediation="The partial solver output is in the reported solver_workdir; retry with "
            "a coarser mesh_size or a larger timeout_secs.",
            details={
                "solver_workdir": workdir,
                "input_file": input_path,
                "elapsed_secs": round(time.monotonic() - started, 3),
                "partial_stdout": stdout[-_FEM_LOG_TAIL:],
                "partial_stderr": stderr[-_FEM_LOG_TAIL:],
            },
        ) from None
    log_paths = _write_fem_logs(workdir, stdout, stderr)
    if returncode != 0:
        raise _fem_error(
            contract.FemError.ERROR_SOLVER_FAILED,
            "CalculiX exited with code %s" % returncode,
            version,
            remediation="Read the solver log in the reported solver_workdir; a non-zero exit "
            "means the analysis did not converge, so no result is reported.",
            details={
                "solver_workdir": workdir,
                "input_file": input_path,
                "exit_code": returncode,
                "stdout": stdout[-_FEM_LOG_TAIL:],
                "stderr": stderr[-_FEM_LOG_TAIL:],
            },
        )
    # A zero exit does not mean the analysis was solved: CalculiX can report
    # success and still write a .frd with nodes but no result set, which surfaces
    # later as an empty result. Detect it here, where the solver's own output is
    # still in hand, instead of leaving it to a downstream node-count check that
    # cannot say why.
    frd_path = os.path.join(str(workdir), "%s.frd" % stem)
    if os.path.isfile(frd_path):
        if not _frd_has_results(frd_path):
            raise _fem_error(
                contract.FemError.ERROR_RESULTS_MISSING,
                "CalculiX exited with code 0 but wrote no result set",
                version,
                remediation="The solver did not produce results; this is what a degenerate mesh "
                "(for example a non-positive jacobian) or an unconstrained model looks like. "
                "Retry with a coarser mesh_size and check that restraints and loads land on "
                "nodes of the mesh.",
                details={
                    "solver_workdir": workdir,
                    "input_file": input_path,
                    "frd_file": frd_path,
                    "exit_code": returncode,
                    "stdout": stdout[-_FEM_LOG_TAIL:],
                    "stderr": stderr[-_FEM_LOG_TAIL:],
                },
            )
    return {
        "exit_code": returncode,
        "command": [binary, stem],
        "duration_secs": round(time.monotonic() - started, 3),
        "stdout_file": log_paths[0],
        "stderr_file": log_paths[1],
        "stdout_tail": stdout[-_FEM_LOG_TAIL:],
        "stderr_tail": stderr[-_FEM_LOG_TAIL:],
        "frd_file": frd_path if os.path.isfile(frd_path) else None,
    }


def _decode_stream(value):
    """Return solver output as text, accepting bytes, str or None.

    subprocess hands back bytes on some paths and str on others, and None when
    nothing was captured; all three have to survive, because dropping the bytes
    silently loses the output the error message promises.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _write_fem_logs(workdir, stdout, stderr):
    """Keep the solver's own output next to its other artefacts."""
    paths = []
    for name, text in (("dcc-mcp-ccx.stdout.log", stdout), ("dcc-mcp-ccx.stderr.log", stderr)):
        path = os.path.join(workdir, name)
        try:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(text or "")
        except OSError:
            pass
        paths.append(path)
    return paths


def _frd_has_results(path):
    """True when a CalculiX ``.frd`` carries at least one result dataset.

    A .frd can contain the node coordinates and nothing else: the solver wrote
    the mesh but no displacements or stresses. That file parses cleanly, the
    reader builds an empty result object, and the failure only surfaces later
    as a zero node count that says nothing about the cause.

    The markers, as they appear in FreeCAD's own golden result files:

    - ``2C`` starts the node coordinate block and ``3C`` the element block, so
      neither is a result.
    - A result dataset header starts with ``100C`` (written as ``100CL``).
    - The step key ``1PSTEP`` sits *before* the header it belongs to -- and not
      adjacent to it, the offset differs between analyses -- so it cannot be
      used to validate a header.

    So a result is present when a dataset header exists **and** a component
    record follows it; requiring the component is what rules out a header with
    no values under it. The file is read streaming: a result file is large and
    the answer is usually in the first block.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            header_seen = False
            for line in stream:
                if not line.strip():
                    continue
                token = line.split(None, 1)[0] if line.split(None, 1) else ""
                if token.startswith("100C"):
                    header_seen = True
                    continue
                if not header_seen:
                    continue
                # -4 <name> opens a result component (DISP, STRESS, TOSTRAIN, ...)
                # and -5 opens one of its value records.
                if token in ("-4", "-5"):
                    return True
    except OSError:
        return False
    return False


def _solver_evidence(workdir, solver_run=None):
    """Collect whatever the solver left behind, for the failure details.

    A solve can exit successfully and still produce nothing usable, so the exit
    code alone cannot explain an empty result. This reads the logs back from
    disk as well as taking the captured tails, because the artefacts on disk are
    what a caller can actually go and read afterwards.
    """
    evidence = {"solver_workdir": str(workdir)}
    if solver_run:
        evidence["solver_exit_code"] = solver_run.get("exit_code")
        evidence["solver_duration_secs"] = solver_run.get("duration_secs")
        for key in ("stdout_tail", "stderr_tail"):
            if solver_run.get(key):
                evidence[key] = solver_run[key]
        for key in ("stdout_file", "stderr_file"):
            if solver_run.get(key):
                evidence[key] = solver_run[key]
    for name, key in (
        ("dcc-mcp-ccx.stdout.log", "ccx_stdout_from_disk"),
        ("dcc-mcp-ccx.stderr.log", "ccx_stderr_from_disk"),
    ):
        path = os.path.join(str(workdir), name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                text = stream.read(_FEM_LOG_TAIL)
        except OSError:
            continue
        if text.strip():
            evidence[key] = text
    return evidence


def _result_object(analysis):
    for obj in getattr(analysis, "Group", ()) or ():
        is_derived = getattr(obj, "isDerivedFrom", None)
        if callable(is_derived):
            try:
                if is_derived("Fem::FemResultObject"):
                    return obj
            except Exception:
                pass
        if "Result" in str(getattr(obj, "TypeId", "")):
            return obj
    return None


def _result_nodes(result):
    """Return the node mapping of a result object, or ``None``.

    ``result.Mesh`` is not the mesh. Upstream builds a ``MeshResult`` document
    object and assigns the mesh to its ``FemMesh`` property
    (``importCcxFrdResults.py``), so the object reached through ``Mesh`` has no
    ``Nodes`` attribute of its own -- reading it there yields nothing and the
    node count collapses to zero even when the solve produced a full result.

    Both spellings are accepted so this works whether the mesh object or the raw
    mesh is handed over, and ``NodeNumbers`` is tried last because a result can
    expose it directly.
    """
    mesh_object = getattr(result, "Mesh", None)
    for candidate in (
        getattr(mesh_object, "FemMesh", None),
        mesh_object,
        getattr(result, "FemMesh", None),
    ):
        nodes = getattr(candidate, "Nodes", None)
        if nodes is not None:
            return nodes
    nodes = getattr(result, "NodeNumbers", None)
    return nodes if nodes is not None else None


def _extract_results(result, axis=None):
    """Pull bounded, finite scalars out of a FreeCAD FEM result object.

    ``axis``, when given, is the requested load direction: the extreme signed
    displacement along it is reported alongside the magnitudes, because a
    magnitude alone cannot distinguish a load applied along an axis from the
    same load applied against it.
    """
    nodes = _result_nodes(result)
    node_count = len(nodes) if nodes is not None else int(getattr(result, "NodeCount", 0) or 0)
    von_mises = [float(item) for item in (getattr(result, "vonMises", None) or ())]
    vectors = getattr(result, "DisplacementVectors", None) or ()
    magnitudes = []
    signed = []
    for vector in vectors:
        x = getattr(vector, "x", 0.0)
        y = getattr(vector, "y", 0.0)
        z = getattr(vector, "z", 0.0)
        magnitudes.append(math.sqrt(x * x + y * y + z * z))
        if axis is not None:
            signed.append(x * axis[0] + y * axis[1] + z * axis[2])
    return {
        "node_count": node_count,
        "max_von_mises": max(von_mises) if von_mises else None,
        "min_von_mises": min(von_mises) if von_mises else None,
        "max_displacement": max(magnitudes) if magnitudes else None,
        "min_displacement": min(magnitudes) if magnitudes else None,
        "axis_displacement": (max(signed, key=abs) if signed else None),
    }


def analysis_list_faces(params):
    """Report the faces of one object with the references this host resolves."""
    import FreeCAD as App

    tool = "analysis.list_faces"
    doc = _open_document(App, _required(params, "document_path", tool))
    try:
        object_name = _required(params, "object_name", tool)
        obj = doc.getObject(object_name)
        if obj is None:
            raise ValueError("Object does not exist: %s" % object_name)
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            raise ValueError("Object has no shape: %s" % object_name)
        faces = _face_entries(shape)
        for face in faces:
            face["reference"] = "%s:%s" % (obj.Name, face["name"])
        return {
            "object_name": obj.Name,
            "face_count": len(faces),
            "solids": len(shape.Solids),
            "faces": faces,
        }
    finally:
        _close_document(App, doc)


def system_fem_probe(_params):
    """Report what this host can do, without attempting a solve."""
    import FreeCAD as App

    return _fem_availability(App)


def analysis_run_fem(params):
    """Solve one static structural case and report it with verified units."""
    import FreeCAD as App

    contract = _fem_module()
    tool = "analysis.run_fem"
    version = _host_version()
    workdir = os.path.abspath(str(_required(params, "workdir", tool)))
    if not os.path.isdir(workdir):
        raise ValueError("Solver work directory does not exist: %s" % workdir)
    solver_timeout = float(params.get("solver_timeout_secs") or 600)
    if not math.isfinite(solver_timeout) or solver_timeout <= 0:
        raise ValueError("solver_timeout_secs must be a positive finite number")

    availability = _fem_availability(App)
    if not availability["available"]:
        raise _fem_error(
            contract.FemError.ERROR_HOST_LIMITED,
            "this host cannot run a structural solve",
            version,
            remediation=" ".join(availability["remediation"]),
            details=availability,
            params=params,
        )
    ccx = availability["solver"]["binary"]
    _set_fem_preference(App, "ccxBinaryPath", "Ccx", ccx)
    _set_fem_preference(App, "gmsh_binary_path", "Gmsh", availability["mesher"]["binary"])

    checks = _fem_checks(version, params)
    doc = _open_document(App, _required(params, "document_path", tool))
    try:
        analysis_name = params.get("analysis_name")
        built = None
        mode = "reused"
        if analysis_name:
            analysis = doc.getObject(str(analysis_name))
            if not _is_fem_analysis(analysis):
                raise _fem_error(
                    contract.FemError.ERROR_NO_ANALYSIS,
                    "%s: the document has no FEM analysis named %s" % (tool, analysis_name),
                    version,
                    remediation="Inspect the document for an existing Fem::FemAnalysis, or omit "
                    "analysis_name to have one built.",
                )
        else:
            mode = "created"
            built = _create_fem_analysis(App, doc, params, version)
            analysis = built["analysis"]
        doc.recompute()

        members = list(getattr(analysis, "Group", ()) or ())
        solver = (
            built["solver"]
            if built
            else next(
                (
                    obj
                    for obj in members
                    if "Solver" in str(getattr(obj, "TypeId", ""))
                    or "Ccx" in str(getattr(obj, "TypeId", ""))
                ),
                None,
            )
        )
        if solver is None:
            raise _fem_error(
                contract.FemError.ERROR_NO_ANALYSIS,
                "%s: the analysis contains no solver object" % tool,
                version,
                remediation="Add a CalculiX solver to the analysis, or omit analysis_name.",
            )
        if hasattr(solver, "WorkingDir"):
            solver.WorkingDir = workdir
        if built is not None:
            mesh = built["mesh"]
            mesh_nodes = getattr(getattr(mesh, "FemMesh", None), "NodeCount", 0) or 0
            checks.check(
                mesh_nodes > 0,
                "mesh.node_count",
                ">0",
                mesh_nodes,
                contract.FemError.ERROR_MESHER_MISSING,
                "The mesher produced no nodes, so there is nothing to solve.",
            )

        fea = _fem_tools(analysis, solver, version)
        # FemToolsCcx does not resolve the analysis members in its constructor:
        # `self.mesh` is only bound by update_objects(), and without it
        # check_prerequisites() raises AttributeError. Upstream's own run() calls
        # update_objects -> setup_working_dir -> check_prerequisites in that
        # order; skipping the first step is why the solve died before writing
        # any input.
        updater = getattr(fea, "update_objects", None)
        if callable(updater):
            updater()
        _setup_working_dir(fea, workdir)
        prerequisites = ""
        checker = getattr(fea, "check_prerequisites", None)
        if callable(checker):
            try:
                prerequisites = checker() or ""
            except TypeError:
                # An older signature that takes an argument; there is nothing to
                # pass here, so fall back to letting the solver's own checks run.
                prerequisites = ""
            except AttributeError as exc:
                # The solver driver is missing state it should have resolved. Let
                # it out as a coded failure: swallowing this yields an error with
                # no code, which a caller cannot branch on.
                raise _fem_error(
                    contract.FemError.ERROR_SOLVER_API,
                    "the FEM solver driver is not ready to check its prerequisites: %s" % exc,
                    version,
                    remediation="The analysis members could not be resolved; check that the "
                    "analysis contains a mesh, a material, a restraint and a load.",
                ) from None
        if prerequisites:
            raise _fem_error(
                contract.FemError.ERROR_PREREQUISITES,
                "the FEM solver refused the analysis: %s" % prerequisites,
                version,
                remediation="Complete the analysis in FreeCAD (mesh, material, at least one "
                "restraint and one load), or omit analysis_name to have one built.",
                details={"prerequisites": str(prerequisites)},
            )
        _fem_call(
            fea,
            ("write_inp_file", "setup_ccx"),
            version,
            contract.FemError.ERROR_WRITE_FAILED,
            "writing the solver input",
        )
        input_path = _latest_input_file(workdir)
        if input_path is None:
            raise _fem_error(
                contract.FemError.ERROR_WRITE_FAILED,
                "no CalculiX input file was produced in %s" % workdir,
                version,
                details={"solver_workdir": workdir, "listing": sorted(os.listdir(workdir))[:200]},
            )

        # Unit schema detection: the workbench writes its input in a
        # configurable unit schema, so a result is meaningless until that schema
        # is known. Node coordinates measured against the target's real extent
        # name the length unit, and the length unit names the rest.
        schema = None
        expected_extents = None
        if built is not None:
            box = getattr(built["target"].Shape, "BoundBox", None)
            expected_extents = [box.XLength, box.YLength, box.ZLength] if box is not None else None
            observed = _parse_node_extents(input_path)
            length_unit = (
                contract.match_length_scale(observed, expected_extents)
                if observed is not None and expected_extents is not None
                else None
            )
            if length_unit is None:
                raise _fem_error(
                    contract.FemError.ERROR_UNIT_SCHEMA_UNKNOWN,
                    "the unit schema of %s could not be identified from its node coordinates"
                    % input_path,
                    version,
                    remediation="Set the FreeCAD FEM unit schema preference to a supported "
                    "schema (mm-N-s, SI or imperial) and retry.",
                    details={
                        "input_file": input_path,
                        "observed_node_extents": observed,
                        "expected_extents_mm": expected_extents,
                        "supported_length_units": sorted(contract.SCHEMA_BY_LENGTH_UNIT),
                    },
                    params=params,
                )
            force_unit, stress_unit = contract.unit_schema_for_length(length_unit)
            schema = {
                "length": length_unit,
                "force": force_unit,
                "stress": stress_unit,
                "source": "detected_from_solver_input",
                "observed_node_extents": observed,
                "expected_extents_mm": expected_extents,
            }
            checks.record("unit_schema.detected")
            # Force read-back: the summed *CLOAD block is what proves the
            # magnitude actually reached the solver. A bare-float force property
            # carries no unit, so this is the check, not the property.
            totals = _parse_cload(input_path)
            if totals is None:
                raise _fem_error(
                    contract.FemError.ERROR_UNIT_READBACK,
                    "no *CLOAD block was found in %s, so the applied force cannot be verified"
                    % input_path,
                    version,
                    remediation="Refusing to report results whose load was never proven.",
                    details={"input_file": input_path},
                    params=params,
                )
            requested = built["load"]["force"]["value"]
            # The workbench writes the load with `Force.getValueAs("N")`, and the
            # CalculiX writer's units_information block states Force: N -- the
            # unit schema only governs lengths and masses upstream, not forces.
            # Dividing by the schema's force factor here would compare a newton
            # value against a scaled expectation.
            expected_vector = [
                component * requested for component in built["load"]["unit_direction"]
            ]
            actual_vector = [totals[1], totals[2], totals[3]]
            # The workbench spreads the constraint force across the referenced
            # face's nodes, so the summed components approximate the total rather
            # than equalling it. Upstream itself only flags a deviation beyond 1%
            # (femmesh/meshtools.py), so a tighter tolerance here rejects meshes
            # that are genuinely correct.
            checks.check(
                _contract_module().sequences_match(
                    expected_vector, actual_vector, rel_tolerance=1e-2
                ),
                "load.total_force",
                {
                    "vector": expected_vector,
                    "unit": force_unit,
                    "requested_N": requested,
                },
                {"vector": actual_vector, "unit": force_unit},
                contract.FemError.ERROR_UNIT_READBACK,
                "The force the solver was given does not match the force that was asked for; "
                "this is the failure mode a unit mix-up produces.",
            )
            material_object = built["material_object"]
            applied_modulus = None
            try:
                applied_modulus = App.Units.Quantity(
                    str((material_object.Material or {}).get("YoungsModulus"))
                ).getValueAs("MPa")
            except Exception:
                applied_modulus = None
            checks.check(
                applied_modulus is not None
                and _contract_module().numbers_match(
                    built["material"]["youngs_modulus"]["value"], applied_modulus
                ),
                "material.youngs_modulus",
                built["material"]["youngs_modulus"]["value"],
                applied_modulus,
                contract.FemError.ERROR_INVALID_MATERIAL,
                "The material that reached the analysis is not the material that was requested.",
            )
            stored_force = len(getattr(built["force"], "References", ()) or ())
            checks.check(
                stored_force == len(built["load_reported"]),
                "load.references",
                len(built["load_reported"]),
                stored_force,
                contract.FemError.ERROR_INVALID_REFERENCE,
                "The force constraint does not reference the requested faces.",
            )
            stored_newtons, force_unit_known = _read_force_newtons(App, built["force"])
            checks.check(
                not force_unit_known
                or _contract_module().numbers_match(requested, stored_newtons, rel_tolerance=1e-9),
                "load.property",
                requested,
                stored_newtons,
                contract.FemError.ERROR_UNIT_READBACK,
                "The force stored on the constraint does not match the request.",
            )

        solver_run = _run_ccx(ccx, input_path, workdir, solver_timeout, version)
        solver_evidence = _solver_evidence(workdir, solver_run)
        _fem_call(
            fea,
            ("load_results", "ccx_results", "get_results"),
            version,
            contract.FemError.ERROR_RESULTS_MISSING,
            "loading the solver results",
        )
        result = _result_object(analysis)
        # A result object existing is not the same as it holding results: when
        # the .frd has nodes but no result set, upstream builds an empty result
        # object rather than reporting failure, so the mesh is what proves it.
        if result is not None and getattr(result, "Mesh", None) is None:
            result = None
        checks.check(
            result is not None,
            "results.present",
            "a result object holding a result mesh",
            None,
            contract.FemError.ERROR_RESULTS_MISSING,
            "The solver reported success but no usable result object was created.",
            extra=solver_evidence,
        )
        # Reused analyses have no requested direction, and the signed component
        # is only meaningful against one; fall back to magnitudes there.
        axis = built["load"]["unit_direction"] if built is not None else None
        extracted = _extract_results(result, axis)
        checks.check(
            extracted["node_count"] > 0,
            "results.node_count",
            ">0",
            extracted["node_count"],
            contract.FemError.ERROR_RESULTS_MISSING,
            "An empty result set is not a result.",
            extra=solver_evidence,
        )
        checks.check(
            extracted["max_displacement"] is not None
            and math.isfinite(extracted["max_displacement"])
            and extracted["max_displacement"] >= 0,
            "results.max_displacement",
            "a finite non-negative displacement",
            extracted["max_displacement"],
            contract.FemError.ERROR_RESULTS_MISSING,
            "The result carries no usable displacement field.",
        )
        checks.check(
            extracted["max_von_mises"] is not None
            and math.isfinite(extracted["max_von_mises"])
            and extracted["max_von_mises"] >= 0,
            "results.max_von_mises",
            "a finite non-negative von Mises stress",
            extracted["max_von_mises"],
            contract.FemError.ERROR_RESULTS_MISSING,
            "The result carries no usable stress field.",
        )
        checks.check(
            extracted["max_displacement"] >= extracted["min_displacement"],
            "results.displacement_ordering",
            "max >= min",
            {
                "max": extracted["max_displacement"],
                "min": extracted["min_displacement"],
            },
            contract.FemError.ERROR_RESULTS_MISSING,
            "The displacement extrema contradict each other.",
        )

        payload = {
            "analysis_name": analysis.Name,
            "mode": mode,
            "result_object": result.Name,
            "node_count": extracted["node_count"],
            "solver_workdir": workdir,
            "solver_exit_code": solver_run["exit_code"],
            "solver": {
                "name": "calculix",
                "binary": ccx,
                "version": availability["solver"]["version"],
                "input_file": input_path,
                "duration_secs": solver_run["duration_secs"],
                "stdout_file": solver_run["stdout_file"],
                "stderr_file": solver_run["stderr_file"],
                "stdout_tail": solver_run["stdout_tail"],
                "stderr_tail": solver_run["stderr_tail"],
            },
            "mesher": {
                "name": "gmsh",
                "binary": availability["mesher"]["binary"],
                "version": availability["mesher"]["version"],
            },
            "verified": list(checks.names),
        }
        if schema is not None:
            # quantity_payload converts to the canonical unit itself, so the
            # extracted values are handed to it unconverted. Pre-multiplying by
            # the conversion factor applied it twice, which is invisible for a
            # mm schema (factor 1) and wrong by the factor for every other one.
            payload["unit_schema"] = schema
            payload["max_von_mises"] = contract.quantity_payload(
                extracted["max_von_mises"], schema["stress"], "stress"
            )
            payload["min_von_mises"] = contract.quantity_payload(
                extracted["min_von_mises"], schema["stress"], "stress"
            )
            payload["max_displacement"] = contract.quantity_payload(
                extracted["max_displacement"], schema["length"], "length"
            )
            payload["min_displacement"] = contract.quantity_payload(
                extracted["min_displacement"], schema["length"], "length"
            )
            # Signed component along the requested load axis: the magnitudes above
            # cannot tell a load applied along an axis from one applied against it.
            payload["axis_displacement"] = contract.quantity_payload(
                extracted["axis_displacement"], schema["length"], "length"
            )
        else:
            # A reused analysis owns its own units; report the raw field with the
            # host's schema rather than inventing one.
            payload["unit_schema"] = None
            payload["max_von_mises"] = None
            payload["min_von_mises"] = None
            payload["max_displacement"] = None
            payload["min_displacement"] = None
        if built is not None:
            payload.update(
                {
                    "target_object": built["target"].Name,
                    "material": built["material"],
                    "load": {
                        "force": built["load"]["force"],
                        "direction": built["load"]["direction"],
                        "unit_direction": built["load"]["unit_direction"],
                        "faces": built["load_reported"],
                    },
                    "fixed_faces": built["fixed_reported"],
                    "mesh": {
                        "object": built["mesh"].Name,
                        "element_order": "2nd",
                        "element_order_property": built["order_property"],
                        "size": built["mesh_size"],
                        "size_property": built["size_property"],
                    },
                }
            )
        return payload
    finally:
        _close_document(App, doc)


def _fem_module():
    """Load the typed FEM unit/reference contract that ships next to this driver."""
    return _load_sibling_module(_FEM_CONTRACT_FILENAME, "dcc_mcp_freecad_fem_contract")


_METHODS = {
    "system.status": system_status,
    "document.create": document_create,
    "document.inspect": document_inspect,
    "document.validate": document_validate,
    "document.save_copy": document_save_copy,
    "document.render_view": document_render_view,
    "document.remove_object": document_remove_object,
    "model.add_primitive": model_add_primitive,
    "model.update_primitive": model_update_primitive,
    "model.transform_object": model_transform_object,
    "model.scale_object": model_scale_object,
    "model.copy_object": model_copy_object,
    "model.mirror_object": model_mirror_object,
    "model.boolean_operation": model_boolean_operation,
    "model.import_geometry": model_import_geometry,
    "model.export_geometry": model_export_geometry,
    "model.fillet_edges": model_fillet_edges,
    "model.chamfer_edges": model_chamfer_edges,
    "model.linear_pattern": model_linear_pattern,
    "model.polar_pattern": model_polar_pattern,
    "model.mirror_feature": model_mirror_feature,
    "model.insert_part": model_insert_part,
    "sketch.create": sketch_create,
    "sketch.add_geometry": sketch_add_geometry,
    "sketch.add_constraint": sketch_add_constraint,
    "sketch.info": sketch_info,
    "partdesign.pad": partdesign_pad,
    "partdesign.pocket": partdesign_pocket,
    "partdesign.revolution": partdesign_revolution,
    "partdesign.groove": partdesign_groove,
    "partdesign.loft": partdesign_loft,
    "partdesign.sweep": partdesign_sweep,
    "partdesign.hole": partdesign_hole,
    "system.fem_probe": system_fem_probe,
    "analysis.run_fem": analysis_run_fem,
    "analysis.list_faces": analysis_list_faces,
}


_FEM_CONTRACT_FILENAME = "fem_contract.py"

# The dispatch entry is deliberately the last statement in the module.
# FreeCADCmd runs this file as a script with `--pass`, so `main()` is called
# the moment the guard is reached. Keeping the entry last means anything
# appended later binds before dispatch, rather than depending on what
# happens to sit below it.
if "--pass" in sys.argv:
    main()
