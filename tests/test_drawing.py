"""TechDraw drawing pages: the read-back contract for 2D output.

The failure mode this file exists for is the one the drawing path invites: a
page and its views are objects that FreeCAD is happy to create whether or not
anything was ever projected into them, and a renderer that produces an empty or
unparseable file still exits without an error. A caller that receives
"exported" then sends a zero-byte PDF to a machinist.

The tests come in two halves, matching the two halves of the module:

* pure helpers, which need no host at all -- projection frames, auto-fitted
  scales, and PDF/SVG artefact parsing;
* driver methods against a fake FreeCAD that stores what it is told, so a
  dropped view or an empty render is observably missing.

The real-host lane lives in ``tests/test_real_drawing.py`` and is what proves
these guards do not fire on correct work.
"""

from __future__ import annotations

import sys
import types
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import drawing, freecad_driver, write_contract  # noqa: E402

HOST_VERSION = "1.1.4"


# ---------------------------------------------------------------------------
# Pure helpers: no host, no FreeCAD
# ---------------------------------------------------------------------------


def test_every_view_direction_has_a_non_parallel_x_direction():
    """A view frame whose X axis is parallel to its view direction is degenerate.

    The right-hand view is the one that would hit this with the default
    (1, 0, 0), so it is not a hypothetical.
    """
    for name, (direction, x_direction) in drawing.VIEWS.items():
        u, v = drawing.view_frame(direction, x_direction)
        forward = drawing._unit(direction)
        assert abs(drawing._dot(u, forward)) < 1e-9, name
        assert abs(drawing._dot(v, forward)) < 1e-9, name
        assert abs(drawing._dot(u, v)) < 1e-9, name
        for axis in (u, v):
            assert abs(drawing._dot(axis, axis) - 1.0) < 1e-9


def test_a_parallel_view_frame_is_refused():
    with pytest.raises(ValueError, match="parallel"):
        drawing.view_frame((1.0, 0.0, 0.0), (1.0, 0.0, 0.0))


class _Box:
    def __init__(self, low, high):
        self.XMin, self.YMin, self.ZMin = low
        self.XMax, self.YMax, self.ZMax = high


def test_projected_extents_match_the_faces_a_view_actually_sees():
    box = _Box((0.0, 0.0, 0.0), (80.0, 50.0, 24.0))

    front = drawing.projected_extents(box, *drawing.VIEWS["front"])
    top = drawing.projected_extents(box, *drawing.VIEWS["top"])
    right = drawing.projected_extents(box, *drawing.VIEWS["right"])

    assert front == pytest.approx((80.0, 24.0))
    assert top == pytest.approx((80.0, 50.0))
    assert right == pytest.approx((50.0, 24.0))
    # An isometric view sees a corner, so it is wider than any single face.
    assert drawing.projected_extents(box, *drawing.VIEWS["isometric"])[0] > 80.0


def test_grid_layout_stays_inside_the_page_margin():
    centers, cell = drawing.grid_layout(3, 297.0, 210.0)

    assert len(centers) == 3
    assert cell[0] > 0 and cell[1] > 0
    for x, y in centers:
        assert drawing.PAGE_MARGIN_MM <= x <= 297.0 - drawing.PAGE_MARGIN_MM
        assert drawing.PAGE_MARGIN_MM <= y <= 210.0 - drawing.PAGE_MARGIN_MM
    # Two columns then a wrapped third cell, top row first.
    assert centers[0][1] == centers[1][1] > centers[2][1]
    assert centers[0][0] < centers[1][0]


def test_grid_layout_refuses_a_page_with_no_room():
    with pytest.raises(ValueError, match="no room"):
        drawing.grid_layout(1, 10.0, 10.0)


