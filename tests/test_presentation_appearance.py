"""Synthetic contract tests only; these never initialize a native host."""

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from dcc_mcp_core import validate_skill
from jsonschema import Draft7Validator

from dcc_mcp_freecad import freecad_driver, presentation, write_contract
from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge


@pytest.fixture(autouse=True)
def mock_coin_import(monkeypatch):
    """Fake only the fixed native dependency; the source guard blocks real Pivy."""
    calls = []

    def imported(name):
        assert name == "pivy.coin"
        calls.append(name)
        return SimpleNamespace()

    monkeypatch.setattr(presentation, "import_module", imported, raising=False)
    return calls


def appearance(name="A", opacity=0.427):
    return {"object_name": name, "rgb": [0.2, 0.4, 0.6], "opacity": opacity}


class Provider:
    def __init__(self):
        self.Visibility = True
        self.ShapeAppearance = [SimpleNamespace(DiffuseColor=(0.5, 0.5, 0.5, 1), Transparency=0.0)]
        self.writes = []
        self.drop = None

    @property
    def ShapeColor(self):
        return self.ShapeAppearance[0].DiffuseColor

    @ShapeColor.setter
    def ShapeColor(self, value):
        self.writes.append("color")
        if self.drop != "color":
            for material in self.ShapeAppearance:
                material.DiffuseColor = tuple(value) + (1.0,)

    @property
    def Transparency(self):
        return int(round(self.ShapeAppearance[0].Transparency * 100))

    @Transparency.setter
    def Transparency(self, value):
        self.writes.append("opacity")
        if self.drop != "opacity":
            for material in self.ShapeAppearance:
                material.Transparency = value / 100.0


class Height:
    def __init__(self):
        self.value = 100.0
        self.drop = False

    def getValue(self):
        return self.value

    def setValue(self, value):
        if not self.drop:
            self.value = value


class View:
    def __init__(self):
        self.height = Height()
        self.camera_type = "Orthographic"
        self.preset = "top"

    def setAnimationEnabled(self, enabled):
        assert enabled is False

    def setCameraType(self, value):
        self.camera_type = value

    def viewTop(self):
        self.preset = "top"

    def fitAll(self):
        self.height.value = 100.0

    def getCameraNode(self):
        return self

    def getCameraType(self):
        return self.camera_type

    def getCameraOrientation(self):
        return SimpleNamespace(Q=presentation.VIEW_ROTATIONS[self.preset])

    def getCamera(self):
        return (
            "position 0 0 100\nnearDistance 1\nfarDistance 1000\n"
            "aspectRatio 1\nfocalDistance 100\nheight " + str(self.height.value)
        )


def scene():
    objects = [
        SimpleNamespace(
            Name=name,
            ViewObject=Provider(),
            isDerivedFrom=lambda value: value == "Part::Feature",
            Shape=SimpleNamespace(isNull=lambda: False),
        )
        for name in ("A", "B")
    ]
    document = SimpleNamespace(Name="Document", Objects=objects, recompute=lambda: None)
    view = View()
    gui = SimpleNamespace(getDocument=lambda name: SimpleNamespace(activeView=lambda: view))
    return document, gui, view


def test_coin_wrappers_load_before_provider_mutation_and_camera_pointer(monkeypatch):
    doc, gui, view = scene()
    events = []

    def imported(name):
        assert name == "pivy.coin"
        assert all(obj.ViewObject.writes == [] and obj.ViewObject.Visibility for obj in doc.Objects)
        events.append("coin_import")
        return SimpleNamespace()

    def camera_node():
        if not events:
            raise RuntimeError("No SWIG wrapped library loaded")
        assert events == ["coin_import"]
        events.append("camera_node")
        return view

    monkeypatch.setattr(presentation, "import_module", imported, raising=False)
    monkeypatch.setattr(view, "getCameraNode", camera_node)
    result = presentation.apply(doc, gui, ["A"], "top", [appearance()], 0.12)
    assert events == ["coin_import", "camera_node"]
    assert result["camera"]["height"] == [124.0]


