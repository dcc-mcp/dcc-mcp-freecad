"""render_view contract tests.

Everything here runs without FreeCAD. The property this tool exists for -- a
waste frame is refused, never returned -- is a decision about bytes, so it is
proven against synthesised PNGs here and only re-confirmed on real hardware.
"""

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from png_fixture import checker, png, ramp, solid

from dcc_mcp_freecad import freecad_driver, presentation, raster, write_contract
from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge, RenderVerificationError


def _bridge(tmp_path):
    return FreecadBridge(allowed_roots=[tmp_path])


def _geometry(width, height, fraction_columns=0.25):
    """A frame where the left band differs from an otherwise flat background."""
    rows = []
    cut = max(1, int(width * fraction_columns))
    for _ in range(height):
        row = bytearray([200, 200, 200] * width)
        for column in range(cut):
            row[3 * column : 3 * column + 3] = b"\x10\x20\x30"
        rows.append(bytes(row))
    return png(width, height, rows)


# --- Bounded input ----------------------------------------------------------


@pytest.mark.parametrize(
    "width,height",
    [
        (0, 720),
        (15, 720),
        (1281, 720),
        (1280, 0),
        (1280, 15),
        (1280, 721),
        (1280.0, 720),
        (True, 720),
        (1280, True),
        ("1280", 720),
        (None, 720),
    ],
)
def test_render_size_is_bounded(width, height):
    with pytest.raises(ValueError, match="must be an integer between"):
        presentation.validate_render_size(width, height)


def test_default_render_size_is_the_maximum():
    assert presentation.validate_render_size(
        presentation.DEFAULT_RENDER_WIDTH, presentation.DEFAULT_RENDER_HEIGHT
    ) == (presentation.MAX_RENDER_WIDTH, presentation.MAX_RENDER_HEIGHT)


@pytest.mark.parametrize("bad", ["document.FCStd", "render.png.txt", "render"])
def test_output_path_must_be_a_png(tmp_path, bad):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    with pytest.raises(BridgeError, match="Unsupported output extension"):
        bridge.render_view(str(source), output_path=str(tmp_path / bad))


def test_a_missing_document_is_refused_before_any_host_call(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", lambda *args: pytest.fail("Host must not start"))
    with pytest.raises(BridgeError, match="does not exist"):
        bridge.render_view(str(tmp_path / "absent.FCStd"))


@pytest.mark.parametrize("flag", ["include_image", "overwrite"])
def test_boolean_flags_reject_non_booleans(tmp_path, monkeypatch, flag):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", lambda *args: pytest.fail("Host must not start"))
    with pytest.raises(BridgeError, match="must be a boolean"):
        bridge.render_view(str(source), **{flag: "yes"})


def test_appearances_require_an_explicit_selection(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", lambda *args: pytest.fail("Host must not start"))
    with pytest.raises(BridgeError, match="explicit visible_objects"):
        bridge.render_view(
            str(source), appearances=[{"object_name": "A", "rgb": [0, 0, 0], "opacity": 1}]
        )


def test_an_invalid_selection_is_refused_before_any_host_call(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", lambda *args: pytest.fail("Host must not start"))
    with pytest.raises(ValueError, match="bounded object name"):
        bridge.render_view(str(source), visible_objects=["1Bad"])


# --- The waste-frame verdict ------------------------------------------------


def _native(subject_bytes, baseline_bytes):
    def invoke(method, params, timeout):
        assert method == "document.render_view"
        Path(params["image_path"]).write_bytes(subject_bytes)
        Path(params["baseline_path"]).write_bytes(baseline_bytes)
        return {
            "view": params.get("view", "isometric"),
            "visible_objects": params.get("visible_objects") or ["Body"],
            "width": params["width"],
            "height": params["height"],
            "presentation": {"camera_type": "Orthographic"},
            "verified": [
                "render.presentation_request",
                "render.view_state_restored",
            ],
        }

    return invoke


@pytest.mark.parametrize("color", [(0, 0, 0), (255, 255, 255), (7, 7, 7)])
def test_a_flat_frame_is_refused_and_never_published(tmp_path, monkeypatch, color):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    output = tmp_path / "render.png"
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(solid(64, 36, *color), ramp(64, 36)))
    with pytest.raises(RenderVerificationError) as caught:
        bridge.render_view(str(source), output_path=str(output), overwrite=True)
    error = caught.value
    assert error.error_code == "degenerate_render"
    assert error.statistics["luminance_stddev"] == 0.0
    assert "flat fill" in str(error)
    assert not output.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["document.FCStd"]
    assert source.read_bytes() == b"original"


def test_a_gradient_background_with_no_geometry_is_refused(tmp_path, monkeypatch):
    """The case pixel variance alone cannot catch.

    FreeCAD bakes its default linear gradient into the saved PNG, so a gradient
    is varied and a naive "is it monochrome?" check accepts it. The empty-scene
    comparison is what refuses it.
    """
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(ramp(64, 36), ramp(64, 36)))
    with pytest.raises(RenderVerificationError) as caught:
        bridge.render_view(str(source))
    error = caught.value
    assert error.error_code == "empty_render"
    assert error.geometry_pixel_fraction == 0.0
    assert error.statistics["luminance_stddev"] > raster.MIN_LUMINANCE_STDDEV
    assert "does not differ from an empty scene" in str(error)
    assert error.payload["remediation"]


def test_a_flat_empty_scene_cannot_mask_a_degenerate_subject(tmp_path, monkeypatch):
    """Identical flat frames are reported as degenerate, not as geometry."""
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(solid(32, 32, 0, 0, 0), solid(32, 32, 0, 0, 0)))
    with pytest.raises(RenderVerificationError) as caught:
        bridge.render_view(str(source))
    assert caught.value.error_code == "degenerate_render"


