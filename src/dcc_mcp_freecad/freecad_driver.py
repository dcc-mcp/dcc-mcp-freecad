"""Package-owned FreeCADCmd entry point. This module runs inside FreeCAD's Python."""

import json
import math
import os
import sys

_MATRIX_FILENAME = "compat_matrix.json"
_COMPAT_MODULE = None


class IncompatibleHostError(RuntimeError):
    """The running FreeCAD host exposes an API the matrix declares as broken."""


def _matrix_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _MATRIX_FILENAME)


def _compat_module():
    """Load the shared compatibility module from this driver's own directory.

    The driver is executed directly by FreeCADCmd, so the adapter package is not
    importable. Loading ``compat.py`` by path keeps one single compatibility
    source of truth instead of duplicating the matrix inside this file.
    """
    global _COMPAT_MODULE
    if _COMPAT_MODULE is None:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compat.py")
        if not os.path.isfile(path) or not os.path.isfile(_matrix_path()):
            raise RuntimeError(
                "The FreeCAD compatibility matrix is missing next to the packaged driver "
                "(%s); reinstall dcc-mcp-freecad" % path
            )
        spec = importlib.util.spec_from_file_location("dcc_mcp_freecad_compat", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _COMPAT_MODULE = module
    return _COMPAT_MODULE


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
    if doc is not None:
        app.closeDocument(doc.Name)


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

    doc = App.newDocument(params["name"])
    try:
        doc.recompute()
        doc.saveAs(params["document_path"])
        return _document_payload(doc)
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


def document_save_copy(params):
    import FreeCAD as App

    doc = _open_document(App, params["document_path"])
    try:
        doc.recompute()
        doc.saveAs(params["output_path"])
        return {"object_count": len(doc.Objects)}
    finally:
        _close_document(App, doc)


def _dependents_recursive(obj, collected):
    for dependent in obj.InList:
        if dependent.Name not in collected:
            collected[dependent.Name] = dependent
            _dependents_recursive(dependent, collected)


def document_remove_object(params):
    import FreeCAD as App

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
        return {"removed_objects": removed, "document": _document_payload(doc)}
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


def model_add_primitive(params):
    import FreeCAD as App

    primitive = params["primitive"]
    if primitive not in _PRIMITIVE_TYPES:
        raise ValueError("Unsupported primitive: %s" % primitive)
    doc = _open_document(App, params["document_path"])
    try:
        if doc.getObject(params["name"]) is not None:
            raise ValueError("Object already exists: %s" % params["name"])
        obj = doc.addObject(_PRIMITIVE_TYPES[primitive], params["name"])
        if params.get("label"):
            obj.Label = str(params["label"])
        _apply_dimensions(obj, params.get("dimensions") or {}, _host_version())
        translation = _vector(params["translation"], "translation")
        obj.Placement = App.Placement(
            App.Vector(*translation),
            _rotation(App, params["rotation_axis"], params["rotation_degrees"]),
        )
        _save_document(doc)
        if obj.Shape.isNull() or not obj.Shape.isValid():
            raise ValueError("Primitive parameters produced an invalid or empty shape")
        return {"object": _object_payload(obj), "document": _document_payload(doc)}
    finally:
        _close_document(App, doc)


def model_update_primitive(params):
    import FreeCAD as App

    doc = _open_document(App, params["document_path"])
    try:
        obj = doc.getObject(params["object_name"])
        if obj is None:
            raise ValueError("Object does not exist: %s" % params["object_name"])
        _apply_dimensions(obj, params.get("dimensions") or {}, _host_version())
        if params.get("label") is not None:
            obj.Label = str(params["label"])
        _save_document(doc)
        if obj.Shape.isNull() or not obj.Shape.isValid():
            raise ValueError("Primitive parameters produced an invalid or empty shape")
        return {"object": _object_payload(obj)}
    finally:
        _close_document(App, doc)


def model_transform_object(params):
    import FreeCAD as App

    doc = _open_document(App, params["document_path"])
    try:
        obj = doc.getObject(params["object_name"])
        if obj is None or not hasattr(obj, "Placement"):
            raise ValueError("Placeable object does not exist: %s" % params["object_name"])
        translation = _vector(params["translation"], "translation")
        obj.Placement = App.Placement(
            App.Vector(*translation),
            _rotation(App, params["rotation_axis"], params["rotation_degrees"]),
        )
        _save_document(doc)
        return {"object": _object_payload(obj)}
    finally:
        _close_document(App, doc)


def model_boolean_operation(params):
    import FreeCAD as App

    mapping = {
        "union": "Part::Fuse",
        "cut": "Part::Cut",
        "intersection": "Part::Common",
    }
    operation = params["operation"]
    if operation not in mapping:
        raise ValueError("Unsupported boolean operation: %s" % operation)
    doc = _open_document(App, params["document_path"])
    try:
        base = doc.getObject(params["base_object"])
        tool = doc.getObject(params["tool_object"])
        if base is None or tool is None:
            raise ValueError("Both boolean operands must exist")
        if not hasattr(base, "Shape") or not hasattr(tool, "Shape"):
            raise ValueError("Boolean operands must have Part shapes")
        if doc.getObject(params["result_name"]) is not None:
            raise ValueError("Result object already exists: %s" % params["result_name"])
        result = doc.addObject(mapping[operation], params["result_name"])
        result.Base = base
        result.Tool = tool
        if params.get("result_label"):
            result.Label = str(params["result_label"])
        doc.recompute()
        if result.Shape.isNull() or not result.Shape.isValid():
            raise ValueError("Boolean operation produced an invalid or empty shape")
        _save_document(doc)
        return {"object": _object_payload(result), "operation": operation}
    finally:
        _close_document(App, doc)


def model_import_geometry(params):
    import FreeCAD as App

    input_path = params["input_path"]
    suffix = input_path.lower().rsplit(".", 1)[-1]
    doc = _open_document(App, params["document_path"])
    try:
        if doc.getObject(params["object_name"]) is not None:
            raise ValueError("Object already exists: %s" % params["object_name"])
        if suffix in ("stl", "obj"):
            import Mesh

            obj = doc.addObject("Mesh::Feature", params["object_name"])
            obj.Mesh = Mesh.Mesh(input_path)
            if obj.Mesh.CountPoints == 0:
                raise ValueError("Imported mesh is empty")
        else:
            import Part

            shape = Part.read(input_path)
            if shape.isNull():
                raise ValueError("Imported Part shape is empty")
            obj = doc.addObject("Part::Feature", params["object_name"])
            obj.Shape = shape
        if params.get("label"):
            obj.Label = str(params["label"])
        _save_document(doc)
        return {"object": _object_payload(obj), "input_path": input_path}
    finally:
        _close_document(App, doc)


def _assert_tessellation(mesh_obj, source_name, version):
    """Assert tessellation produced a real mesh instead of a silent empty one.

    MeshPart deflection behaviour moved between FreeCAD 1.0 and 1.1. A changed
    deflection is only visible as a degenerate mesh, so the export asserts the
    result and names the host version instead of writing a plausible file.
    """
    mesh = getattr(mesh_obj, "Mesh", None)
    points = int(getattr(mesh, "CountPoints", 0) or 0)
    facets = int(getattr(mesh, "CountFacets", 0) or 0)
    if points <= 0 or facets <= 0:
        entry = _breaking_change_for_symbol("MeshPart.meshFromShape", version) or {}
        raise IncompatibleHostError(
            "FreeCAD %s tessellated %s into an empty mesh (%d points, %d facets) with "
            "linear_deflection=%s and angular_deflection_degrees=%s. Refusing to export a "
            "degenerate mesh. %s"
            % (
                version,
                source_name,
                points,
                facets,
                "unknown",
                "unknown",
                entry.get("remediation")
                or "Increase the deflection or repair the shape, then retry.",
            )
        )
    return mesh


def model_export_geometry(params):
    import FreeCAD as App

    output_path = params["output_path"]
    suffix = output_path.lower().rsplit(".", 1)[-1]
    doc = _open_document(App, params["document_path"])
    temp_meshes = []
    try:
        objects = []
        for name in params["object_names"]:
            obj = doc.getObject(name)
            if obj is None:
                raise ValueError("Object does not exist: %s" % name)
            objects.append(obj)
        if suffix in ("stl", "obj"):
            import Mesh
            import MeshPart

            mesh_objects = []
            for index, obj in enumerate(objects):
                if hasattr(obj, "Mesh") and obj.Mesh.CountPoints:
                    mesh_objects.append(obj)
                elif hasattr(obj, "Shape") and not obj.Shape.isNull():
                    mesh_obj = doc.addObject("Mesh::Feature", "DccMcpExportMesh%d" % index)
                    mesh_obj.Mesh = MeshPart.meshFromShape(
                        Shape=obj.Shape,
                        LinearDeflection=_positive(
                            params["linear_deflection"], "linear_deflection"
                        ),
                        AngularDeflection=math.radians(
                            _positive(
                                params["angular_deflection_degrees"],
                                "angular_deflection_degrees",
                            )
                        ),
                        Relative=False,
                    )
                    _assert_tessellation(mesh_obj, obj.Name, _host_version())
                    temp_meshes.append(mesh_obj)
                    mesh_objects.append(mesh_obj)
                else:
                    raise ValueError("Object cannot be meshed: %s" % obj.Name)
            Mesh.export(mesh_objects, output_path)
        else:
            import Part

            if any(not hasattr(obj, "Shape") or obj.Shape.isNull() for obj in objects):
                raise ValueError("STEP/IGES/BREP exports require Part shape objects")
            Part.export(objects, output_path)
        return {"object_names": [obj.Name for obj in objects], "format": suffix}
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
    with open(result_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)


if "--pass" in sys.argv:
    main()
