"""Package-owned FreeCADCmd entry point. This module runs inside FreeCAD's Python."""

import json
import math
import sys


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
    return {
        "version": ".".join(version[:3]),
        "version_details": version,
        "console_mode": True,
        "python_version": sys.version.split()[0],
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


def _apply_dimensions(obj, dimensions):
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
        setattr(obj, allowed[name], number)


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
        _apply_dimensions(obj, params.get("dimensions") or {})
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
        _apply_dimensions(obj, params.get("dimensions") or {})
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