def test_a_real_frame_passes_and_reports_the_measurement(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(_geometry(80, 40), checker(80, 40)))
    result = bridge.render_view(str(source))
    assert result["view"] == "isometric"
    assert result["width"] == 1280 and result["height"] == 720
    assert result["geometry_pixel_fraction"] > 0.0
    assert result["statistics"]["pixel_count"] == 80 * 40
    assert result["statistics"]["luminance_stddev"] > 0.0
    assert "image_base64" not in result
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*.png"))
    assert source.read_bytes() == b"original"


def test_a_real_frame_is_published_only_after_the_verdict(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    output = tmp_path / "render.png"
    rendered = _geometry(80, 40)
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(rendered, checker(80, 40)))
    result = bridge.render_view(str(source), output_path=str(output))
    assert output.read_bytes() == rendered
    assert result["sha256"] == result["image_sha256"]
    assert result["output_path"] == str(output)
    assert result["overwritten"] is False


@pytest.mark.parametrize("existing", [False, True])
def test_an_existing_destination_requires_overwrite(tmp_path, monkeypatch, existing):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    output = tmp_path / "render.png"
    if existing:
        output.write_bytes(b"previous render")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(_geometry(80, 40), checker(80, 40)))
    if existing:
        with pytest.raises(BridgeError, match="Output already exists"):
            bridge.render_view(str(source), output_path=str(output))
        assert output.read_bytes() == b"previous render"
    else:
        bridge.render_view(str(source), output_path=str(output))
        assert output.read_bytes() == _geometry(80, 40)
    assert source.read_bytes() == b"original"


def test_a_destination_created_during_the_native_call_is_not_replaced(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    output = tmp_path / "render.png"
    bridge = _bridge(tmp_path)

    def invoke(method, params, timeout):
        Path(params["image_path"]).write_bytes(_geometry(80, 40))
        Path(params["baseline_path"]).write_bytes(checker(80, 40))
        output.write_bytes(b"created while rendering")
        return {
            "verified": [],
            "visible_objects": ["Body"],
            "view": "isometric",
            "width": 80,
            "height": 40,
            "presentation": {},
        }

    monkeypatch.setattr(bridge, "_invoke", invoke)
    with pytest.raises(BridgeError, match="Output already exists"):
        bridge.render_view(str(source), output_path=str(output))
    assert output.read_bytes() == b"created while rendering"


def test_include_image_attaches_the_rendered_bytes(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    rendered = _geometry(80, 40)
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(rendered, checker(80, 40)))
    result = bridge.render_view(str(source), include_image=True)
    assert base64.b64decode(result["image_base64"]) == rendered
    assert result["image_media_type"] == "image/png"
    assert result["image_bytes"] == len(rendered)


def test_an_oversized_inline_image_is_refused_with_a_pointer_to_output_path(tmp_path, monkeypatch):
    from dcc_mcp_freecad import bridge as bridge_module

    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(_geometry(80, 40), checker(80, 40)))
    monkeypatch.setattr(bridge_module, "MAX_INLINE_IMAGE_BYTES", 8)
    with pytest.raises(BridgeError, match="output_path"):
        bridge.render_view(str(source), include_image=True)


