"""Package-owned FreeCADCmd entry point. This module runs inside FreeCAD's Python."""

import json
import math
import os
import sys

_MATRIX_FILENAME = "compat_matrix.json"
_CONTRACT_FILENAME = "write_contract.py"
_COMPAT_MODULE = None
_SIBLING_MODULES = {}


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


def document_validate(params):
    import FreeCAD as App

    doc = _open_document(App, params["document_path"])
    try:
        doc.recompute()
        invalid = []
        empty_shapes = []
        for obj in doc.Objects:
            shape = getattr(obj, "Shape", None)
            if shape is not None:
                if shape.isNull():
                    empty_shapes.append(obj.Name)
                elif not shape.isValid():
                    invalid.append(obj.Name)
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
    if params.get("visible_objects") is not None:
        presentation = _load_sibling_module("presentation.py", "dcc_mcp_freecad_presentation")
        presentation.validate_selection(params["visible_objects"], params.get("view", "isometric"))
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
                doc, gui, params["visible_objects"], params.get("view", "isometric")
            )
            read_back.check(
                presentation.requested_matches(
                    params["visible_objects"],
                    params.get("view", "isometric"),
                    expected_presentation,
                ),
                "copy.presentation_request",
                {
                    "visible_objects": sorted(params["visible_objects"]),
                    "camera_type": "Orthographic",
                    "camera_orientation": presentation.VIEW_ROTATIONS[
                        params.get("view", "isometric")
                    ],
                },
                expected_presentation,
                "Native visibility and orientation must match the requested presentation.",
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
            actual_presentation = presentation.inspect(copy, gui)
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
        "object_count": count,
        "object_names": expected,
        "verified": _verified_checks(read_back),
    }


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
}


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
        if name in ("length", "width", "height", "radius") and number <= 0:
            raise ValueError("%s must be positive" % name)
        if name in ("radius1", "radius2") and number < 0:
            raise ValueError("%s must be non-negative" % name)
        if name in ("angle", "angle3") and not 0 < number <= 360:
            raise ValueError("%s must be greater than 0 and no more than 360" % name)
        if name in ("angle1", "angle2"):
            limit = 90 if obj.TypeId == "Part::Sphere" else 180
            if not -limit <= number <= limit:
                raise ValueError("%s must be between -%s and %s" % (name, limit, limit))
        property_name = _resolve_property(obj, allowed[name], version)
        setattr(obj, property_name, number)


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
    is_mesh = suffix in ("stl", "obj")
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


def model_export_geometry(params):
    import FreeCAD as App

    tool = "model.export_geometry"
    version = _host_version()
    output_path = _required(params, "output_path", tool)
    suffix = output_path.lower().rsplit(".", 1)[-1]
    is_mesh = suffix in ("stl", "obj")
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
        return {
            "object_names": [obj.Name for obj in objects],
            "format": suffix,
            "verified": _verified_checks(read_back),
        }
    finally:
        for obj in temp_meshes:
            doc.removeObject(obj.Name)
        _close_document(App, doc)


_METHODS = {
    "system.status": system_status,
    "document.create": document_create,
    "document.inspect": document_inspect,
    "document.validate": document_validate,
    "document.save_copy": document_save_copy,
    "document.remove_object": document_remove_object,
    "model.add_primitive": model_add_primitive,
    "model.update_primitive": model_update_primitive,
    "model.transform_object": model_transform_object,
    "model.boolean_operation": model_boolean_operation,
    "model.import_geometry": model_import_geometry,
    "model.export_geometry": model_export_geometry,
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
        # A read-back mismatch carries expected/actual across the process
        # boundary verbatim, so the caller can act on the numbers instead of
        # re-reading a sentence.
        verification = getattr(exc, "payload", None)
        if isinstance(verification, dict):
            payload["error"]["write_verification"] = verification
    with open(result_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)


if "--pass" in sys.argv:
    main()
