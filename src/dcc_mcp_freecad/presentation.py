"""Bounded native view-provider state, and offscreen raster capture.

The caller opts in to the installed FreeCAD GUI library. The raster helpers
below additionally capture the active view to a PNG; nothing here claims that a
given offscreen host can drive OpenGL, which is why every capture is measured
before it is returned and why the view state is put back afterwards.
"""

import math
import os
import re
import struct
from decimal import ROUND_HALF_UP, Decimal
from importlib import import_module

VIEWS = {"isometric", "front", "top", "right"}
OBJECT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Bounded raster capture. The default and the ceiling are the same value: a
# bigger frame costs the agent context without adding information an agent can
# act on, and an unbounded size is exactly the kind of knob that becomes a
# memory problem on a workstation with a 4K document open.
DEFAULT_RENDER_WIDTH = 1280
DEFAULT_RENDER_HEIGHT = 720
MAX_RENDER_WIDTH = 1280
MAX_RENDER_HEIGHT = 720
MIN_RENDER_WIDTH = 16
MIN_RENDER_HEIGHT = 16
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


def validate_render_size(width, height):
    """Bound a raster capture to a pixel size this adapter will publish."""
    values = []
    for value, name, low, high in (
        (width, "render width", MIN_RENDER_WIDTH, MAX_RENDER_WIDTH),
        (height, "render height", MIN_RENDER_HEIGHT, MAX_RENDER_HEIGHT),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(
                "%s must be an integer between %d and %d, got %r" % (name, low, high, value)
            )
        values.append(value)
    return tuple(values)


def _container_names(doc):
    """Names of container objects and of objects a container claims.

    Local Visibility cannot prove visibility through hidden container parents;
    hiding a Group or LinkGroup may also affect its children. Both halves of
    that profile are named here so an explicit selection and a default render
    use the same rule instead of two rules that can drift apart.
    """
    grouped = set()
    containers = set()
    for obj in doc.Objects:
        for members_property in ("Group", "ElementList"):
            if hasattr(obj, members_property):
                containers.add(obj.Name)
                grouped.update(child.Name for child in getattr(obj, members_property))
    return containers, grouped


def renderable_names(doc):
    """The default render selection: top-level non-container view providers.

    A caller who passes ``visible_objects`` chooses explicitly. A caller who
    omits it is asking "show me the document", and the honest answer to that is
    the objects whose visibility this adapter can actually prove -- containers
    and their members are excluded for the same reason an explicit selection
    rejects them.
    """
    containers, grouped = _container_names(doc)
    return sorted(
        obj.Name
        for obj in doc.Objects
        if obj.ViewObject is not None and obj.Name not in containers and obj.Name not in grouped
    )


def hide_all(doc):
    """Hide every view provider without touching the camera.

    Used to capture the empty-scene reference that proves the selection
    contributed pixels. Deliberately does not re-fit the camera: the reference
    must be the same frame with less in it, not a differently framed scene.
    """
    for obj in doc.Objects:
        if obj.ViewObject is not None:
            obj.ViewObject.Visibility = False


def capture(view, path, width, height):
    """Write the active view to ``path`` as a PNG at an explicit pixel size.

    ``saveImage`` takes positional arguments only. It is registered as
    ``METH_VARARGS`` and parsed with ``PyArg_ParseTuple``, so keyword arguments
    raise ``TypeError`` on every supported host -- there is no keyword spelling
    to fall back to. The signature is
    ``saveImage(filename, width, height, color, comment, samples)`` and the
    defaults (``"Current"``, ``"$MIBA"``, the host's MSAA preference) are what
    this adapter wants: "Current" keeps the document's own background and
    ``"$MIBA"`` records the camera matrix in the PNG as provenance.

    A capture that produced no file is reported, never treated as an image: the
    failure mode this whole path exists to catch is a host that answers "done"
    while writing nothing or nothing usable.
    """
    view.saveImage(str(path), int(width), int(height))
    if not os.path.isfile(path):
        raise RuntimeError("The native view wrote no image to %s" % path)
    if os.path.getsize(path) == 0:
        raise RuntimeError("The native view wrote an empty image to %s" % path)


def view_state(doc, gui):
    """Snapshot everything a render is allowed to borrow and must give back.

    A render has to frame the scene to see it, which means moving the camera,
    changing visibility and fitting the view. None of that may outlive the call:
    a "look at the model" tool that quietly re-frames or re-hides the user's
    scene is worse than one that cannot see anything. The camera is recorded as
    the host's own serialized string so the comparison is byte-exact rather
    than a tolerance away from a silent drift.

    ``selection`` is ``None`` when the host does not expose it, rather than
    being recorded as empty: an unrecorded state must not be restored as
    "nothing was selected".

    The camera is reported in its settled form -- see
    :func:`restore_view_state` -- so that a snapshot taken before the render and
    one taken after describe the same view with the same fields on both
    supported release lines. FreeCAD 1.1 adds ``nearDistance``/``farDistance``
    the first time a camera is written back, so an unsettled "before" snapshot
    and a settled "after" one would differ in serialization while describing the
    identical view.
    """
    active = gui.getDocument(doc.Name).activeView()
    try:
        selection = sorted({obj.Name for obj in gui.Selection.getSelection()})
    except Exception:
        selection = None
    return {
        "camera": settled_camera(active),
        "camera_type": active.getCameraType(),
        "visibility": {
            obj.Name: bool(obj.ViewObject.Visibility)
            for obj in doc.Objects
            if obj.ViewObject is not None
        },
        "selection": selection,
    }


def settled_camera(active):
    """Return the camera string the host will keep reporting for this view.

    FreeCAD 1.1's ``getCamera`` omits ``nearDistance``/``farDistance`` until
    the camera has been written back once, then reports them for good. Feeding
    the host its own string makes that promotion happen here rather than at
    comparison time, so every snapshot is in the same canonical form. This reads
    the view and writes the same camera straight back: it moves nothing.
    """
    camera = active.getCamera()
    try:
        active.setCamera(camera)
    except Exception:
        # A view that cannot be written back cannot be promoted either; report
        # the host's own string rather than failing the snapshot.
        return camera
    settled = active.getCamera()
    return settled if settled.count("\n") >= camera.count("\n") else camera


def restore_view_state(doc, gui, state):
    """Put back exactly what :func:`view_state` recorded."""
    active = gui.getDocument(doc.Name).activeView()
    active.setAnimationEnabled(False)
    # The type is part of the serialized camera, but setting it first keeps the
    # restore from depending on the host re-parsing its own node type.
    if state.get("camera_type") and active.getCameraType() != state["camera_type"]:
        active.setCameraType(state["camera_type"])
    active.setCamera(state["camera"])
    # FreeCAD 1.1 grows the serialized camera on the way back in: its
    # ``getCamera`` omits ``nearDistance``/``farDistance`` for a default camera,
    # but ``setCamera`` computes and stores them, so one set turns
    # ``OrthographicCamera { position 0 0 1 ... }`` into one that also carries
    # ``nearDistance 1 / farDistance 10``. The added values are the ones the
    # host derives for this exact camera, so the view is where it was -- only
    # the serialization changed, which would still fail a byte-exact comparison.
    # Re-applying the string the host reports settles the node: the second call
    # is fed a camera that already lists every field the host writes, so
    # ``getCamera`` stops changing and both sides of the comparison report the
    # same canonical form. On 1.0 the string is already stable and this is a
    # no-op.
    active.setCamera(active.getCamera())
    visibility = state.get("visibility") or {}
    for obj in doc.Objects:
        if obj.ViewObject is not None and obj.Name in visibility:
            obj.ViewObject.Visibility = visibility[obj.Name]
    if state.get("selection") is not None:
        gui.Selection.clearSelection()
        for name in state["selection"]:
            if doc.getObject(name) is not None:
                gui.Selection.addSelection(doc.Name, name)


def states_match(expected, actual):
    """Byte-exact comparison of two :func:`view_state` snapshots.

    Deliberately has no tolerance. Once the camera has been round-tripped
    through ``setCamera`` (see :func:`restore_view_state`) the serialized string
    is stable on both supported release lines, so anything less than equality is
    a real difference in where the user's view now points.
    """
    if set(expected) != set(actual):
        return False
    for key in expected:
        if expected[key] is None or actual[key] is None:
            if expected[key] is not actual[key]:
                return False
        elif expected[key] != actual[key]:
            return False
    return True


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

    # The bridge gives this native process an isolated preference file.
    # No blank thumbnail is embedded when an offscreen host has no GL context.
    App.ParamGet("User parameter:BaseApp/Preferences/Document").SetBool("SaveThumbnail", False)
    # The notification area can self-deadlock under the Qt offscreen platform:
    # showing a notification raises the area, the unsupported raise() is logged
    # to FreeCAD's console, and the console handler re-enters the notification
    # area on the same thread. Disabling it before FreeCADGui is imported keeps
    # an offscreen render call from hanging.
    notifications = App.ParamGet("User parameter:BaseApp/Preferences/NotificationArea")
    notifications.SetBool("NotificationAreaEnabled", False)
    notifications.SetBool("NonIntrusiveNotificationsEnabled", False)

    import FreeCADGui as Gui

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
    containers, grouped = _container_names(doc)
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
