"""Bounded TechDraw drawing pages and their PDF/SVG export.

This module runs inside FreeCAD's own interpreter, where the adapter package is
not importable, so ``freecad_driver.py`` loads it by path the way it loads
``presentation.py``. It therefore imports nothing FreeCAD-related at module
scope: the helpers that need no host are unit-testable from the wrapper side,
and the host entry points import FreeCAD inside the function.

Why creating a page and exporting it are two calls
--------------------------------------------------

Creating a page needs only ``TechDraw``, which is an App module and works in a
console process. Rendering a page to PDF or SVG needs the page's view provider
and ``PagePrinter``, both of which live in ``TechDrawGui``. The GUI is started
only inside the export call, in the same isolated process the rest of the
adapter already uses, and it never becomes a persistent session: every call gets
a fresh process with ``QT_QPA_PLATFORM=offscreen``, a throwaway user config and
the configured deadline. That is the same process-level isolation the modelling
tools rely on; no GUI state outlives the call that needed it.

Why the built-in templates ship with the adapter
------------------------------------------------

FreeCAD's own template directory is not part of the host API this adapter can
pin, and a page with no template has no page size, so an auto-fitted scale
would have nothing to fit into. The two built-ins below are shipped next to the
driver and read from there, which makes the page size identical on every
supported host.
"""

import math
import os
import re
import zlib

AVAILABLE = "available"
HOST_LIMITED = "host_limited"

# Built-in page templates, shipped next to this module under ``templates/``.
BUILTIN_TEMPLATES = {
    "A4_Landscape": "a4_landscape.svg",
    "A4_Portrait": "a4_portrait.svg",
}
TEMPLATE_DIRECTORY = "templates"

# Projection direction and X (right-hand) direction per named view. The X
# direction must never be parallel to the projection direction: for a view down
# +X the default (1, 0, 0) would leave the view frame degenerate.
VIEWS = {
    "isometric": ((1.0, 1.0, 1.0), (1.0, -1.0, 0.0)),
    "front": ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),
    "top": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
}
DEFAULT_VIEWS = ("isometric", "front", "top", "right")
MAX_VIEWS = 8

# Standard drawing scales, descending. Auto-fit picks the largest one the
# geometry still fits into, so a fitted page reads like a drawing and not like
# an arbitrary zoom factor.
SCALE_SERIES = (100.0, 50.0, 20.0, 10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)

PAGE_MARGIN_MM = 10.0
# Fraction of a layout cell the projected view is allowed to fill.
CELL_FILL = 0.9

EXPORT_SUFFIXES = (".pdf", ".svg")

_UNIT_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
    "px": 25.4 / 96.0,
    "": 25.4 / 96.0,
}
_LENGTH = re.compile(r"([-+]?(?:\d+\.?\d*|\.\d+))\s*(mm|cm|in|pt|pc|px)?\s*$")
_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)


def _attribute_pattern(name):
    """Match one quoted attribute inside a tag."""
    return re.compile(r"\b%s\s*=\s*[\"']([^\"']*)[\"']" % re.escape(name))


def _first_attribute(tag, name):
    match = _attribute_pattern(name).search(tag)
    return match.group(1) if match else None


def template_path(name, base=None):
    """Absolute path of a built-in template shipped next to this module."""
    if name not in BUILTIN_TEMPLATES:
        raise ValueError(
            "Unknown built-in drawing template %r; built-ins: %s"
            % (name, ", ".join(sorted(BUILTIN_TEMPLATES)))
        )
    directory = base or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(directory, TEMPLATE_DIRECTORY, BUILTIN_TEMPLATES[name])


def builtin_templates(base=None):
    """The built-in template names whose files are actually present."""
    return sorted(name for name in BUILTIN_TEMPLATES if os.path.isfile(template_path(name, base)))