def test_fit_scale_picks_the_largest_standard_scale_that_fits():
    cell = (100.0, 100.0)

    assert drawing.fit_scale([(80.0, 24.0)], cell) == 1.0
    assert drawing.fit_scale([(800.0, 240.0)], cell) == 0.1
    assert drawing.fit_scale([(8000.0, 2400.0)], cell) == 0.01
    # Every view has to fit, so the largest projected extent wins.
    assert drawing.fit_scale([(10.0, 10.0), (1000.0, 10.0)], cell) == 0.05


def test_fit_scale_clamps_a_part_that_fits_at_every_scale():
    # A 0.5 mm part on an A4 page would fit at 100:1 and beyond; the series
    # stops there rather than inventing an unbounded enlargement.
    assert drawing.fit_scale([(0.5, 0.5)], (200.0, 200.0)) == drawing.SCALE_SERIES[0]


def test_fit_scale_refuses_geometry_with_no_extent():
    with pytest.raises(ValueError, match="no projectable geometry"):
        drawing.fit_scale([(0.0, 10.0)], (100.0, 100.0))


def test_validate_scale_refuses_an_explicit_zero_instead_of_defaulting():
    """A scale of 0 is refused, not silently replaced.

    ``params.get("scale") or 1.0`` would accept the parameter and then ignore
    it, which is the swallowed-parameter behaviour the contract forbids.
    """
    assert drawing.validate_scale(None) is None
    assert drawing.validate_scale(0.5) == 0.5
    with pytest.raises(ValueError, match="finite positive"):
        drawing.validate_scale(0)
    with pytest.raises(ValueError, match="finite positive"):
        drawing.validate_scale(-1)


def test_views_are_bounded_and_unique():
    assert drawing.validate_views(None) == list(drawing.DEFAULT_VIEWS)
    assert drawing.validate_views(["front", "top"]) == ["front", "top"]
    with pytest.raises(ValueError, match="Unsupported drawing view: side"):
        drawing.validate_views(["side"])
    with pytest.raises(ValueError, match="unique"):
        drawing.validate_views(["front", "front"])
    with pytest.raises(ValueError, match="between 1 and"):
        drawing.validate_views([])


# ---------------------------------------------------------------------------
# Artefact parsing
# ---------------------------------------------------------------------------


def _pdf(page_count, compress=False):
    """A minimal PDF whose page tree is either plain or inside a flate stream."""
    pages = b" ".join(b"%d 0 R" % (3 + index) for index in range(page_count))
    tree = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (pages, page_count)
    leaves = b"\n".join(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>" for _ in range(page_count)
    )
    if not compress:
        body = b"\n".join((b"<< /Type /Catalog /Pages 2 0 R >>", tree, leaves))
    else:
        compressed = zlib.compress(b"\n".join((tree, leaves)))
        body = b"\n".join(
            (
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Length %d /Filter /FlateDecode >>" % len(compressed),
                b"stream",
            )
        )
        body += b"\n" + compressed + b"\nendstream"
    return b"%%PDF-1.4\n" + body + b"\ntrailer\n%%%%EOF\n"


def test_pdf_page_count_reads_a_plain_page_tree(tmp_path):
    path = tmp_path / "drawing.pdf"
    path.write_bytes(_pdf(3))

    assert drawing.pdf_page_count(str(path)) == 3


def test_pdf_page_count_reads_a_compressed_page_tree(tmp_path):
    """Object streams must not turn a real PDF into "cannot be determined".

    Without the flate fallback a compressed page tree reads as an unknown page
    count, and the export read-back then fails on correct output.
    """
    path = tmp_path / "compressed.pdf"
    path.write_bytes(_pdf(1, compress=True))

    assert drawing.pdf_page_count(str(path)) == 1


def test_pdf_page_count_returns_none_for_a_file_that_is_not_a_pdf(tmp_path):
    path = tmp_path / "drawing.pdf"
    path.write_bytes(b"not a pdf at all")

    assert drawing.pdf_page_count(str(path)) is None


def test_pdf_page_count_returns_none_for_a_missing_file(tmp_path):
    assert drawing.pdf_page_count(str(tmp_path / "absent.pdf")) is None