@pytest.mark.parametrize("failure", [ImportError, RuntimeError])
def test_coin_load_failure_precedes_all_provider_mutations(monkeypatch, failure):
    doc, gui, view = scene()

    def fail(name):
        assert name == "pivy.coin"
        raise failure("Synthetic Pivy dependency failure")

    monkeypatch.setattr(presentation, "import_module", fail)
    with pytest.raises(failure, match="Pivy dependency"):
        presentation.apply(doc, gui, ["A"], "top", [appearance()], 0.12)
    assert all(obj.ViewObject.writes == [] and obj.ViewObject.Visibility for obj in doc.Objects)
    assert view.height.value == 100.0


def test_plain_presentation_does_not_require_coin_import(mock_coin_import):
    doc, gui, view = scene()
    result = presentation.apply(doc, gui, ["A"], "top", [appearance()])
    assert mock_coin_import == []
    assert result["camera"]["height"] == [100.0]


def test_invalid_margin_is_rejected_before_coin_import(mock_coin_import):
    doc, gui, view = scene()
    with pytest.raises(ValueError):
        presentation.apply(doc, gui, ["A"], "top", [appearance()], float("nan"))
    assert mock_coin_import == []


@pytest.mark.parametrize(
    "value", [True, False, "0.1", None, float("nan"), float("inf"), -0.01, 1.01]
)
@pytest.mark.parametrize("field", ["rgb", "opacity", "frame_margin"])
def test_invalid_numbers_rejected_before_any_provider_changes(value, field):
    doc, gui, _view = scene()
    item = appearance()
    margin = 0.1
    if field == "rgb":
        item["rgb"][1] = value
    elif field == "opacity":
        item["opacity"] = value
    else:
        if value is None:  # Omitted optional framing retains legacy fitAll.
            return
        margin = value
    with pytest.raises(ValueError):
        presentation.apply(doc, gui, ["A"], "top", [item], margin)
    assert all(obj.ViewObject.Visibility and obj.ViewObject.writes == [] for obj in doc.Objects)


@pytest.mark.parametrize(
    "items",
    [
        [],
        {},
        [appearance(), appearance()],
        [appearance("Missing")],
        [{**appearance(), "python": "pass"}],
        [{"object_name": "A", "rgb": [0, 0, 0]}],
        [{**appearance(), "rgb": [0, 0]}],
        [{**appearance(), "rgb": [0, 0, 0, 1]}],
        [{**appearance(), "object_name": []}],
        [appearance()] * 1001,
    ],
)
def test_invalid_appearance_structure_rejected(items):
    with pytest.raises(ValueError):
        presentation.validate_options(["A"], "top", items, 0.1)


@pytest.mark.parametrize("failure", ["missing", "no_provider", "non_part", "null", "mixed_faces"])
def test_all_objects_prevalidated_before_any_mutation(failure):
    doc, gui, _view = scene()
    target = doc.Objects[1]
    if failure == "missing":
        doc.Objects.pop()
    elif failure == "no_provider":
        target.ViewObject = None
    elif failure == "non_part":
        target.isDerivedFrom = lambda value: False
    elif failure == "null":
        target.Shape.isNull = lambda: True
    else:
        target.ViewObject.ShapeAppearance.append(
            SimpleNamespace(DiffuseColor=(1.0, 0.0, 0.0, 1.0), Transparency=0)
        )
    with pytest.raises(ValueError):
        presentation.apply(doc, gui, ["A", "B"], "top", [appearance(), appearance("B")])
    assert doc.Objects[0].ViewObject.writes == []
    assert doc.Objects[0].ViewObject.Visibility is True


@pytest.mark.parametrize(
    "opacity,applied", [(0, 0), (1, 1), (0.427, 0.43), (0.125, 0.12), (0.555, 0.55)]
)
@pytest.mark.parametrize("margin,height", [(0, 100), (0.1, 120), (1, 300)])
def test_appearance_and_camera_effect_readback(opacity, applied, margin, height):
    doc, gui, _view = scene()
    result = presentation.apply(doc, gui, ["A"], "top", [appearance(opacity=opacity)], margin)
    assert result["appearances"][0]["opacity"] == pytest.approx(applied)
    assert result["appearances"][0]["rgb"] == [0.2, 0.4, 0.6]
    assert result["camera"]["height"] == [height]
    assert result["visible_objects"] == ["A"]
    assert doc.Objects[1].ViewObject.writes == []
    assert presentation.requested_matches(["A"], "top", result, [appearance(opacity=opacity)])