def _length_mm(value):
    """Parse an SVG length into millimetres, or None when it is not a length.

    An unrecognised unit is refused rather than assumed: a page size guessed
    from a value in the wrong unit produces a fitted scale that is wrong by a
    constant factor, which is exactly the kind of silently-wrong drawing this
    module exists to avoid.
    """
    if value is None:
        return None
    match = _LENGTH.match(str(value).strip())
    if match is None:
        return None
    unit = match.group(2) or ""
    if unit not in _UNIT_TO_MM:
        return None
    number = float(match.group(1))
    if not math.isfinite(number) or number <= 0:
        return None
    return number * _UNIT_TO_MM[unit]


def page_size(path):
    """Read a template's page size in millimetres from its root ``<svg>`` tag.

    Raises rather than falling back: a template with no usable size would give
    every view a scale fitted to a page that does not exist.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            head = stream.read(1 << 16)
    except OSError as error:
        raise ValueError("Drawing template is not readable: %s (%s)" % (path, error)) from None
    match = _SVG_OPEN.search(head)
    if match is None:
        raise ValueError("Drawing template has no <svg> root element: %s" % path)
    tag = match.group(0)
    width = _length_mm(_first_attribute(tag, "width"))
    height = _length_mm(_first_attribute(tag, "height"))
    if width is None or height is None:
        raise ValueError(
            "Drawing template declares no usable page size (width/height in mm expected): %s" % path
        )
    return width, height


def resolve_template(value, base=None):
    """Resolve a caller template request to ``(path, builtin_name, size_mm)``.

    A request is either a built-in name or a path to an ``.svg`` file. Anything
    else is an error: an unrecognised template must not silently become the
    default, because the page size - and therefore every fitted scale - depends
    on it.
    """
    if value is None:
        value = "A4_Landscape"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("template must be a built-in name or a path to an .svg file")
    value = value.strip()
    if value.lower().endswith(".svg"):
        if not os.path.isfile(value):
            raise ValueError("Drawing template does not exist: %s" % value)
        path = os.path.abspath(value)
        return path, None, page_size(path)
    if value not in BUILTIN_TEMPLATES:
        raise ValueError(
            "Unknown drawing template %r; use a built-in name (%s) or a path to an .svg file"
            % (value, ", ".join(sorted(BUILTIN_TEMPLATES)))
        )
    path = template_path(value, base)
    if not os.path.isfile(path):
        raise ValueError(
            "Built-in drawing template %r is missing next to the packaged driver (%s); "
            "reinstall dcc-mcp-freecad" % (value, path)
        )
    return path, value, page_size(path)


def validate_views(views):
    if views is None:
        return list(DEFAULT_VIEWS)
    if not isinstance(views, (list, tuple)) or not 1 <= len(views) <= MAX_VIEWS:
        raise ValueError("views must contain between 1 and %d view names" % MAX_VIEWS)
    for name in views:
        if name not in VIEWS:
            raise ValueError("Unsupported drawing view: %s" % name)
    if len(set(views)) != len(views):
        raise ValueError("views must be unique")
    return list(views)


def validate_scale(value):
    """``None`` means auto-fit; an explicit scale must be a finite positive number.

    An explicit 0 is refused instead of being replaced by the default, which is
    the swallowed-parameter behaviour the read-back contract forbids.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("scale must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("scale must be a finite positive number")
    return number


def _unit(vector):
    length = math.sqrt(sum(component * component for component in vector))
    if not math.isfinite(length) or length <= 0:
        raise ValueError("Drawing view direction may not be the zero vector")
    return tuple(component / length for component in vector)


def _cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _dot(first, second):
    return sum(a * b for a, b in zip(first, second))


def view_frame(direction, x_direction):
    """Orthonormal 2D frame ``(u, v)`` for a projection direction."""
    forward = _unit(direction)
    right = _unit(x_direction)
    if abs(_dot(forward, right)) > 1.0 - 1e-9:
        raise ValueError("Drawing view X direction may not be parallel to the view direction")
    # Remove the component along the view direction so u is truly in-plane.
    right = _unit(
        tuple(component - _dot(right, forward) * axis for component, axis in zip(right, forward))
    )
    return right, _unit(_cross(forward, right))


def projected_extents(box, direction, x_direction):
    """Width and height in millimetres of a bounding box seen from ``direction``.

    Measured on the box corners, so the result is a conservative over-estimate
    for a rotated solid. That is the safe direction to be wrong in: a view that
    is slightly smaller than its cell still fits, while one that is slightly
    larger would spill onto its neighbour.
    """
    u, v = view_frame(direction, x_direction)
    corners = [
        (x, y, z)
        for x in (box.XMin, box.XMax)
        for y in (box.YMin, box.YMax)
        for z in (box.ZMin, box.ZMax)
    ]
    xs = [_dot(corner, u) for corner in corners]
    ys = [_dot(corner, v) for corner in corners]
    return max(xs) - min(xs), max(ys) - min(ys)


def grid_layout(count, page_width, page_height):
    """Centre positions in page millimetres (origin bottom-left) for ``count`` views."""
    columns = int(math.ceil(math.sqrt(count)))
    rows = int(math.ceil(float(count) / columns))
    usable_width = page_width - 2 * PAGE_MARGIN_MM
    usable_height = page_height - 2 * PAGE_MARGIN_MM
    if usable_width <= 0 or usable_height <= 0:
        raise ValueError(
            "Drawing template page (%s x %s mm) leaves no room inside a %s mm margin"
            % (page_width, page_height, PAGE_MARGIN_MM)
        )
    cell = (usable_width / columns, usable_height / rows)
    centers = []
    for index in range(count):
        column = index % columns
        row = index // columns
        centers.append(
            (
                PAGE_MARGIN_MM + (column + 0.5) * cell[0],
                page_height - PAGE_MARGIN_MM - (row + 0.5) * cell[1],
            )
        )
    return centers, cell


def fit_scale(extents, cell):
    """Largest standard scale at which every projected view fits its cell."""
    if not extents:
        raise ValueError("At least one drawing view is required")
    ratios = []
    for width, height in extents:
        if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
            raise ValueError("Drawing sources have no projectable geometry")
        ratios.append(min(cell[0] * CELL_FILL / width, cell[1] * CELL_FILL / height))
    fit = min(ratios)
    for value in SCALE_SERIES:
        if value <= fit:
            return value
    return SCALE_SERIES[-1]


def source_box(objects):
    """Union of the sources' shape bounding boxes, or None if none has one."""
    union = None
    for obj in objects:
        box = None
        shape = getattr(obj, "Shape", None)
        if shape is not None and hasattr(shape, "isNull") and not shape.isNull():
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


# ---------------------------------------------------------------------------
# PDF and SVG artefact inspection
#
# "The export ran without raising" is not evidence. A PDF that parses to the
# expected page count is: a zero-byte file, a truncated file and a file with the
# wrong number of pages all fail here instead of reaching the caller.
# ---------------------------------------------------------------------------

_PAGE_TREE = re.compile(rb"/Type\s*/Pages\b")
_PAGE_NODE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_COUNT = re.compile(rb"/Count\s+(\d+)")
_STREAM = re.compile(rb"stream\r?\n")
_WINDOW = 1024


def _page_count_in(data):
    """Page count declared inside one uncompressed PDF byte string."""
    counts = [
        int(match.group(1))
        for match in _PAGE_TREE.finditer(data)
        for match in [_COUNT.search(data[max(0, match.start() - _WINDOW) : match.end() + _WINDOW])]
        if match is not None
    ]
    if counts:
        return max(counts)
    pages = len(_PAGE_NODE.findall(data))
    return pages or None


def _partial_inflate(chunk):
    return zlib.decompressobj().decompress(chunk)


def _inflate(chunk):
    """Decompress one PDF stream body, or None when it is not a flate stream.

    A PDF stream is bracketed by end-of-line markers the stream body does not
    belong to, so the body is tried both exactly as bracketed and with those
    markers trimmed. A body that will not inflate at all is skipped: the point
    is to read the streams that do, not to fail on the ones that do not.
    """
    for candidate in (chunk, chunk.strip(b"\r\n")):
        for decompress in (zlib.decompress, _partial_inflate):
            try:
                return decompress(candidate)
            except zlib.error:
                continue
    return None


def _flate_chunks(data):
    """Best-effort decompressed content of every PDF stream."""
    for match in _STREAM.finditer(data):
        end = data.find(b"endstream", match.end())
        if end < 0:
            continue
        chunk = _inflate(data[match.end() : end])
        if chunk:
            yield chunk


def pdf_page_count(path):
    """Number of pages a PDF declares, or None when it cannot be determined."""
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return None
    count = _page_count_in(data)
    if count is not None:
        return count
    for chunk in _flate_chunks(data):
        count = _page_count_in(chunk)
        if count is not None:
            return count
    return None


def svg_root_tag(path):
    """Root element tag of an SVG file, or None when it is not parseable XML."""
    import xml.etree.ElementTree as ElementTree

    try:
        return ElementTree.parse(path).getroot().tag
    except (ElementTree.ParseError, OSError):
        return None


SVG_ROOT_TAG = "{http://www.w3.org/2000/svg}svg"


def _components(value):
    """A FreeCAD vector as three floats, tolerating a plain sequence."""
    try:
        return [float(value.x), float(value.y), float(value.z)]
    except AttributeError:
        return [float(component) for component in value]


def _unique_name(doc, prefix):
    """First ``<prefix><n>`` name the document does not already use."""
    for index in range(1, 1000):
        candidate = "%s%d" % (prefix, index)
        if doc.getObject(candidate) is None:
            return candidate
    raise ValueError("No free drawing page name after 999 attempts")


def _page_size_of(page):
    """The page size a DrawPage takes from its template, or None if unavailable.

    The size is reported as evidence, never as a gate: a host that stops
    exposing the template's read-only size must not turn a rendered page into a
    failed export, since the artefact itself is what the read-back asserts.
    """
    template = getattr(page, "Template", None)
    if template is None:
        return None
    try:
        width = float(template.Width)
        height = float(template.Height)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
        return None
    return [width, height]


def create_page(doc, params, read_back, app):
    """Create a TechDraw page with one bounded view per requested direction.

    Uses the App-side ``TechDraw`` module only, so this runs in a plain console
    process. Returns the page description; every assertion that proves the page
    is really there is made by the caller through ``read_back``.
    """
    import TechDraw  # noqa: F401  (import is the capability gate)

    page_name = params.get("page_name") or _unique_name(doc, "Page")
    if doc.getObject(page_name) is not None:
        raise ValueError("Object already exists: %s" % page_name)
    views = validate_views(params.get("views"))
    template_file, builtin, (width, height) = resolve_template(params.get("template"))

    sources = []
    for name in params.get("object_names") or ():
        obj = doc.getObject(name)
        if obj is None:
            raise ValueError("Object does not exist: %s" % name)
        shape = getattr(obj, "Shape", None)
        if shape is None or not hasattr(shape, "isNull") or shape.isNull():
            raise ValueError(
                "Drawing sources require a non-null Part shape; %s has no projectable geometry"
                % name
            )
        sources.append(obj)
    box = source_box(sources)
    if box is None:
        raise ValueError("Drawing sources have no projectable geometry")

    centers, cell = grid_layout(len(views), width, height)
    extents = []
    for name in views:
        direction, x_direction = VIEWS[name]
        size = projected_extents(box, direction, x_direction)
        if not all(math.isfinite(value) and value > 0 for value in size):
            raise ValueError("Drawing sources have no projectable geometry for view %s" % name)
        extents.append(size)
    requested_scale = validate_scale(params.get("scale"))
    scale = requested_scale if requested_scale is not None else fit_scale(extents, cell)

    page = doc.addObject("TechDraw::DrawPage", page_name)
    template_name = "%s_Template" % page_name
    template = doc.addObject("TechDraw::DrawSVGTemplate", template_name)
    template.Template = template_file
    page.Template = template
    created = []
    for index, name in enumerate(views):
        direction, x_direction = VIEWS[name]
        view_name = "%s_%s" % (page_name, name.capitalize())
        view = doc.addObject("TechDraw::DrawViewPart", view_name)
        view.Source = list(sources)
        view.Direction = app.Vector(*direction)
        view.XDirection = app.Vector(*x_direction)
        view.ScaleType = "Custom"
        view.Scale = scale
        page.addView(view)
        # Position and scale are applied again *after* the view joins the page.
        # DrawPage::addView re-centres every new view that has no owner, and
        # drops ScaleType back to "Automatic" when the view does not fit the
        # page as first added. Neither is a rejection of the request -- both are
        # the host imposing its own layout -- so they are applied over rather
        # than refused. The read-back below is what proves they then stuck.
        view.LockPosition = True
        view.ScaleType = "Custom"
        view.Scale = scale
        view.X = centers[index][0]
        view.Y = centers[index][1]
        created.append((view_name, name, direction, x_direction, centers[index]))
    # Saved before any read-back: a page that only exists in memory is not a
    # page the caller can export from a later process.
    doc.recompute()
    doc.save()

    # Read-back: the page, its template and every view must exist with what was
    # asked for, and each view must actually have projected something.
    stored_page = doc.getObject(page_name)
    read_back.exists("page", page_name, stored_page)
    read_back.check(
        stored_page.TypeId == "TechDraw::DrawPage",
        "page.type_id",
        "TechDraw::DrawPage",
        stored_page.TypeId,
        "The page was created as a different type than requested.",
    )
    read_back.check(
        getattr(stored_page, "Template", None) is not None
        and stored_page.Template.Name == template_name,
        "page.template",
        template_name,
        getattr(getattr(stored_page, "Template", None), "Name", None),
        "The page is not wired to its template, so it has no page size.",
    )
    page_views = [view.Name for view in (getattr(stored_page, "Views", None) or ())]
    read_back.check(
        page_views == [item[0] for item in created],
        "page.views",
        [item[0] for item in created],
        page_views,
        "The page does not list the views that were added to it.",
    )
    for view_name, name, direction, _x_direction, center in created:
        stored = doc.getObject(view_name)
        read_back.exists("view", view_name, stored)
        read_back.check(
            stored.TypeId == "TechDraw::DrawViewPart",
            "view.%s.type_id" % name,
            "TechDraw::DrawViewPart",
            stored.TypeId,
            "The view was created as a different type than requested.",
        )
        read_back.check(
            sorted(item.Name for item in (getattr(stored, "Source", None) or ()))
            == sorted(obj.Name for obj in sources),
            "view.%s.source" % name,
            sorted(obj.Name for obj in sources),
            sorted(item.Name for item in (getattr(stored, "Source", None) or ())),
            "The view is not wired to the requested sources.",
        )
        read_back.sequences(
            "view.%s.direction" % name,
            list(direction),
            _components(stored.Direction),
            "The view was saved with a different projection direction.",
        )
        read_back.numbers(
            "view.%s.scale" % name,
            scale,
            getattr(stored, "Scale", None),
            "The view was saved with a different scale than the one that was requested.",
        )
        read_back.sequences(
            "view.%s.position" % name,
            list(center),
            [getattr(stored, "X", None), getattr(stored, "Y", None)],
            "The view was saved at a different page position.",
        )
        projected = TechDraw.viewPartAsSvg(stored)
        read_back.check(
            bool(projected) and "<" in str(projected),
            "view.%s.projected" % name,
            "a non-empty SVG projection",
            "" if not projected else "%d characters" % len(str(projected)),
            "The view projected no geometry, so the page would render empty.",
        )
    return {
        "page_name": page_name,
        "object_names": sorted(obj.Name for obj in sources),
        "template": {
            "builtin": builtin,
            "file": template_file,
            "width_mm": width,
            "height_mm": height,
        },
        "scale": scale,
        "scale_auto": requested_scale is None,
        "views": [
            {
                "name": view_name,
                "view": name,
                "direction": list(direction),
                "x_direction": list(x_direction),
                "scale": scale,
                "x_mm": center[0],
                "y_mm": center[1],
            }
            for view_name, name, direction, x_direction, center in created
        ],
    }


def initialize():
    """Start the offscreen FreeCAD GUI and return the ``FreeCADGui`` module.

    Called only inside an export, in the isolated process the bridge already
    owns. The main window is needed because ``PagePrinter`` renders a page
    through its view provider; the process is discarded when the call returns,
    so no GUI session outlives it.
    """
    import FreeCAD as App
    import FreeCADGui as Gui

    if App.GuiUp:
        return Gui
    App.ParamGet("User parameter:BaseApp/Preferences/Document").SetBool("SaveThumbnail", False)
    Gui.showMainWindow()
    if not App.GuiUp:
        raise RuntimeError(
            "Native FreeCAD GUI view providers are unavailable, so a drawing page cannot be "
            "rendered; install the FreeCAD GUI library or use a full FreeCAD installation"
        )
    return Gui


def export_page(doc, params, read_back, app=None):
    """Render one TechDraw page to PDF or SVG and prove the artefact is real."""
    page_name = params.get("page_name")
    output_path = params.get("output_path")
    if not page_name:
        raise ValueError("drawing.export requires parameter 'page_name'")
    if not output_path:
        raise ValueError("drawing.export requires parameter 'output_path'")
    suffix = os.path.splitext(str(output_path).lower())[1]
    if suffix not in EXPORT_SUFFIXES:
        raise ValueError("Unsupported drawing export extension: %s" % suffix)

    page = doc.getObject(page_name)
    if page is None:
        raise ValueError("Drawing page does not exist: %s" % page_name)
    if page.TypeId != "TechDraw::DrawPage":
        raise ValueError("Object is not a TechDraw page: %s" % page_name)
    if not (getattr(page, "Views", None) or ()):
        raise ValueError("Drawing page has no views, so it would render empty: %s" % page_name)

    gui = initialize()
    try:
        gui.getDocument(doc.Name)
    except Exception as error:
        raise RuntimeError(
            "FreeCAD attached no GUI document to %s, so the page has no view provider to render "
            "(%s)" % (doc.Name, error)
        ) from None
    doc.recompute()

    import TechDrawGui

    if suffix == ".pdf":
        TechDrawGui.exportPageAsPdf(page, output_path)
    else:
        TechDrawGui.exportPageAsSvg(page, output_path)

    # Read-back: an export only counts once the artefact exists, is not empty,
    # and parses back into the page it came from.
    read_back.check(
        os.path.isfile(output_path) and os.path.getsize(output_path) > 0,
        "artifact.non_empty",
        "a non-empty %s" % suffix,
        ("missing: %s" % output_path)
        if not os.path.isfile(output_path)
        else "%d bytes" % os.path.getsize(output_path),
        "FreeCAD reported a successful export but wrote no drawing.",
    )
    if suffix == ".pdf":
        pages = pdf_page_count(output_path)
        read_back.check(
            pages == 1,
            "artifact.page_count",
            1,
            pages,
            "The exported PDF does not parse to one page, so it is not the page that was selected.",
        )
    else:
        root = svg_root_tag(output_path)
        read_back.check(
            root == SVG_ROOT_TAG,
            "artifact.svg_root",
            SVG_ROOT_TAG,
            root,
            "The exported file does not parse as an SVG document.",
        )
    return {
        "page_name": page_name,
        "format": suffix.lstrip("."),
        "pages": 1,
        "page_size_mm": _page_size_of(page),
        "view_count": len(page.Views),
    }
