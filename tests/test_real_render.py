"""Real-hardware render qualification on the SHA-verified, pinned AppImages.

This is the CI half of the render_view contract. `tests/test_raster.py` and
`tests/test_render_view.py` prove the verdict logic against synthesised frames;
this suite proves a real FreeCAD host can produce a frame worth judging at all,
on both release lines, that the read-only view-state restore actually holds on a
host, and that the refusal path works end to end rather than against a mock.

Selecting these tests requires a host; an unavailable GUI never becomes a skip.
All documents are synthetic.
"""

import base64
import hashlib
import os
from pathlib import Path

import pytest

from dcc_mcp_freecad import raster
from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge, RenderVerificationError
from dcc_mcp_freecad.presentation import MAX_RENDER_HEIGHT, MAX_RENDER_WIDTH

pytestmark = [pytest.mark.freecad, pytest.mark.freecad_gui]
HOST_SCRIPT = Path(__file__).parent / "native/presentation_host.py"
VIEWS = ["isometric", "front", "top", "right"]
SELECTION = ["BodyWithPort", "PortCut"]
# Small enough to keep four views affordable; the verdict is a fraction of
# pixels, so it does not depend on the capture size.
SIZE = 640


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _files(directory):
    return sorted(path.relative_to(directory).as_posix() for path in Path(directory).rglob("*"))


@pytest.fixture(scope="module")
def real_host():
    executable = os.environ.get("FREECAD_TEST_EXECUTABLE", "")
    expected = os.environ.get("FREECAD_REAL_VERSION", "")
    assert executable and Path(executable).is_file(), "Selected render tests require real FreeCAD"
    assert expected in ("1.0.2", "1.1.4"), "Render qualification requires the exact pinned host"
    assert os.environ.get("QT_QPA_PLATFORM", "offscreen") == "offscreen"
    return executable, expected


@pytest.fixture(scope="module")
def rendered_document(real_host, tmp_path_factory):
    executable, version = real_host
    root = tmp_path_factory.mktemp("native-render-source")
    source = root / "source.FCStd"
    bridge = FreecadBridge(executable, allowed_roots=[root])
    assert bridge.status()["version"] == version
    bridge.create_document(str(source))
    bridge.add_primitive(
        str(source),
        "box",
        "Body",
        dimensions={"length": 8, "width": 6, "height": 4},
        translation=[1, 2, 3],
        rotation_axis=[0, 0, 1],
        rotation_degrees=30,
    )
    bridge.add_primitive(
        str(source),
        "cylinder",
        "PortCut",
        dimensions={"radius": 1, "height": 6},
        translation=[3, 5, 2],
    )
    bridge.boolean_operation(str(source), "cut", "Body", "PortCut", "BodyWithPort")
    return source, source.read_bytes(), _sha(source)


def _bridge(executable, roots, fixture=False):
    bridge = FreecadBridge(executable, allowed_roots=roots)
    if fixture:
        bridge.driver_path = HOST_SCRIPT.resolve()
    return bridge


def _unchanged(rendered_document):
    source, original, digest = rendered_document
    assert source.read_bytes() == original
    assert _sha(source) == digest


@pytest.mark.parametrize("view", VIEWS)
def test_real_render_is_not_a_waste_frame(
    real_host, rendered_document, tmp_path, view, record_property
):
    executable, version = real_host
    source, _original, _digest = rendered_document
    output = tmp_path / ("%s.png" % view)
    result = _bridge(executable, [source.parent, tmp_path]).render_view(
        str(source),
        output_path=str(output),
        view=view,
        visible_objects=SELECTION,
        width=SIZE,
        height=SIZE,
    )
    assert result["view"] == view
    assert result["visible_objects"] == SELECTION
    assert result["width"] == SIZE and result["height"] == SIZE
    assert "render.presentation_request" in result["verified"]
    assert "render.view_state_restored" in result["verified"]
    assert result["geometry_pixel_fraction"] >= raster.MIN_GEOMETRY_PIXEL_FRACTION
    assert result["statistics"]["pixel_count"] == SIZE * SIZE
    assert result["statistics"]["luminance_stddev"] >= raster.MIN_LUMINANCE_STDDEV
    assert raster.degeneracy_reasons(result["statistics"]) == []
    # The published artefact is the frame that was judged, re-measured here.
    published = output.read_bytes()
    assert published[: len(raster.PNG_SIGNATURE)] == raster.PNG_SIGNATURE
    width, height, opaque, rgb = raster.decode_rgb(published)
    summary = raster.describe(width, height, opaque, raster.statistics(rgb))
    assert (summary["width"], summary["height"]) == (SIZE, SIZE)
    assert raster.degeneracy_reasons(summary) == []
    assert summary["pixel_count"] == result["statistics"]["pixel_count"]
    _unchanged(rendered_document)
    record_property("render_host_version", version)
    record_property("render_view", view)
    record_property("render_geometry_pixel_fraction", result["geometry_pixel_fraction"])
    record_property("render_luminance_stddev", summary["luminance_stddev"])