def test_a_missing_capture_is_reported_rather_than_judged_as_black(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)

    def invoke(method, params, timeout):
        Path(params["image_path"]).write_bytes(solid(16, 16, 0, 0, 0))
        return {
            "verified": [],
            "visible_objects": ["Body"],
            "view": "isometric",
            "width": 16,
            "height": 16,
            "presentation": {},
        }

    monkeypatch.setattr(bridge, "_invoke", invoke)
    with pytest.raises(BridgeError, match="captured no image"):
        bridge.render_view(str(source))


def test_mismatched_capture_sizes_are_refused(tmp_path, monkeypatch):
    source = tmp_path / "document.FCStd"
    source.write_bytes(b"original")
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_invoke", _native(_geometry(80, 40), checker(80, 20)))
    with pytest.raises(RenderVerificationError) as caught:
        bridge.render_view(str(source))
    assert caught.value.error_code == "degenerate_render"
    assert "not comparable" in str(caught.value)


# --- Native host behaviour --------------------------------------------------


class _FakePresentation:
    """The native presentation surface, recorded instead of performed."""

    DEFAULT_RENDER_WIDTH = presentation.DEFAULT_RENDER_WIDTH
    DEFAULT_RENDER_HEIGHT = presentation.DEFAULT_RENDER_HEIGHT
    VIEW_ROTATIONS = presentation.VIEW_ROTATIONS
    VIEWS = presentation.VIEWS

    def __init__(self, names=("A",), visible=("A",)):
        self.calls = []
        self.names = list(names)
        self.restored_with = None
        self.state = {
            "camera": "camera-before",
            "camera_type": "Orthographic",
            "visibility": {"A": True, "B": False},
            "selection": ["A"],
        }
        # None means "the restore worked"; a test that wants a broken
        # restore sets it explicitly.
        self.final_state = None
        self.snapshot = {
            "visible_objects": sorted(visible),
            "camera_type": "Orthographic",
            "camera_orientation": presentation.VIEW_ROTATIONS["isometric"],
            "camera": {"height": [1.0]},
            "appearances": [],
        }
        self.validate_render_size = presentation.validate_render_size

    def validate_options(self, names, view, appearances=None, frame_margin=None):
        self.calls.append(("validate_options", list(names), view))
        return []

    def initialize(self):
        self.calls.append(("initialize",))
        return SimpleNamespace(
            getDocument=lambda name: SimpleNamespace(activeView=lambda: "active-view")
        )

    def apply(self, doc, gui, names, view, appearances=None, frame_margin=None):
        self.calls.append(("apply", list(names), view))
        return self.snapshot

    def requested_matches(self, names, view, actual, appearances=None):
        return True

    def renderable_names(self, doc):
        self.calls.append(("renderable_names",))
        return list(self.names)

    def hide_all(self, doc):
        self.calls.append(("hide_all",))
        for obj in doc.Objects:
            obj.ViewObject.Visibility = False

    def capture(self, view, path, width, height):
        self.calls.append(("capture", str(path), width, height))
        Path(path).write_bytes(solid(8, 8, 3, 3, 3))

    def view_state(self, doc, gui):
        if self.restored_with is None:
            # Recorded from the document, so the snapshot really is the state
            # the render found rather than a constant that looks right.
            self.state = dict(
                self.state,
                visibility={obj.Name: bool(obj.ViewObject.Visibility) for obj in doc.Objects},
            )
            return dict(self.state)
        return dict(self.final_state) if self.final_state is not None else dict(self.state)

    def restore_view_state(self, doc, gui, state):
        self.calls.append(("restore_view_state",))
        self.restored_with = dict(state)
        for obj in doc.Objects:
            if obj.Name in state.get("visibility", {}):
                obj.ViewObject.Visibility = state["visibility"][obj.Name]

    def states_match(self, expected, actual):
        return expected == actual


