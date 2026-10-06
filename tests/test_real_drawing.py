"""Real TechDraw drawing qualification on the pinned AppImage hosts.

Every test here runs against the SHA-verified FreeCAD that CI installed, on both
release lines. Nothing is mocked, and unavailable hardware never becomes a skip:
the point of this file is the half a fake host cannot show -- that the guards do
not fire on correct work, and that a real page really does render to a real PDF.

The split mirrors the capability split in ``drawing.py``:

* creating a page needs only the App-side ``TechDraw`` module, so those cases
  carry the ``freecad`` marker alone;
* rendering a page goes through ``TechDrawGui``, so those cases additionally
  carry ``freecad_gui`` and run in the offscreen GUI lane.

All documents are synthetic. No case makes a rendered-image assertion: an image
comparison would pin Qt's rasteriser, not the drawing.
"""

import os
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge
from dcc_mcp_freecad.drawing import SVG_ROOT_TAG, pdf_page_count, svg_root_tag

VIEWS = ["isometric", "front", "top"]


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


def _require_host() -> str:
    executable = _real_freecad()
    assert executable and Path(executable).is_file(), (
        "Real drawing tests require FREECAD_TEST_EXECUTABLE"
    )
    return executable


@pytest.fixture(scope="module")
def real_host():
    executable = _require_host()
    assert os.environ.get("FREECAD_REAL_VERSION"), "real drawing tests need the pinned host"
    return executable, os.environ["FREECAD_REAL_VERSION"]


@pytest.fixture(scope="module")
def drawing_document(real_host, tmp_path_factory):
    """A synthetic document with one boolean-cut solid to draw.

    The same shape the modelling lane uses, so a drawing regression cannot hide
    behind a geometry difference.
    """
    executable, version = real_host
    root = tmp_path_factory.mktemp("real-drawing-source")
    document = root / "enclosure.FCStd"
    bridge = FreecadBridge(executable, allowed_roots=[root])
    assert bridge.status()["version"] == version
    bridge.create_document(str(document))
    bridge.add_primitive(
        str(document),
        "box",
        "Body",
        dimensions={"length": 84, "width": 50, "height": 24},
    )
    bridge.add_primitive(
        str(document),
        "cylinder",
        "PortCut",
        dimensions={"radius": 6, "height": 20},
    )
    bridge.transform_object(
        str(document),
        "PortCut",
        translation=[42, 25, 12],
        rotation_axis=[0, 1, 0],
        rotation_degrees=90,
    )
    bridge.boolean_operation(str(document), "cut", "Body", "PortCut", "BodyWithPort")
    return document


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_drawing_capability_is_reported_for_the_pinned_host(tmp_path):
    """Get the honest answer out of the host before the first drawing call."""
    bridge = FreecadBridge(_require_host(), allowed_roots=[tmp_path])

    drawing = bridge.capabilities()["drawing"]

    assert drawing["status"] in ("available", "host_limited")
    assert drawing["views"] == ["front", "isometric", "right", "top"]
    assert drawing["templates"] == ["A4_Landscape", "A4_Portrait"]
    assert drawing["extensions"] == [".pdf", ".svg"]
    # A host that claims the capability has to be able to back it up; a host
    # that cannot must say why, so a caller can gate on it.
    assert bool(drawing["reason"]) == (drawing["status"] == "host_limited")
    if drawing["status"] == "available":
        assert bridge.status()["drawing"]["templates"]


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_drawing_page_holds_every_requested_view(real_host, drawing_document, tmp_path):
    """The page is saved, wired to its template, and every view projected."""
    executable, version = real_host
    bridge = FreecadBridge(executable, allowed_roots=[drawing_document.parent, tmp_path])

    result = bridge.create_drawing_page(
        str(drawing_document),
        ["BodyWithPort"],
        views=VIEWS,
        page_name="Sheet",
    )

    assert result["page_name"] == "Sheet"
    assert result["object_names"] == ["BodyWithPort"]
    assert result["scale"] > 0
    assert result["scale_auto"] is True
    assert [item["view"] for item in result["views"]] == VIEWS
    assert result["template"]["builtin"] == "A4_Landscape"
    assert result["template"]["width_mm"] == pytest.approx(297.0)
    assert result["template"]["height_mm"] == pytest.approx(210.0)

    required = {
        "page.exists",
        "page.type_id",
        "page.template",
        "page.views",
    }
    for view in VIEWS:
        required |= {
            "view.%s.type_id" % view,
            "view.%s.source" % view,
            "view.%s.direction" % view,
            "view.%s.scale" % view,
            "view.%s.position" % view,
            "view.%s.projected" % view,
        }
    assert required <= set(result["verified"])

    # The page is durable, not just present in the process that made it: a
    # separate FreeCAD process has to find it on the document.
    inspected = bridge.inspect_document(str(drawing_document))
    names = {item["name"] for item in inspected["objects"]}
    assert {"Sheet", "Sheet_Template"} <= names
    assert {"Sheet_Isometric", "Sheet_Front", "Sheet_Top"} <= names
    assert bridge.status()["version"] == version
    # The staging copy and the native backup FreeCAD writes beside it are owned
    # by the call, so none of them may survive next to the document.
    assert not list(drawing_document.parent.glob(".*.FCStd"))
    assert not list(drawing_document.parent.glob(".*.FCBak"))


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_drawing_page_refuses_geometry_it_cannot_project(real_host, tmp_path):
    """A mesh source is refused instead of yielding an empty page.

    TechDraw projects Part shapes, so a mesh is the boundary case that a
    permissive implementation would happily turn into a blank drawing.
    """
    executable, _version = real_host
    bridge = FreecadBridge(executable, allowed_roots=[tmp_path])
    source = tmp_path / "mesh-source.FCStd"
    bridge.create_document(str(source))
    bridge.add_primitive(
        str(source), "box", "Body", dimensions={"length": 20, "width": 10, "height": 5}
    )
    stl = tmp_path / "body.stl"
    bridge.export_geometry(str(source), ["Body"], str(stl))
    meshed = tmp_path / "meshed.FCStd"
    bridge.create_document(str(meshed))
    bridge.import_geometry(str(meshed), str(stl), "MeshBody")

    before = meshed.read_bytes()
    with pytest.raises(BridgeError, match="no projectable geometry"):
        bridge.create_drawing_page(str(meshed), ["MeshBody"])

    assert meshed.read_bytes() == before, "a refused drawing must not touch the document"