def test_svg_root_tag_identifies_an_svg_document(tmp_path):
    good = tmp_path / "drawing.svg"
    good.write_bytes(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>')
    broken = tmp_path / "broken.svg"
    broken.write_bytes(b"<svg><unclosed>")

    assert drawing.svg_root_tag(str(good)) == drawing.SVG_ROOT_TAG
    assert drawing.svg_root_tag(str(broken)) is None


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_builtin_templates_ship_next_to_the_driver():
    """A built-in that is missing at export time has no host-layout excuse.

    The templates are package data, so this is the one place a packaging
    mistake is cheap to catch.
    """
    root = Path(freecad_driver.__file__).parent

    assert drawing.builtin_templates(str(root)) == sorted(drawing.BUILTIN_TEMPLATES)


def test_resolve_template_defaults_to_the_builtin_a4_landscape(tmp_path):
    root = Path(freecad_driver.__file__).parent

    path, builtin, size = drawing.resolve_template(None, str(root))

    assert builtin == "A4_Landscape"
    assert Path(path).is_file()
    assert size == pytest.approx((297.0, 210.0))


def test_resolve_template_accepts_an_explicit_svg_file(tmp_path):
    template = tmp_path / "custom.svg"
    template.write_text('<svg width="420mm" height="297mm"/>', encoding="utf-8")

    path, builtin, size = drawing.resolve_template(str(template))

    assert path == str(template.resolve() if hasattr(Path, "resolve") else path)
    assert builtin is None
    assert size == pytest.approx((420.0, 297.0))


def test_resolve_template_refuses_a_missing_file(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        drawing.resolve_template(str(tmp_path / "absent.svg"))


def test_resolve_template_refuses_an_unknown_name(tmp_path):
    with pytest.raises(ValueError, match="Unknown drawing template"):
        drawing.resolve_template("A2_Landscape", str(tmp_path))


def test_page_size_refuses_a_template_with_no_usable_size(tmp_path):
    template = tmp_path / "unsized.svg"
    template.write_text('<svg viewBox="0 0 100 100"/>', encoding="utf-8")

    with pytest.raises(ValueError, match="no usable page size"):
        drawing.page_size(str(template))


# ---------------------------------------------------------------------------
# A fake FreeCAD that stores what it is told, so a dropped write is observable
# ---------------------------------------------------------------------------


class _Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))


class _BoundBox:
    def __init__(self, low=(0.0, 0.0, 0.0), high=(80.0, 50.0, 24.0)):
        self.XMin, self.YMin, self.ZMin = low
        self.XMax, self.YMax, self.ZMax = high

    def add(self, other):
        self.XMin = min(self.XMin, other.XMin)
        self.YMin = min(self.YMin, other.YMin)
        self.ZMin = min(self.ZMin, other.ZMin)
        self.XMax = max(self.XMax, other.XMax)
        self.YMax = max(self.YMax, other.YMax)
        self.ZMax = max(self.ZMax, other.ZMax)


class _Shape:
    def __init__(self, box=None, null=False):
        self.BoundBox = box if box is not None else _BoundBox()
        self._null = null

    def isNull(self):
        return self._null


class _Object:
    """A document object that stores writes, unless ``silenced``."""

    _IDENTITY = frozenset({"TypeId", "Name", "Label", "_doc", "_silenced"})

    def __init__(self, type_id, name, doc):
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        self.Source = []
        self.Direction = None
        self.XDirection = None
        self.ScaleType = None
        self.Scale = None
        self.X = None
        self.Y = None
        self.Template = None
        self.Views = []
        self.Shape = None
        self._silenced = False
        self._doc = doc

    def __setattr__(self, key, value):
        if key in _Object._IDENTITY or key.startswith("__"):
            object.__setattr__(self, key, value)
            return
        if getattr(self, "_silenced", False):
            return
        object.__setattr__(self, key, value)

    def addView(self, view):
        self.Views.append(view)


