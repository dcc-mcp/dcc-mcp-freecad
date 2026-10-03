"""Repository-owned native GUI fixtures; executed by the production Cmd protocol.

These synthetic fixtures inspect state, not rendered pixels. No QApplication is
created here: the native FreeCADGui.showMainWindow implementation owns startup.
"""

import importlib.util
import json
import math
import os
import re
import sys
from importlib import import_module
from pathlib import Path


def _driver():
    path = Path(__file__).resolve().parents[2] / "src/dcc_mcp_freecad/freecad_driver.py"
    spec = importlib.util.spec_from_file_location("native_presentation_driver", str(path))
    module = importlib.util.module_from_spec(spec)
    arguments = sys.argv
    try:
        # The production driver dispatches when --pass is present. Load its
        # functions first, then invoke main exactly once after fault injection.
        sys.argv = [str(path)]
        spec.loader.exec_module(module)
    finally:
        sys.argv = arguments
    return module


def _gui():
    import FreeCAD as App
    import FreeCADGui as Gui

    App.ParamGet("User parameter:BaseApp/Preferences/Document").SetBool("SaveThumbnail", False)
    Gui.showMainWindow()
    assert App.GuiUp, "The real host did not initialize native GUI view providers"
    assert Gui.getMainWindow() is not None
    return App, Gui


def _camera(view):
    result = {}
    for name, count in (
        ("position", 3),
        ("orientation", 4),
        ("nearDistance", 1),
        ("farDistance", 1),
        ("aspectRatio", 1),
        ("focalDistance", 1),
        ("height", 1),
    ):
        match = re.search(r"^\s*" + name + r"\s+([^\n]+)$", view.getCamera(), re.M)
        if match is None and name == "orientation":
            # Coin may omit the default axis-angle field. Prove the actual
            # native quaternion is identity rather than supplying a value.
            quaternion = list(view.getCameraOrientation().Q)
            assert len(quaternion) == 4 and all(math.isfinite(item) for item in quaternion)
            length = math.sqrt(sum(item * item for item in quaternion))
            assert length > 0
            assert all(abs(item / length) <= 1e-6 for item in quaternion[:3])
            assert abs(abs(quaternion[3] / length) - 1) <= 1e-6
            continue
        assert match is not None, "The native camera omitted " + name
        values = [float(item) for item in match.group(1).split()]
        assert len(values) == count and all(math.isfinite(item) for item in values)
        result[name] = values
    return result


def _object(obj):
    placement = getattr(obj, "Placement", None)
    result = {
        "name": obj.Name,
        "label": obj.Label,
        "type_id": obj.TypeId,
        "placement": (
            {
                "translation": [placement.Base.x, placement.Base.y, placement.Base.z],
                "quaternion": list(placement.Rotation.Q),
            }
            if placement is not None
            else None
        ),
        "incoming_links": sorted(item.Name for item in obj.InList),
        "outgoing_links": sorted(item.Name for item in obj.OutList),
        "group_members": sorted(item.Name for item in getattr(obj, "Group", [])),
        "element_members": sorted(item.Name for item in getattr(obj, "ElementList", [])),
        "element_visibility": list(getattr(obj, "VisibilityList", [])),
    }
    shape = getattr(obj, "Shape", None)
    if shape is not None and not shape.isNull():
        box = shape.optimalBoundingBox(False, False)
        result["shape"] = {
            "valid": bool(shape.isValid()),
            "closed": bool(shape.isClosed()),
            "shape_type": shape.ShapeType,
            "solids": len(shape.Solids),
            "shells": len(shape.Shells),
            "faces": len(shape.Faces),
            "edges": len(shape.Edges),
            "vertices": len(shape.Vertexes),
            "volume": float(shape.Volume),
            "area": float(shape.Area),
            "length": float(shape.Length),
            "bounds": [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax],
        }
    return result