def _driver_call(tmp_path, monkeypatch, params, fake, contract=None):
    document = SimpleNamespace(
        Name="Doc",
        Objects=[
            SimpleNamespace(Name="A", ViewObject=SimpleNamespace(Visibility=True)),
            SimpleNamespace(Name="B", ViewObject=SimpleNamespace(Visibility=True)),
        ],
        recompute=lambda: None,
    )
    saved = []
    document.saveAs = lambda path: saved.append(path)
    document.save = lambda: saved.append("save")
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace())
    monkeypatch.setattr(freecad_driver, "_host_version", lambda: "1.1.4")
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    monkeypatch.setattr(freecad_driver, "_open_document", lambda app, path: document)
    monkeypatch.setattr(freecad_driver, "_close_document", lambda app, doc: None)
    contract = contract or write_contract
    monkeypatch.setattr(
        freecad_driver,
        "_load_sibling_module",
        lambda filename, module_name: fake if filename == "presentation.py" else contract,
    )
    result = freecad_driver.document_render_view(params)
    return result, document, saved


def test_the_host_captures_the_subject_then_the_empty_scene(tmp_path, monkeypatch):
    fake = _FakePresentation(names=["A", "B"], visible=["A", "B"])
    subject = tmp_path / "subject.png"
    baseline = tmp_path / "baseline.png"
    result, _document, saved = _driver_call(
        tmp_path,
        monkeypatch,
        {
            "document_path": str(tmp_path / "document.FCStd"),
            "image_path": str(subject),
            "baseline_path": str(baseline),
            "view": "front",
            "width": 640,
            "height": 360,
            "visible_objects": ["A", "B"],
        },
        fake,
    )
    kinds = [call[0] for call in fake.calls]
    assert kinds == [
        "initialize",
        "validate_options",
        "apply",
        "capture",
        "hide_all",
        "capture",
        "restore_view_state",
    ]
    assert fake.calls[3][1] == str(subject)
    assert fake.calls[5][1] == str(baseline)
    assert fake.calls[3][2] == 640 and fake.calls[3][3] == 360
    assert result["view"] == "front"
    assert result["visible_objects"] == ["A", "B"]
    assert "render.presentation_request" in result["verified"]
    # The document is never saved: a render must not touch the caller's file.
    assert saved == []


def test_the_view_state_is_restored_and_that_restoration_is_a_check(tmp_path, monkeypatch):
    """A tool that only looks must not leave the view somewhere else."""
    fake = _FakePresentation(names=["A", "B"], visible=["A", "B"])
    result, document, _saved = _driver_call(
        tmp_path,
        monkeypatch,
        {
            "document_path": str(tmp_path / "document.FCStd"),
            "image_path": str(tmp_path / "s.png"),
            "baseline_path": str(tmp_path / "b.png"),
        },
        fake,
    )
    assert "restore_view_state" in [call[0] for call in fake.calls]
    assert "render.view_state_restored" in result["verified"]
    # Restored with the state read before anything was touched, not the state
    # the render left behind.
    assert fake.restored_with == fake.state
    assert fake.restored_with["visibility"] == {"A": True, "B": True}
    # Both snapshots are returned so the claim is visible and assertable.
    assert result["view_state_before"] == result["view_state_after"]
    assert result["view_state_after"] == fake.state
    # Visibility was changed for the capture and put back afterwards.
    assert [obj.ViewObject.Visibility for obj in document.Objects] == [True, True]


def test_a_view_state_that_does_not_come_back_is_refused(tmp_path, monkeypatch):
    fake = _FakePresentation(names=["A", "B"], visible=["A", "B"])
    fake.final_state = {
        "camera": "camera-after",
        "camera_type": "Perspective",
        "visibility": {"A": False, "B": False},
        "selection": [],
    }
    with pytest.raises(write_contract.WriteVerificationError) as caught:
        _driver_call(
            tmp_path,
            monkeypatch,
            {
                "document_path": str(tmp_path / "document.FCStd"),
                "image_path": str(tmp_path / "s.png"),
                "baseline_path": str(tmp_path / "b.png"),
            },
            fake,
        )
    assert caught.value.payload["check"] == "render.view_state_restored"


