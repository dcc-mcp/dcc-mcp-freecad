"""Bounded native view-provider state for an explicitly requested copy.

The caller opts in to the installed FreeCAD GUI library. This creates no image
and makes no offscreen OpenGL rendering claim.
"""

import math
import re

VIEWS = {"isometric", "front", "top", "right"}
OBJECT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# FreeCAD 1.0.2/1.1.4 Gui/Camera.cpp presets, ordered (x, y, z, w).
VIEW_ROTATIONS = {
    "top": (0.0, 0.0, 0.0, 1.0),
    "front": (math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)),
    "right": (0.5, 0.5, 0.5, 0.5),
    "isometric": (0.424708, 0.17592, 0.339851, 0.820473),
}


def validate_selection(names, view):
    if not isinstance(names, list) or not 1 <= len(names) <= 1000:
        raise ValueError("visible_objects must contain 1 to 1000 object names")
    if any(not isinstance(name, str) or not OBJECT_NAME.fullmatch(name) for name in names):
        raise ValueError("Each visible object must be a bounded object name")
    if len(set(names)) != len(names):
        raise ValueError("visible_objects must be unique")
    if not isinstance(view, str) or view not in VIEWS:
        raise ValueError("view must be isometric, front, top or right")
    return set(names)


def initialize():
    import FreeCAD as App
    import FreeCADGui as Gui

    # The bridge gives this native process an isolated preference file.
    # No blank thumbnail is embedded when an offscreen host has no GL context.
    App.ParamGet("User parameter:BaseApp/Preferences/Document").SetBool("SaveThumbnail", False)
    Gui.showMainWindow()
    if not App.GuiUp:
        raise RuntimeError("Native FreeCAD GUI view providers are unavailable")
    return Gui


def apply(doc, gui, names, view):
    selected = validate_selection(names, view)
    available = {obj.Name: obj for obj in doc.Objects}
    if not selected <= set(available):
        raise ValueError("A requested visible object does not exist")
    if any(available[name].ViewObject is None for name in selected):
        raise ValueError("A requested object has no native view provider")
    # Local Visibility cannot prove visibility through hidden container parents;
    # hiding a Group or LinkGroup may also affect its children. Reject this profile
    # before changing any provider state, even if the caller selected a parent.
    grouped = set()
    containers = set()
    for obj in doc.Objects:
        for members_property in ("Group", "ElementList"):
            if hasattr(obj, members_property):
                containers.add(obj.Name)
                grouped.update(child.Name for child in getattr(obj, members_property))
    if selected & (grouped | containers):
        raise ValueError("Presentation selection requires top-level non-container objects")
    for obj in doc.Objects:
        if obj.ViewObject is not None:
            obj.ViewObject.Visibility = obj.Name in selected
    active = gui.getDocument(doc.Name).activeView()
    active.setAnimationEnabled(False)
    active.setCameraType("Orthographic")
    getattr(
        active,
        {
            "isometric": "viewIsometric",
            "front": "viewFront",
            "top": "viewTop",
            "right": "viewRight",
        }[view],
    )()
    active.fitAll()
    return inspect(doc, gui)


def _normalized_rotation(values):
    if len(values) != 4:
        raise ValueError("Native camera quaternion must have four components")
    values = tuple(float(value) for value in values)
    length = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(length) or length <= 0:
        raise ValueError("Native camera quaternion must be finite and nonzero")
    return tuple(value / length for value in values)


def rotations_match(first, second):
    first, second = _normalized_rotation(first), _normalized_rotation(second)
    return any(all(abs(a - sign * b) <= 1e-6 for a, b in zip(first, second)) for sign in (-1, 1))


def requested_matches(names, view, actual):
    return (
        actual["visible_objects"] == sorted(names)
        and actual["camera_type"] == "Orthographic"
        and rotations_match(VIEW_ROTATIONS[view], actual["camera_orientation"])
    )


def inspect(doc, gui):
    active = gui.getDocument(doc.Name).activeView()
    camera = {}
    orientation = list(active.getCameraOrientation().Q)
    _normalized_rotation(orientation)
    for field in (
        "position",
        "orientation",
        "nearDistance",
        "farDistance",
        "aspectRatio",
        "focalDistance",
        "height",
    ):
        match = re.search(r"^\s*" + field + r"\s+([^\n]+)$", active.getCamera(), re.M)
        if match is None:
            if field == "orientation":
                # Coin may omit its default axis-angle identity field; the
                # independently read native quaternion remains mandatory.
                continue
            raise RuntimeError("Native orthographic camera omitted " + field)
        values = [float(value) for value in match.group(1).split()]
        if len(values) != {"position": 3, "orientation": 4}.get(field, 1):
            raise RuntimeError("Native camera field has an unexpected dimension")
        if not all(math.isfinite(value) for value in values):
            raise RuntimeError("Native camera has a non-finite value")
        camera[field] = values
    return {
        "visible_objects": sorted(
            obj.Name
            for obj in doc.Objects
            if obj.ViewObject is not None and obj.ViewObject.Visibility
        ),
        "camera_type": active.getCameraType(),
        "camera_orientation": orientation,
        "camera": camera,
    }


def matches(expected, actual):
    """Coin serializes single-precision camera values with decimal rounding."""
    if expected.keys() != actual.keys():
        return False
    if expected["visible_objects"] != actual["visible_objects"]:
        return False
    if expected["camera_type"] != actual["camera_type"]:
        return False
    if not rotations_match(expected["camera_orientation"], actual["camera_orientation"]):
        return False
    first, second = expected["camera"], actual["camera"]
    fields = set(first) - {"orientation"}
    if fields != set(second) - {"orientation"}:
        return False
    return all(
        len(values) == len(second[key])
        and all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in zip(values, second[key]))
        for key in fields
        for values in (first[key],)
    )


def geometry_matches(expected, actual):
    """Compare topology counts and recorded aggregate geometry at double precision."""
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, float):
        return math.isclose(expected, actual, rel_tol=1e-10, abs_tol=1e-9)
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            geometry_matches(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            geometry_matches(a, b) for a, b in zip(expected, actual)
        )
    return expected == actual
