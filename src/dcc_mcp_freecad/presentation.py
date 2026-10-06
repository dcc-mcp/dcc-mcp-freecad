"""Bounded native view-provider state for an explicitly requested copy.

The caller opts in to the installed FreeCAD GUI library. This creates no image
and makes no offscreen OpenGL rendering claim.
"""

import math
import re
import struct
from decimal import ROUND_HALF_UP, Decimal
from importlib import import_module

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


def _unit_number(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
        or not math.isfinite(value)
    ):
        raise ValueError(name + " must be a finite number between 0 and 1")
    return float(value)


def _stored_rgb(value):
    """FreeCAD packs float32 RGB using lround(channel * 255.0F)."""
    value = _unit_number(value, "rgb")
    native = struct.unpack("f", struct.pack("f", value))[0]
    scaled = struct.unpack("f", struct.pack("f", native * 255.0))[0]
    return int(math.floor(scaled + 0.5)) / 255.0


def validate_options(names, view, appearances=None, frame_margin=None):
    """Validate the complete request before a host starts or providers change."""
    selected = validate_selection(names, view)
    if frame_margin is not None:
        _unit_number(frame_margin, "frame_margin")
    if appearances is None:
        return []
    if not isinstance(appearances, list) or not 1 <= len(appearances) <= 1000:
        raise ValueError("appearances must contain 1 to 1000 entries")
    normalized = []
    seen = set()
    for item in appearances:
        if not isinstance(item, dict) or set(item) != {"object_name", "rgb", "opacity"}:
            raise ValueError("Each appearance requires only object_name, rgb and opacity")
        name = item["object_name"]
        if not isinstance(name, str) or name not in selected:
            raise ValueError("Appearance names must belong to visible_objects")
        if name in seen:
            raise ValueError("Appearance object names must be unique")
        seen.add(name)
        rgb = item["rgb"]
        if not isinstance(rgb, list) or len(rgb) != 3:
            raise ValueError("rgb must contain exactly three numbers")
        rgb = [_stored_rgb(value) for value in rgb]
        opacity = _unit_number(item["opacity"], "opacity")
        # Native Transparency is an integer percent. Round half upward, and
        # report/check this applied value rather than the unrepresentable input.
        transparency = int(
            ((Decimal(1) - Decimal(str(opacity))) * 100).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
        normalized.append({"object_name": name, "rgb": rgb, "opacity": 1.0 - transparency / 100.0})
    return sorted(normalized, key=lambda item: item["object_name"])


def _appearance_state(obj):
    provider = obj.ViewObject
    color = list(provider.ShapeColor)[:3]
    if len(color) != 3:
        raise ValueError("Native ShapeColor must contain RGB")
    color = [_unit_number(value, "Native ShapeColor") for value in color]
    transparency = provider.Transparency
    if type(transparency) is not int or not 0 <= transparency <= 100:
        raise ValueError("Native Transparency must be an integer percent")
    # ShapeColor is a compatibility alias for ShapeAppearance on FreeCAD 1.x.
    # Inspect every face material too, so inherited face overrides cannot make
    # the scalar alias look correct while the actual part has another color.
    materials = provider.ShapeAppearance
    if not materials or any(
        len(material.DiffuseColor) < 3
        or not all(
            math.isclose(a, b, rel_tol=0, abs_tol=1e-6)
            for a, b in zip(material.DiffuseColor[:3], color)
        )
        or not math.isclose(material.Transparency, transparency / 100.0, abs_tol=1e-6)
        for material in materials
    ):
        raise ValueError("Native part appearance has inconsistent face materials")
    return {"object_name": obj.Name, "rgb": color, "opacity": 1.0 - transparency / 100.0}


def appearances_match(expected, actual):
    if len(expected) != len(actual):
        return False
    return all(
        a["object_name"] == b["object_name"]
        and a["opacity"] == b["opacity"]
        and len(b["rgb"]) == 3
        and all(math.isclose(x, y, rel_tol=0, abs_tol=1e-6) for x, y in zip(a["rgb"], b["rgb"]))
        for a, b in zip(expected, actual)
    )


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


def apply(doc, gui, names, view, appearances=None, frame_margin=None):
    normalized = validate_options(names, view, appearances, frame_margin)
    selected = set(names)
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
    for item in normalized:
        obj = available[item["object_name"]]
        if not obj.isDerivedFrom("Part::Feature") or obj.Shape.isNull():
            raise ValueError("Appearance requires a non-null native Part feature")
        _appearance_state(obj)
    if frame_margin is not None:
        # getCameraNode converts a native Coin pointer through the SWIG table.
        # FreeCAD 1.0 does not import the named module during that conversion.
        # Load its wrappers before changing any provider or camera state.
        import_module("pivy.coin")
    for item in normalized:
        provider = available[item["object_name"]].ViewObject
        provider.ShapeColor = tuple(item["rgb"])
        provider.Transparency = int(round((1.0 - item["opacity"]) * 100.0))
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
    if frame_margin is not None:
        # Margin is an extra fraction of fitted camera height at EACH side.
        # This is relative orthographic height, not guaranteed pixel padding.
        height_field = active.getCameraNode().height
        fitted_height = float(height_field.getValue())
        if not math.isfinite(fitted_height) or fitted_height <= 0:
            raise ValueError("Native fitted camera height must be finite and positive")
        expected_height = fitted_height * (1.0 + 2.0 * frame_margin)
        height_field.setValue(expected_height)
        if not math.isclose(
            float(height_field.getValue()), expected_height, rel_tol=1e-6, abs_tol=0
        ):
            raise RuntimeError("Native frame margin was not applied")
    actual = inspect(doc, gui, [item["object_name"] for item in normalized])
    if frame_margin is not None and not math.isclose(
        actual["camera"]["height"][0], expected_height, rel_tol=1e-6, abs_tol=0
    ):
        raise RuntimeError("Native serialized camera did not reflect the frame margin")
    if not appearances_match(normalized, actual.get("appearances", [])):
        raise RuntimeError("Native part appearance was not applied")
    return actual


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


def requested_matches(names, view, actual, appearances=None):
    return (
        actual["visible_objects"] == sorted(names)
        and actual["camera_type"] == "Orthographic"
        and rotations_match(VIEW_ROTATIONS[view], actual["camera_orientation"])
        and appearances_match(
            validate_options(names, view, appearances), actual.get("appearances", [])
        )
    )


def inspect(doc, gui, appearance_names=None):
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
    result = {
        "visible_objects": sorted(
            obj.Name
            for obj in doc.Objects
            if obj.ViewObject is not None and obj.ViewObject.Visibility
        ),
        "camera_type": active.getCameraType(),
        "camera_orientation": orientation,
        "camera": camera,
    }
    if appearance_names:
        objects = {obj.Name: obj for obj in doc.Objects}
        result["appearances"] = [
            _appearance_state(objects[name]) for name in sorted(appearance_names)
        ]
    return result


def matches(expected, actual):
    """Coin serializes single-precision camera values with decimal rounding."""
    if expected.keys() != actual.keys():
        return False
    if expected["visible_objects"] != actual["visible_objects"]:
        return False
    if expected["camera_type"] != actual["camera_type"]:
        return False
    if not appearances_match(expected.get("appearances", []), actual.get("appearances", [])):
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
