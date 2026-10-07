"""Package-owned FreeCADCmd entry point. This module runs inside FreeCAD's Python."""

import json
import math
import os
import sys

_MATRIX_FILENAME = "compat_matrix.json"
_CONTRACT_FILENAME = "write_contract.py"
_COMPAT_MODULE = None
_SIBLING_MODULES = {}

# Exchange formats the Mesh module owns. 3MF joined the set because it is the
# mainstream 3D-printing container and the host's MeshCore writer has shipped a
# Reader3MF/Writer3MF pair on both supported release lines; it tessellates from
# the same mesh as STL/OBJ and is therefore guarded by the same assertions.
_MESH_SUFFIXES = ("stl", "obj", "3mf")

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
    """Rebase copied geometry out of the source's placement, in place.

    ``obj.Shape`` and ``obj.Mesh`` are reported in document coordinates, so a
    copy taken straight from the source already carries the source's placement
    baked in. Assigning ``Placement`` on top of it then composes the two, which
    makes ``translation`` mean "offset from the source" for a copied solid but
    "absolute position" for a primitive re-created from its dimensions. The copy
    is moved back by the inverse placement so that every source type then takes
    the same absolute ``Placement``.

    A mesh is transformed by ``transformGeometry``'s mesh equivalent rather than
    by a Part shape call, because ``Mesh.Mesh`` has no ``transformGeometry``.
    """
    placement = getattr(source, "Placement", None)
    if placement is None:
        return geometry
    matrix = placement.inverse().toMatrix()
    if hasattr(geometry, "transformGeometry"):
        return geometry.transformGeometry(matrix)
    geometry.transform(matrix)
    return geometry


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
            if not -_HELIX_ANGLE_LIMIT <= number <= _HELIX_ANGLE_LIMIT:
                raise ValueError(
                    "%s must be between -%s and %s" % (name, _HELIX_ANGLE_LIMIT, _HELIX_ANGLE_LIMIT)
                )
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

    ``translation`` and ``rotation_degrees`` are absolute for every source type,
    not relative to the source: a copy made without them lands at the document
    origin, the same as a primitive copy does. The shape and mesh branches copy
    geometry FreeCAD reports in document coordinates, so it is moved back into
    local coordinates first; only then does the requested ``Placement`` mean the
    same thing it means for a primitive re-created from its own dimensions.
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
            copied = mesh.copy()
            _move_to_local(copied, source)
            result.Mesh = copied
        else:
            result = doc.addObject("Part::Feature", new_name)
            copied = _shape_or_error(source, object_name, tool).copy()
            _move_to_local(copied, source)
            result.Shape = copied
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
        if is_mesh:
            import Mesh

            # Read and reject the source before creating an object for it, so a
            # failed import leaves no half-initialised object behind.
            mesh = Mesh.Mesh(input_path)
            if not getattr(mesh, "CountPoints", 0):
                raise ValueError("Imported mesh is empty")
            obj = doc.addObject("Mesh::Feature", object_name)
            obj.Mesh = mesh
        else:
            import Part

            shape = Part.read(input_path)
            if shape.isNull():
                raise ValueError("Imported Part shape is empty")
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
            _verify_box(read_back, "mesh", getattr(mesh, "BoundBox", None))
        else:
            shape = read_back.shape("object", stored)
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
}


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
        # re-reading a sentence.
        verification = getattr(exc, "payload", None)
        if isinstance(verification, dict):
            payload["error"]["write_verification"] = verification
        # A sketch that must not be consumed carries the measured state that
        # explains the refusal, so the caller can act on the numbers instead
        # of re-reading a sentence that differs between host versions.
        state = getattr(exc, "state_payload", None)
        if isinstance(state, dict):
            payload["error"]["sketch_state"] = state
    with open(result_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)


if "--pass" in sys.argv:
    main()