def _inspect(params):
    App, Gui = _gui()
    from PySide import QtGui

    assert QtGui.QGuiApplication.instance() is not None
    doc = App.openDocument(params["document_path"])
    assert doc is not None
    try:
        view = Gui.getDocument(doc.Name).activeView()
        assert view is not None
        objects = [_object(obj) for obj in sorted(doc.Objects, key=lambda item: item.Name)]
        visibility = {}
        for obj in doc.Objects:
            assert obj.ViewObject is not None, "The real object has no native view provider"
            visibility[obj.Name] = bool(obj.ViewObject.Visibility)
        aggregate = {
            field: sum(item.get("shape", {}).get(field, 0) for item in objects)
            for field in ("solids", "shells", "faces", "edges", "vertices", "volume", "area")
        }
        return {
            "host": {
                "version": ".".join(list(App.Version())[:3]),
                "python_version": list(sys.version_info[:3]),
                "gui_up": bool(App.GuiUp),
                "qt_platform": QtGui.QGuiApplication.platformName(),
            },
            "objects": objects,
            "aggregate": aggregate,
            "visibility": visibility,
            "visible_objects": sorted(name for name, visible in visibility.items() if visible),
            "camera_type": view.getCameraType(),
            "camera_orientation": list(view.getCameraOrientation().Q),
            "camera": _camera(view),
        }
    finally:
        App.closeDocument(doc.Name)


def _prepare(params):
    App, Gui = _gui()
    doc = App.openDocument(params["document_path"])
    assert doc is not None
    try:
        for obj in doc.Objects:
            assert obj.ViewObject is not None
            obj.ViewObject.Visibility = obj.Name == "PortCut"
        view = Gui.getDocument(doc.Name).activeView()
        view.setAnimationEnabled(False)
        view.setCameraType("Orthographic")
        view.viewFront()
        view.fitAll()
        doc.save()
    finally:
        App.closeDocument(doc.Name)
    return _inspect(params)


def _container(params):
    import Part

    App, Gui = _gui()
    kind = params["type_id"]
    assert kind in ("App::DocumentObjectGroup", "App::Part", "PartDesign::Body", "App::LinkGroup")
    if kind == "PartDesign::Body":
        import_module("PartDesign")
    doc = App.openDocument(params["document_path"])
    assert doc is not None
    try:
        parent = doc.addObject(kind, "Container")
        feature_type = "PartDesign::Feature" if kind == "PartDesign::Body" else "Part::Feature"
        leaf = doc.addObject(feature_type, "GroupedLeaf")
        leaf.Shape = Part.makeBox(2, 3, 4)
        if kind == "App::LinkGroup":
            parent.ElementList = [leaf]
        else:
            parent.addObject(leaf)
        doc.recompute()
        members = parent.ElementList if kind == "App::LinkGroup" else parent.Group
        assert leaf in list(members), "The native container did not take ownership"
        assert parent.ViewObject is not None and leaf.ViewObject is not None
        parent.ViewObject.Visibility = True
        leaf.ViewObject.Visibility = True
        if kind == "App::LinkGroup":
            assert parent.setElementVisible(leaf.Name + ".", False) == 1
            assert parent.isElementVisible(leaf.Name + ".") == 0
            assert list(parent.VisibilityList) == [False]
        view = Gui.getDocument(doc.Name).activeView()
        view.setAnimationEnabled(False)
        view.setCameraType("Orthographic")
        view.viewFront()
        doc.save()
    finally:
        App.closeDocument(doc.Name)
    return _inspect(params)


def _save_with_fault(driver):
    fault = os.environ.get("DCC_MCP_FREECAD_TEST_PRESENTATION_FAULT")
    assert fault in ("visibility", "camera"), "Unknown repository-owned native fault"
    presentation = driver._load_sibling_module("presentation.py", "dcc_mcp_freecad_presentation")
    original = presentation.apply

    def faulty_apply(doc, gui, names, view):
        expected = original(doc, gui, names, view)
        if fault == "visibility":
            doc.getObject(names[0]).ViewObject.Visibility = False
        else:
            gui.getDocument(doc.Name).activeView().viewTop()
        # Native save and reopen must discover the actual discrepancy. The
        # production readback and its exception payload are not patched.
        return expected

    presentation.apply = faulty_apply
    driver.main()


def main():
    request_path, result_path = sys.argv[-2:]
    with open(request_path, encoding="utf-8") as stream:
        request = json.load(stream)
    driver = _driver()
    if request["method"] == "document.save_copy":
        _save_with_fault(driver)
        return
    methods = {
        "fixture.inspect": _inspect,
        "fixture.prepare": _prepare,
        "fixture.container": _container,
    }
    try:
        driver._require_supported_host(driver._host_version())
        operation = methods[request["method"]]
        result = operation(request["params"])
        payload = {"ok": True, "result": result}
    except Exception as exc:
        payload = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    with open(result_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, allow_nan=False)


if "--pass" in sys.argv:
    main()