def test_real_render_restores_the_native_view_state_byte_for_byte(
    real_host, rendered_document, tmp_path
):
    """A tool whose only job is to look must not leave the view somewhere else.

    The camera is compared as the host's own serialized string, so this is an
    exactness claim, not a tolerance. Framing the scene moves the camera and
    changes visibility; both have to come back.
    """
    executable, _version = real_host
    source = rendered_document[0]
    result = _bridge(executable, [source.parent, tmp_path]).render_view(
        str(source), visible_objects=SELECTION, width=SIZE, height=SIZE
    )
    before = result["view_state_before"]
    after = result["view_state_after"]
    assert set(before) == {"camera", "camera_type", "visibility", "selection"}
    assert after["camera"] == before["camera"]
    assert after["camera_type"] == before["camera_type"]
    assert after["visibility"] == before["visibility"]
    assert after["selection"] == before["selection"]
    assert before["camera_type"] == "Orthographic"
    assert before["camera"].strip()
    # The render really did reframe: every selected object ends up visible, and
    # the document's own state is then put back.
    assert before["visibility"]
    _unchanged(rendered_document)


def test_real_render_at_the_maximum_size_is_accepted(real_host, rendered_document, tmp_path):
    executable, _version = real_host
    source = rendered_document[0]
    result = _bridge(executable, [source.parent, tmp_path]).render_view(
        str(source), visible_objects=SELECTION
    )
    assert (result["width"], result["height"]) == (MAX_RENDER_WIDTH, MAX_RENDER_HEIGHT)
    assert result["statistics"]["pixel_count"] == MAX_RENDER_WIDTH * MAX_RENDER_HEIGHT
    assert result["geometry_pixel_fraction"] >= raster.MIN_GEOMETRY_PIXEL_FRACTION
    assert result["output_path"] is None
    # With no output_path the frame is measured and discarded, not left behind.
    assert _files(tmp_path) == []
    _unchanged(rendered_document)


def test_real_render_include_image_matches_the_published_file(
    real_host, rendered_document, tmp_path
):
    executable, _version = real_host
    source = rendered_document[0]
    output = tmp_path / "with-image.png"
    result = _bridge(executable, [source.parent, tmp_path]).render_view(
        str(source),
        output_path=str(output),
        visible_objects=SELECTION,
        include_image=True,
        width=SIZE,
        height=SIZE,
    )
    assert base64.b64decode(result["image_base64"]) == output.read_bytes()
    assert result["image_media_type"] == "image/png"
    assert result["sha256"] == _sha(output)
    _unchanged(rendered_document)


def test_real_render_existing_destination_requires_overwrite(
    real_host, rendered_document, tmp_path
):
    executable, _version = real_host
    source = rendered_document[0]
    output = tmp_path / "existing.png"
    output.write_bytes(b"previous render")
    before = _files(tmp_path)
    with pytest.raises(BridgeError, match="Output already exists"):
        _bridge(executable, [source.parent, tmp_path]).render_view(
            str(source),
            output_path=str(output),
            visible_objects=SELECTION,
            width=SIZE,
            height=SIZE,
        )
    assert output.read_bytes() == b"previous render"
    assert _files(tmp_path) == before
    _unchanged(rendered_document)


def test_real_render_capability_is_available_on_these_hosts(real_host, tmp_path):
    executable, _version = real_host
    bridge = _bridge(executable, [tmp_path])
    report = bridge._render_capability(bridge.status())
    assert report["status"] == "available", report["remediation"]
    assert report["host_gui_importable"] is True
    assert report["waste_image_detection"] is True


@pytest.mark.parametrize(
    "fault,code", [("flat", "degenerate_render"), ("baseline_copy", "empty_render")]
)
def test_real_waste_frame_is_refused_and_never_published(
    real_host, rendered_document, tmp_path, monkeypatch, fault, code
):
    """The end-to-end refusal path, on a real host.

    Only the bytes the host wrote are corrupted, after the real capture. The
    production driver, its read-back and the adapter's verdict all run for real,
    so this proves the whole chain rather than a patched comparison.
    """
    executable, _version = real_host
    source = rendered_document[0]
    output = tmp_path / "refused.png"
    monkeypatch.setenv("DCC_MCP_FREECAD_TEST_RENDER_FAULT", fault)
    with pytest.raises(RenderVerificationError) as caught:
        _bridge(executable, [source.parent, tmp_path], fixture=True).render_view(
            str(source),
            output_path=str(output),
            overwrite=True,
            visible_objects=SELECTION,
            width=SIZE,
            height=SIZE,
        )
    error = caught.value
    assert error.error_code == code
    assert error.statistics["pixel_count"] == SIZE * SIZE
    assert error.payload["remediation"]
    # Nothing is published: a refused frame must not reach the caller's path.
    assert not output.exists()
    assert _files(tmp_path) == []
    _unchanged(rendered_document)