def test_the_view_state_is_restored_even_when_the_capture_fails(tmp_path, monkeypatch):
    def exploding_capture(view, path, width, height):
        raise RuntimeError("offscreen rendering failed")

    fake = _FakePresentation(names=["A"], visible=["A"])
    fake.capture = exploding_capture
    with pytest.raises(RuntimeError, match="offscreen rendering failed"):
        _driver_call(
            tmp_path,
            monkeypatch,
            {
                "document_path": str(tmp_path / "document.FCStd"),
                "image_path": str(tmp_path / "s.png"),
                "baseline_path": str(tmp_path / "b.png"),
            },
            fake,
        )
    assert "restore_view_state" in [call[0] for call in fake.calls]


def test_the_default_selection_is_every_top_level_object(tmp_path, monkeypatch):
    fake = _FakePresentation(names=["A", "B"], visible=["A", "B"])
    _result, document, _saved = _driver_call(
        tmp_path,
        monkeypatch,
        {
            "document_path": str(tmp_path / "document.FCStd"),
            "image_path": str(tmp_path / "s.png"),
            "baseline_path": str(tmp_path / "b.png"),
        },
        fake,
    )
    assert "renderable_names" in [call[0] for call in fake.calls]
    assert all(obj.ViewObject.Visibility for obj in document.Objects)


def test_a_document_with_nothing_renderable_is_refused(tmp_path, monkeypatch):
    fake = _FakePresentation(names=[], visible=[])
    with pytest.raises(ValueError, match="no top-level non-container object"):
        _driver_call(
            tmp_path,
            monkeypatch,
            {
                "document_path": str(tmp_path / "document.FCStd"),
                "image_path": str(tmp_path / "s.png"),
                "baseline_path": str(tmp_path / "b.png"),
            },
            fake,
        )


def test_appearances_without_a_selection_are_refused_by_the_host(tmp_path, monkeypatch):
    fake = _FakePresentation()
    with pytest.raises(ValueError, match="explicit visible_objects"):
        _driver_call(
            tmp_path,
            monkeypatch,
            {
                "document_path": str(tmp_path / "document.FCStd"),
                "image_path": str(tmp_path / "s.png"),
                "baseline_path": str(tmp_path / "b.png"),
                "appearances": [{"object_name": "A", "rgb": [0, 0, 0], "opacity": 1}],
            },
            fake,
        )


def test_a_request_the_native_state_does_not_match_is_refused_before_capture(tmp_path, monkeypatch):
    fake = _FakePresentation(names=["A", "B"], visible=["A", "B"])
    fake.requested_matches = lambda *args: False
    with pytest.raises(write_contract.WriteVerificationError) as caught:
        _driver_call(
            tmp_path,
            monkeypatch,
            {
                "document_path": str(tmp_path / "document.FCStd"),
                "image_path": str(tmp_path / "s.png"),
                "baseline_path": str(tmp_path / "b.png"),
                "visible_objects": ["A", "B"],
            },
            fake,
        )
    assert caught.value.payload["check"] == "render.presentation_request"
    assert "capture" not in [call[0] for call in fake.calls]


# --- Capability reporting ---------------------------------------------------


def test_a_host_without_the_gui_library_is_marked_limited():
    report = FreecadBridge._render_capability(
        {
            "ready": True,
            "gui_library": {
                "importable": False,
                "error": "ImportError: No module named 'FreeCADGui'",
            },
        }
    )
    assert report["status"] == "host_limited"
    assert report["host_gui_importable"] is False
    assert "FreeCADGui" in report["remediation"]
    assert "No module named" in report["remediation"]
    assert report["image_included_by_default"] is False
    assert report["waste_image_detection"] is True


def test_a_host_with_no_backend_at_all_is_marked_limited():
    report = FreecadBridge._render_capability({"ready": False})
    assert report["status"] == "host_limited"
    assert "DCC_MCP_FREECAD_EXECUTABLE" in report["remediation"]


def test_a_host_with_the_gui_library_is_reported_available():
    report = FreecadBridge._render_capability(
        {"ready": True, "gui_library": {"importable": True, "error": None}}
    )
    assert report["status"] == "available"
    assert report["remediation"] is None
    assert report["views"] == ["front", "isometric", "right", "top"]


def test_capabilities_expose_render_view_and_its_limits(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(
        bridge,
        "status",
        lambda: {"ready": True, "gui_library": {"importable": True, "error": None}},
    )
    capabilities = bridge.capabilities()
    assert "render_view" in capabilities["methods"]
    assert capabilities["render_view"]["status"] == "available"
    assert capabilities["render_view"]["max_width"] == presentation.MAX_RENDER_WIDTH