class _Document:
    def __init__(self, name, path="", app=None):
        self.Name = name
        self.Label = name
        self.FileName = path
        self.Objects = []
        self.saves = 0
        self._app = app

    def addObject(self, type_id, name):
        obj = _Object(type_id, name, self)
        if type_id.startswith("Part::") or type_id == "Part::Feature":
            obj.Shape = _Shape()
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self):
        pass

    def save(self):
        self.saves += 1


class _App:
    def __init__(self, version=HOST_VERSION):
        self.Version = lambda: version.split(".") + ["extra"]
        self.Vector = _Vec
        self.documents = {}
        self.GuiUp = False

    def ParamGet(self, _path):
        return types.SimpleNamespace(SetBool=lambda *_args: None)

    def newDocument(self, name):
        doc = _Document(name, "", self)
        self.documents[name] = doc
        return doc

    def openDocument(self, path):
        key = str(path)
        if key not in self.documents:
            self.documents[key] = _Document(Path(key).stem, key, self)
        return self.documents[key]

    def closeDocument(self, name):
        self.documents.pop(name, None)


class FakeTechDraw(types.ModuleType):
    def __init__(self):
        super().__init__("TechDraw")
        # What a projected view returns. Empty models the bug class: the view
        # object was created and saved, and nothing was ever projected into it.
        self.projection = '<g id="view"><path d="M0 0 L10 0"/></g>'
        self.rendered = []

    def viewPartAsSvg(self, view):
        self.rendered.append(view.Name)
        return self.projection


class FakeGui(types.ModuleType):
    def __init__(self, app):
        super().__init__("FreeCADGui")
        self._app = app
        self.main_window_calls = 0
        self.documents = {}

    def showMainWindow(self):
        self.main_window_calls += 1
        if self._app.gui_available:
            self._app.GuiUp = True

    def getDocument(self, name):
        if name not in self.documents:
            raise RuntimeError("no such GUI document: %s" % name)
        return self.documents[name]


class FakeTechDrawGui(types.ModuleType):
    def __init__(self):
        super().__init__("TechDrawGui")
        self.exports = []
        # Bytes written per suffix; empty models a renderer that produced
        # nothing while still returning without an error.
        self.payload = {".pdf": None, ".svg": None}

    def _write(self, kind, page, path):
        self.exports.append((kind, page.Name, str(path)))
        data = self.payload[kind]
        if data is None:
            data = (
                _pdf(1)
                if kind == ".pdf"
                else b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
            )
        Path(path).write_bytes(data)

    def exportPageAsPdf(self, page, path):
        self._write(".pdf", page, path)

    def exportPageAsSvg(self, page, path):
        self._write(".svg", page, path)


@pytest.fixture()
def host(monkeypatch):
    """Install a fake FreeCAD plus fake TechDraw and GUI modules.

    The driver loads ``write_contract.py`` and ``drawing.py`` by path, so those
    module objects are not the ones imported here. Seeding the driver's cache
    with the imported modules makes them the same object, which is what lets a
    test catch the exact error type the driver raises.
    """
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_write_contract"] = write_contract
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_drawing"] = drawing
    app = _App()
    app.gui_available = True
    gui = FakeGui(app)
    techdraw = FakeTechDraw()
    techdraw_gui = FakeTechDrawGui()
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "FreeCADGui", gui)
    monkeypatch.setitem(sys.modules, "TechDraw", techdraw)
    monkeypatch.setitem(sys.modules, "TechDrawGui", techdraw_gui)
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    return types.SimpleNamespace(
        app=app,
        gui=gui,
        techdraw=techdraw,
        techdraw_gui=techdraw_gui,
        template_root=str(Path(freecad_driver.__file__).parent),
    )


def _document(host, tmp_path, name="model"):
    path = tmp_path / ("%s.FCStd" % name)
    path.write_bytes(b"document")
    return host.app.openDocument(str(path)), str(path)