@pytest.mark.parametrize("dropped", ["color", "opacity", "frame"])
def test_silent_setter_failure_cannot_report_success(dropped):
    doc, gui, view = scene()
    doc.Objects[0].ViewObject.drop = dropped
    view.height.drop = dropped == "frame"
    with pytest.raises(RuntimeError, match="not applied"):
        presentation.apply(doc, gui, ["A"], "top", [appearance()], 0.1)


def test_serialized_camera_has_independent_margin_readback():
    doc, gui, view = scene()
    original = view.getCamera
    view.getCamera = lambda: original().replace("height 120.0", "height 100.0")
    with pytest.raises(RuntimeError, match="serialized camera"):
        presentation.apply(doc, gui, ["A"], "top", [appearance()], 0.1)


@pytest.mark.parametrize("changed", ["rgb", "opacity", "name", "height"])
def test_changed_persistent_values_do_not_match(changed):
    doc, gui, _view = scene()
    expected = presentation.apply(doc, gui, ["A"], "top", [appearance()], 0.1)
    actual = deepcopy(expected)
    if changed == "rgb":
        actual["appearances"][0]["rgb"][1] += 0.01
    elif changed == "opacity":
        actual["appearances"][0]["opacity"] += 0.01
    elif changed == "name":
        actual["appearances"][0]["object_name"] = "B"
    else:
        actual["camera"]["height"][0] += 1
    assert not presentation.matches(expected, actual)


@pytest.mark.parametrize(
    "options",
    [
        {"appearances": [appearance()]},
        {"frame_margin": 0},
        {"visible_objects": ["A"], "appearances": [appearance(), appearance()]},
        {"visible_objects": ["A"], "frame_margin": float("nan")},
    ],
)
def test_invalid_bridge_request_never_starts_native_or_creates_stage(
    tmp_path, monkeypatch, options
):
    source = tmp_path / "source.FCStd"
    source.write_bytes(b"source")
    bridge = FreecadBridge(allowed_roots=[tmp_path])
    monkeypatch.setattr(bridge, "_invoke", lambda *args: pytest.fail("Native call must not start"))
    with pytest.raises((BridgeError, ValueError)):
        bridge.save_copy(str(source), str(tmp_path / "copy.FCStd"), **options)
    assert source.read_bytes() == b"source"
    assert [p.name for p in tmp_path.iterdir()] == ["source.FCStd"]


@pytest.mark.parametrize("changed", [None, "rgb", "opacity", "height", "geometry"])
def test_driver_reopen_mismatch_cannot_publish(tmp_path, monkeypatch, changed):
    source = tmp_path / "source.FCStd"
    source.write_bytes(b"source")
    doc, gui, view = scene()
    doc.saveAs = lambda path: Path(path).write_bytes(b"synthetic-stage")
    geometry = ["A", "B"]

    def reopen(path):
        assert Path(path).read_bytes() == b"synthetic-stage"
        if changed == "rgb":
            doc.Objects[0].ViewObject.ShapeColor = (0.1, 0.1, 0.1)
        elif changed == "opacity":
            doc.Objects[0].ViewObject.Transparency = 0
        elif changed == "height":
            view.height.value = 100
        elif changed == "geometry":
            geometry.append("Unexpected")
        return doc

    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(openDocument=reopen))
    monkeypatch.setitem(
        freecad_driver._SIBLING_MODULES, "dcc_mcp_freecad_presentation", presentation
    )
    monkeypatch.setitem(
        freecad_driver._SIBLING_MODULES, "dcc_mcp_freecad_write_contract", write_contract
    )
    monkeypatch.setattr(presentation, "initialize", lambda: gui)
    monkeypatch.setattr(freecad_driver, "_host_version", lambda: "1.0.0")
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    monkeypatch.setattr(freecad_driver, "_open_document", lambda app, path: doc)
    monkeypatch.setattr(freecad_driver, "_close_document", lambda app, obj: None)
    monkeypatch.setattr(freecad_driver, "_presentation_geometry", lambda obj: list(geometry))
    bridge = FreecadBridge(allowed_roots=[tmp_path])
    monkeypatch.setattr(
        bridge, "_invoke", lambda method, params, timeout: freecad_driver.document_save_copy(params)
    )
    requested = [{**appearance(), "rgb": [0.9, 0.7, 0.12345]}]
    if changed is None:
        result = bridge.save_copy(
            str(source),
            str(tmp_path / "copy.FCStd"),
            visible_objects=["A"],
            view="top",
            appearances=requested,
            frame_margin=0.1,
        )
        assert result["presentation_request"]["appearances"] == requested
        assert result["presentation"]["appearances"][0]["rgb"] == [230 / 255, 179 / 255, 31 / 255]
        assert result["presentation"]["appearances"][0]["opacity"] == pytest.approx(0.43)
        assert source.read_bytes() == b"source"
        return
    with pytest.raises(write_contract.WriteVerificationError) as error:
        bridge.save_copy(
            str(source),
            str(tmp_path / "copy.FCStd"),
            visible_objects=["A"],
            view="top",
            appearances=[appearance()],
            frame_margin=0.1,
        )
    assert error.value.payload["check"] == (
        "copy.geometry" if changed == "geometry" else "copy.presentation"
    )
    assert source.read_bytes() == b"source"
    assert [p.name for p in tmp_path.iterdir()] == ["source.FCStd"]