@pytest.mark.freecad
@pytest.mark.freecad_gui
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_drawing_export_pdf_parses_to_one_page(real_host, drawing_document, tmp_path):
    """The acceptance criterion: a non-empty PDF that parses to one page.

    "The export ran without raising" is not evidence. The magic bytes and the
    page count read back out of the file are.
    """
    executable, version = real_host
    bridge = FreecadBridge(executable, allowed_roots=[drawing_document.parent, tmp_path])
    bridge.create_drawing_page(str(drawing_document), ["BodyWithPort"], views=VIEWS)
    output = tmp_path / "enclosure.pdf"

    result = bridge.export_drawing(str(drawing_document), "Page1", str(output))

    assert result["format"] == "pdf"
    assert result["pages"] == 1
    assert result["bytes"] == output.stat().st_size > 100
    assert result["sha256"]
    assert {"artifact.non_empty", "artifact.page_count"} <= set(result["verified"])
    assert output.read_bytes()[:5] == b"%PDF-"
    # Parsed again here, outside the host process, by the same contract helper.
    assert pdf_page_count(str(output)) == 1
    assert result["page_size_mm"] == pytest.approx([297.0, 210.0])
    assert result["view_count"] == len(VIEWS)
    assert bridge.status()["version"] == version


@pytest.mark.freecad
@pytest.mark.freecad_gui
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_drawing_export_svg_parses_as_an_svg_document(real_host, drawing_document, tmp_path):
    executable, _version = real_host
    bridge = FreecadBridge(executable, allowed_roots=[drawing_document.parent, tmp_path])
    bridge.create_drawing_page(str(drawing_document), ["BodyWithPort"], views=VIEWS)
    output = tmp_path / "enclosure.svg"

    result = bridge.export_drawing(str(drawing_document), "Page1", str(output))

    assert result["format"] == "svg"
    assert result["bytes"] == output.stat().st_size > 0
    assert {"artifact.non_empty", "artifact.svg_root"} <= set(result["verified"])
    assert svg_root_tag(str(output)) == SVG_ROOT_TAG


@pytest.mark.freecad
@pytest.mark.freecad_gui
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_drawing_export_leaves_the_document_and_destination_alone(
    real_host, drawing_document, tmp_path
):
    """Exporting is a read of the document and an exclusive write of the file."""
    executable, _version = real_host
    bridge = FreecadBridge(executable, allowed_roots=[drawing_document.parent, tmp_path])
    bridge.create_drawing_page(str(drawing_document), ["BodyWithPort"], views=VIEWS)
    output = tmp_path / "enclosure.pdf"
    output.write_bytes(b"existing synthetic destination")

    before = drawing_document.read_bytes()
    with pytest.raises(BridgeError, match="already exists"):
        bridge.export_drawing(str(drawing_document), "Page1", str(output))

    assert output.read_bytes() == b"existing synthetic destination"
    assert drawing_document.read_bytes() == before

    bridge.export_drawing(str(drawing_document), "Page1", str(output), overwrite=True)
    assert output.read_bytes()[:5] == b"%PDF-"
    assert drawing_document.read_bytes() == before
    assert not list(tmp_path.glob(".*.pdf"))