def _with_body(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.addObject("Part::Box", "Body")
    body.Shape = _Shape()
    return doc, path


def _silence_new_objects(doc, type_id="TechDraw::DrawViewPart"):
    """Make every object of ``type_id`` created from here on drop its writes."""
    original = doc.addObject

    def addObject(created_type_id, name):
        obj = original(created_type_id, name)
        if created_type_id == type_id:
            obj._silenced = True
        return obj

    doc.addObject = addObject
    return original


def _mismatch(excinfo):
    error = excinfo.value
    assert isinstance(error, write_contract.WriteVerificationError), type(error)
    assert error.tool, "the error must name the tool"
    assert error.check, "the error must name the check that disagreed"
    assert "expected" in error.payload and "actual" in error.payload
    assert error.host_version == HOST_VERSION
    return error


# ---------------------------------------------------------------------------
# drawing.create_page
# ---------------------------------------------------------------------------


def test_create_page_reports_the_checks_it_ran(host, tmp_path):
    _doc, path = _with_body(host, tmp_path)

    result = freecad_driver.drawing_create_page(
        {
            "document_path": path,
            "object_names": ["Body"],
            "views": ["isometric", "front", "top"],
            "template": "A4_Landscape",
        }
    )

    assert result["page_name"] == "Page1"
    assert result["object_names"] == ["Body"]
    assert result["scale_auto"] is True
    assert [item["view"] for item in result["views"]] == ["isometric", "front", "top"]
    for check in (
        "page.exists",
        "page.type_id",
        "page.template",
        "page.views",
        "view.front.type_id",
        "view.front.source",
        "view.front.direction",
        "view.front.scale",
        "view.front.position",
        "view.front.projected",
    ):
        assert check in result["verified"], check


def test_create_page_saves_before_it_reads_back(host, tmp_path):
    """The page must be durable before anything is asserted about it.

    A read-back taken in memory would pass on a page that was never saved, and
    the caller would then find nothing to export in a later process.
    """
    doc, path = _with_body(host, tmp_path)

    freecad_driver.drawing_create_page({"document_path": path, "object_names": ["Body"]})

    assert doc.saves == 1


def test_create_page_refuses_when_a_view_write_did_not_land(host, tmp_path):
    """The view object exists and was saved, but none of its wiring took.

    This is the drawing flavour of "reported success, model unchanged": the
    page is on the document, the view is on the page, and the view is wired to
    nothing at all.
    """
    doc, path = _with_body(host, tmp_path)
    _silence_new_objects(doc)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.drawing_create_page(
            {"document_path": path, "object_names": ["Body"], "views": ["front"]}
        )

    error = _mismatch(excinfo)
    assert error.check == "view.front.source"
    assert error.expected == ["Body"]
    assert error.actual == []


def test_create_page_refuses_a_view_that_projected_nothing(host, tmp_path):
    """A view object that exists but rendered nothing is the drawing bug class.

    The object, its links, its direction and its scale are all correct; only the
    projection is empty, which is exactly what a page full of nothing looks
    like to every other check.
    """
    _doc, path = _with_body(host, tmp_path)
    host.techdraw.projection = ""

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.drawing_create_page(
            {"document_path": path, "object_names": ["Body"], "views": ["front"]}
        )

    error = _mismatch(excinfo)
    assert error.check == "view.front.projected"
    assert error.expected == "a non-empty SVG projection"


def test_create_page_refuses_a_page_that_lost_its_template(host, tmp_path):
    doc, path = _with_body(host, tmp_path)

    def addObject(type_id, name):
        obj = _Document.addObject(doc, type_id, name)
        if type_id == "TechDraw::DrawPage":
            obj._silenced = True
        return obj

    doc.addObject = addObject

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.drawing_create_page(
            {"document_path": path, "object_names": ["Body"], "views": ["front"]}
        )

    assert _mismatch(excinfo).check == "page.template"


def test_create_page_refuses_a_source_with_no_projectable_geometry(host, tmp_path):
    doc, path = _document(host, tmp_path)
    empty = doc.addObject("Part::Feature", "Empty")
    empty.Shape = _Shape(null=True)

    with pytest.raises(ValueError, match="Empty has no projectable geometry"):
        freecad_driver.drawing_create_page({"document_path": path, "object_names": ["Empty"]})


def test_create_page_refuses_an_object_that_does_not_exist(host, tmp_path):
    _doc, path = _with_body(host, tmp_path)

    with pytest.raises(ValueError, match="Object does not exist: Missing"):
        freecad_driver.drawing_create_page({"document_path": path, "object_names": ["Missing"]})


def test_create_page_refuses_to_replace_an_existing_page(host, tmp_path):
    _doc, path = _with_body(host, tmp_path)
    freecad_driver.drawing_create_page(
        {"document_path": path, "object_names": ["Body"], "page_name": "Sheet"}
    )

    with pytest.raises(ValueError, match="Object already exists: Sheet"):
        freecad_driver.drawing_create_page(
            {"document_path": path, "object_names": ["Body"], "page_name": "Sheet"}
        )


def test_create_page_honours_an_explicit_scale_and_says_it_is_not_fitted(host, tmp_path):
    _doc, path = _with_body(host, tmp_path)

    result = freecad_driver.drawing_create_page(
        {
            "document_path": path,
            "object_names": ["Body"],
            "views": ["front"],
            "scale": 0.25,
        }
    )

    assert result["scale"] == 0.25
    assert result["scale_auto"] is False
    assert all(item["scale"] == 0.25 for item in result["views"])


def test_create_page_refuses_an_explicit_zero_scale(host, tmp_path):
    _doc, path = _with_body(host, tmp_path)

    with pytest.raises(ValueError, match="scale must be a finite positive number"):
        freecad_driver.drawing_create_page(
            {"document_path": path, "object_names": ["Body"], "scale": 0}
        )


def test_create_page_reports_the_template_it_used(host, tmp_path):
    _doc, path = _with_body(host, tmp_path)

    result = freecad_driver.drawing_create_page(
        {"document_path": path, "object_names": ["Body"], "template": "A4_Portrait"}
    )

    assert result["template"]["builtin"] == "A4_Portrait"
    assert result["template"]["width_mm"] == pytest.approx(210.0)
    assert result["template"]["height_mm"] == pytest.approx(297.0)


# ---------------------------------------------------------------------------
# drawing.export
# ---------------------------------------------------------------------------


def _page(host, tmp_path, views=("front",)):
    doc, path = _with_body(host, tmp_path)
    freecad_driver.drawing_create_page(
        {"document_path": path, "object_names": ["Body"], "views": list(views)}
    )
    host.gui.documents[doc.Name] = types.SimpleNamespace(activeView=lambda: None)
    return doc, path


def test_export_page_proves_the_pdf_parses_to_one_page(host, tmp_path):
    doc, path = _page(host, tmp_path)
    output = tmp_path / "drawing.pdf"

    result = freecad_driver.drawing_export(
        {"document_path": path, "page_name": "Page1", "output_path": str(output)}
    )

    assert result["format"] == "pdf"
    assert result["pages"] == 1
    for check in ("artifact.non_empty", "artifact.page_count"):
        assert check in result["verified"], check
    assert host.gui.main_window_calls == 1, "the GUI starts inside the call"
    assert host.app.GuiUp is True


def test_export_page_proves_the_svg_parses(host, tmp_path):
    doc, path = _page(host, tmp_path)
    output = tmp_path / "drawing.svg"

    result = freecad_driver.drawing_export(
        {"document_path": path, "page_name": "Page1", "output_path": str(output)}
    )

    assert result["format"] == "svg"
    assert "artifact.svg_root" in result["verified"]


def test_export_page_refuses_an_empty_artifact(host, tmp_path):
    """A renderer that writes nothing must not be reported as an export.

    This is the acceptance criterion "no empty PDF", and it is the one a
    "ran without raising" check would let through.
    """
    doc, path = _page(host, tmp_path)
    output = tmp_path / "drawing.pdf"
    host.techdraw_gui.payload[".pdf"] = b""

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.drawing_export(
            {"document_path": path, "page_name": "Page1", "output_path": str(output)}
        )

    assert _mismatch(excinfo).check == "artifact.non_empty"


def test_export_page_refuses_a_pdf_with_the_wrong_page_count(host, tmp_path):
    doc, path = _page(host, tmp_path)
    output = tmp_path / "drawing.pdf"
    host.techdraw_gui.payload[".pdf"] = _pdf(2)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.drawing_export(
            {"document_path": path, "page_name": "Page1", "output_path": str(output)}
        )

    error = _mismatch(excinfo)
    assert error.check == "artifact.page_count"
    assert error.expected == 1
    assert error.actual == 2


def test_export_page_refuses_an_svg_that_does_not_parse(host, tmp_path):
    doc, path = _page(host, tmp_path)
    output = tmp_path / "drawing.svg"
    host.techdraw_gui.payload[".svg"] = b"<svg><unclosed>"

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.drawing_export(
            {"document_path": path, "page_name": "Page1", "output_path": str(output)}
        )

    assert _mismatch(excinfo).check == "artifact.svg_root"


def test_export_page_refuses_when_the_gui_library_cannot_start(host, tmp_path):
    doc, path = _page(host, tmp_path)
    host.app.gui_available = False
    output = tmp_path / "drawing.pdf"

    with pytest.raises(RuntimeError, match="GUI view providers are unavailable"):
        freecad_driver.drawing_export(
            {"document_path": path, "page_name": "Page1", "output_path": str(output)}
        )

    assert not output.exists()
    assert host.techdraw_gui.exports == [], "nothing is rendered without a GUI"


def test_export_page_refuses_when_the_page_has_no_view_provider(host, tmp_path):
    doc, path = _page(host, tmp_path)
    host.gui.documents.clear()

    with pytest.raises(RuntimeError, match="no GUI document"):
        freecad_driver.drawing_export(
            {
                "document_path": path,
                "page_name": "Page1",
                "output_path": str(tmp_path / "drawing.pdf"),
            }
        )


def test_export_page_refuses_a_page_with_no_views(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("TechDraw::DrawPage", "EmptyPage")
    host.gui.documents[doc.Name] = types.SimpleNamespace(activeView=lambda: None)

    with pytest.raises(ValueError, match="no views"):
        freecad_driver.drawing_export(
            {
                "document_path": path,
                "page_name": "EmptyPage",
                "output_path": str(tmp_path / "drawing.pdf"),
            }
        )


def test_export_page_refuses_an_object_that_is_not_a_page(host, tmp_path):
    doc, path = _with_body(host, tmp_path)
    host.gui.documents[doc.Name] = types.SimpleNamespace(activeView=lambda: None)

    with pytest.raises(ValueError, match="not a TechDraw page"):
        freecad_driver.drawing_export(
            {
                "document_path": path,
                "page_name": "Body",
                "output_path": str(tmp_path / "drawing.pdf"),
            }
        )


def test_export_page_refuses_an_unsupported_extension(host, tmp_path):
    doc, path = _page(host, tmp_path)

    with pytest.raises(ValueError, match="Unsupported drawing export extension: .png"):
        freecad_driver.drawing_export(
            {"document_path": path, "page_name": "Page1", "output_path": str(tmp_path / "d.png")}
        )


def test_export_page_does_not_save_the_document(host, tmp_path):
    """Exporting is not a mutation: the document it read must not be rewritten."""
    doc, path = _page(host, tmp_path)
    saved = doc.saves

    freecad_driver.drawing_export(
        {
            "document_path": path,
            "page_name": "Page1",
            "output_path": str(tmp_path / "drawing.pdf"),
        }
    )

    assert doc.saves == saved