def test_schema_and_skill_validate_closed_appearance_contract():
    skill = Path(__file__).parents[1] / "src/dcc_mcp_freecad/skills/freecad-session"
    report = validate_skill(str(skill))
    assert [item.message for item in report.issues if item.severity == "error"] == []
    tools = yaml.safe_load((skill / "tools.yaml").read_text())["tools"]
    schema = next(tool for tool in tools if tool["name"] == "save_copy")["input_schema"]
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    valid = {
        "source_path": "source.FCStd",
        "output_path": "copy.FCStd",
        "visible_objects": ["A"],
        "appearances": [appearance()],
        "frame_margin": 0.1,
    }
    assert validator.is_valid(valid)
    invalid = deepcopy(valid)
    invalid.pop("visible_objects")
    assert not validator.is_valid(invalid)
    invalid = deepcopy(valid)
    invalid["appearances"][0]["python"] = "pass"
    assert not validator.is_valid(invalid)


@pytest.mark.parametrize(
    "value,byte", [(0, 0), (1, 255), (0.5, 128), (0.9, 230), (0.7, 179), (0.12345, 31)]
)
def test_rgb_uses_native_8bit_rounding_before_save(value, byte):
    doc, gui, _view = scene()
    item = {**appearance(), "rgb": [value] * 3}
    actual = presentation.apply(doc, gui, ["A"], "top", [item])
    assert actual["appearances"][0]["rgb"] == [byte / 255] * 3
    assert presentation.requested_matches(["A"], "top", actual, [item])


def test_every_8bit_component_is_stable_through_repeated_normalization():
    for byte in range(256):
        assert presentation._stored_rgb(byte / 255) == byte / 255


def test_rounded_rgb_survives_simulated_native_storage_without_looser_tolerance():
    doc, gui, _view = scene()
    requested = [{**appearance(), "rgb": [0.9, 0.7, 0.12345]}]
    before = presentation.apply(doc, gui, ["A"], "top", requested)
    after = deepcopy(before)
    after["appearances"][0]["rgb"] = [230 / 255, 179 / 255, 31 / 255]
    assert presentation.matches(before, after)
    after["appearances"][0]["rgb"][0] = 229 / 255
    assert not presentation.matches(before, after)


@pytest.mark.parametrize(
    "value,byte",
    [
        (0.49999998, 127),
        (0.499999999, 128),
        (0.5, 128),
        (0.50000001, 128),
        (0.0019607, 0),
        (0.5 / 255, 1),
        (0.0019608, 1),
        (1 - 1e-9, 255),
    ],
)
def test_rgb_half_steps_follow_float32_then_lround(value, byte):
    assert presentation._stored_rgb(value) == byte / 255
