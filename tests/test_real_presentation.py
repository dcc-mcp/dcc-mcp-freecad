"""Real GUI state qualification on the SHA-verified, pinned AppImage hosts.

Selecting these tests requires a host; unavailable GUI never becomes a skip.
All documents are synthetic. This suite makes no rendered-image assertion.
"""

import hashlib
import math
import os
import shutil
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge, WriteVerificationError

pytestmark = [pytest.mark.freecad, pytest.mark.freecad_gui]
HOST_SCRIPT = Path(__file__).parent / "native/presentation_host.py"
PRESETS = {
    "isometric": (0.424708, 0.17592, 0.339851, 0.820473),
    "front": (math.sqrt(0.5), 0, 0, math.sqrt(0.5)),
    "top": (0, 0, 0, 1),
    "right": (0.5, 0.5, 0.5, 0.5),
}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(directory):
    return {
        path.relative_to(directory).as_posix(): _sha(path)
        for path in directory.rglob("*")
        if path.is_file()
    }


def _bridge(executable, roots, fixture=False):
    bridge = FreecadBridge(executable, allowed_roots=roots)
    if fixture:
        bridge.driver_path = HOST_SCRIPT.resolve()
    return bridge


def _snapshot(executable, source, root):
    return _bridge(executable, [root], fixture=True)._invoke(
        "fixture.inspect", {"document_path": str(source)}
    )


def _assert_numbers(actual, expected, tolerance=1e-9):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key, value in expected.items():
            _assert_numbers(actual[key], value, tolerance)
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for value, reference in zip(actual, expected):
            _assert_numbers(value, reference, tolerance)
    elif isinstance(expected, float):
        assert math.isfinite(actual)
        assert actual == pytest.approx(expected, rel=tolerance, abs=tolerance)
    else:
        assert actual == expected


def _assert_quaternion(actual, expected):
    assert len(actual) == len(expected) == 4
    assert all(math.isfinite(value) for value in actual)
    first = math.sqrt(sum(value * value for value in actual))
    second = math.sqrt(sum(value * value for value in expected))
    assert first > 0 and second > 0
    normalized = [value / first for value in actual]
    reference = [value / second for value in expected]
    sign = 1 if sum(a * b for a, b in zip(normalized, reference)) >= 0 else -1
    assert normalized == pytest.approx([sign * value for value in reference], abs=1e-6, rel=0)


def _assert_presentation(snapshot, view, visible):
    assert snapshot["host"]["gui_up"] is True
    assert snapshot["host"]["qt_platform"] == "offscreen"
    assert snapshot["camera_type"] == "Orthographic"
    assert snapshot["visible_objects"] == sorted(visible)
    assert snapshot["visibility"] == {
        item["name"]: item["name"] in visible for item in snapshot["objects"]
    }
    _assert_quaternion(snapshot["camera_orientation"], PRESETS[view])
    camera = snapshot["camera"]
    required = {
        "position",
        "nearDistance",
        "farDistance",
        "aspectRatio",
        "focalDistance",
        "height",
    }
    assert required <= set(camera) <= required | {"orientation"}
    if view != "top":
        assert "orientation" in camera
    assert camera["height"][0] > 0
    assert camera["aspectRatio"][0] > 0
    assert camera["focalDistance"][0] > 0
    assert camera["farDistance"][0] > camera["nearDistance"][0]
    assert all(math.isfinite(number) for values in camera.values() for number in values)


@pytest.fixture(scope="module")
def real_host():
    executable = os.environ.get("FREECAD_TEST_EXECUTABLE", "")
    expected = os.environ.get("FREECAD_REAL_VERSION", "")
    assert executable and Path(executable).is_file(), "Selected GUI tests require real FreeCADCmd"
    assert expected in ("1.0.2", "1.1.4"), "GUI qualification requires the exact pinned host"
    assert os.environ.get("QT_QPA_PLATFORM", "offscreen") == "offscreen"
    return executable, expected


@pytest.fixture(scope="module")
def native_source(real_host, tmp_path_factory):
    executable, version = real_host
    root = tmp_path_factory.mktemp("native-presentation-source")
    source = root / "source.FCStd"
    bridge = _bridge(executable, [root])
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
    before = _bridge(executable, [root], fixture=True)._invoke(
        "fixture.prepare", {"document_path": str(source)}
    )
    assert before["host"]["version"] == version
    assert before["host"]["python_version"][:2] == [3, 11]
    _assert_presentation(before, "front", ["PortCut"])
    objects = {item["name"]: item for item in before["objects"]}
    assert set(objects) == {"Body", "PortCut", "BodyWithPort"}
    assert objects["BodyWithPort"]["outgoing_links"] == ["Body", "PortCut"]
    assert objects["Body"]["incoming_links"] == ["BodyWithPort"]
    assert objects["Body"]["shape"]["volume"] == pytest.approx(192)
    assert 0 < objects["BodyWithPort"]["shape"]["volume"] < 192
    assert objects["Body"]["placement"]["translation"] == [1.0, 2.0, 3.0]
    assert all(item["shape"]["valid"] for item in objects.values())
    return source, source.read_bytes(), _sha(source), before


def _unchanged_source(source_state):
    source, original, digest, _snapshot_before = source_state
    assert source.read_bytes() == original
    assert _sha(source) == digest


@pytest.mark.parametrize("view", list(PRESETS))
def test_real_gui_public_copy_relocated_reopen(
    real_host, native_source, tmp_path, view, record_property
):
    executable, version = real_host
    source, _original, _digest, before = native_source
    target = tmp_path / "published.FCStd"
    result = _bridge(executable, [source.parent, tmp_path]).save_copy(
        str(source),
        str(target),
        visible_objects=["BodyWithPort"],
        view=view,
    )
    assert {
        "copy.presentation_request",
        "artifact.non_empty",
        "copy.objects",
        "copy.presentation",
        "copy.geometry",
    } <= set(result["verified"])
    assert result["object_names"] == ["Body", "BodyWithPort", "PortCut"]
    assert result["object_count"] == 3
    assert result["bytes"] == target.stat().st_size > 0
    assert result["sha256"] == _sha(target)
    assert result["presentation"]["visible_objects"] == ["BodyWithPort"]
    assert result["presentation"]["camera_type"] == "Orthographic"
    _assert_quaternion(result["presentation"]["camera_orientation"], PRESETS[view])
    _unchanged_source(native_source)

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    delivered = relocated / "delivered.FCStd"
    shutil.copy2(str(target), str(delivered))
    assert _sha(delivered) == result["sha256"]
    target.unlink()
    # This is a separate Cmd process opening only the delivered native file.
    after = _snapshot(executable, delivered, tmp_path)
    assert after["host"]["version"] == version
    assert after["host"]["python_version"][:2] == [3, 11]
    _assert_presentation(after, view, ["BodyWithPort"])
    _assert_numbers(after["objects"], before["objects"])
    _assert_numbers(after["aggregate"], before["aggregate"])
    # Axis-angle text can change representation without changing rotation;
    # the independent native quaternion comparison below remains mandatory.
    camera = {key: value for key, value in after["camera"].items() if key != "orientation"}
    expected_camera = {
        key: value
        for key, value in result["presentation"]["camera"].items()
        if key != "orientation"
    }
    _assert_numbers(camera, expected_camera, tolerance=1e-6)
    _assert_quaternion(after["camera_orientation"], result["presentation"]["camera_orientation"])
    assert _sha(delivered) == result["sha256"]
    assert set(_files(tmp_path)) == {"relocated/delivered.FCStd"}
    _unchanged_source(native_source)
    record_property("native_gui_version", after["host"]["version"])
    record_property("native_gui_python", ".".join(map(str, after["host"]["python_version"])))
    record_property("native_qt_platform", after["host"]["qt_platform"])


@pytest.mark.parametrize("existing", [False, True])
def test_real_gui_unknown_object_preserves_source_and_output(
    real_host,
    native_source,
    tmp_path,
    existing,
):
    executable, _version = real_host
    source = native_source[0]
    target = tmp_path / "published.FCStd"
    if existing:
        target.write_bytes(b"existing synthetic destination")
    before = _files(tmp_path)
    with pytest.raises(BridgeError, match="does not exist"):
        _bridge(executable, [source.parent, tmp_path]).save_copy(
            str(source),
            str(target),
            overwrite=existing,
            visible_objects=["Missing"],
            view="front",
        )
    assert _files(tmp_path) == before
    _unchanged_source(native_source)


@pytest.mark.parametrize("fault", ["visibility", "camera"])
@pytest.mark.parametrize("existing", [False, True])
def test_real_gui_post_apply_fault_reopens_and_refuses_publication(
    real_host,
    native_source,
    tmp_path,
    monkeypatch,
    fault,
    existing,
):
    executable, version = real_host
    source = native_source[0]
    target = tmp_path / "published.FCStd"
    if existing:
        target.write_bytes(b"existing synthetic destination")
    before = _files(tmp_path)
    monkeypatch.setenv("DCC_MCP_FREECAD_TEST_PRESENTATION_FAULT", fault)
    bridge = _bridge(executable, [source.parent, tmp_path], fixture=True)
    with pytest.raises(WriteVerificationError) as caught:
        bridge.save_copy(
            str(source),
            str(target),
            overwrite=existing,
            visible_objects=["BodyWithPort"],
            view="front",
        )
    error = caught.value
    assert error.tool == "document.save_copy"
    assert error.check == "copy.presentation"
    assert error.host_version == version
    assert error.expected["visible_objects"] == ["BodyWithPort"]
    _assert_quaternion(error.expected["camera_orientation"], PRESETS["front"])
    assert error.actual["camera_type"] == "Orthographic"
    if fault == "visibility":
        assert error.actual["visible_objects"] == []
    else:
        assert error.actual["visible_objects"] == ["BodyWithPort"]
        _assert_quaternion(error.actual["camera_orientation"], PRESETS["top"])
    assert _files(tmp_path) == before
    _unchanged_source(native_source)


@pytest.mark.parametrize(
    "type_id", ["App::DocumentObjectGroup", "App::Part", "PartDesign::Body", "App::LinkGroup"]
)
@pytest.mark.parametrize("selected", [["Container"], ["GroupedLeaf"], ["Container", "GroupedLeaf"]])
def test_real_gui_container_or_grouped_leaf_is_rejected_before_save(
    real_host,
    native_source,
    tmp_path,
    type_id,
    selected,
):
    executable, _version = real_host
    source = tmp_path / "group-source.FCStd"
    shutil.copy2(str(native_source[0]), str(source))
    grouped = _bridge(executable, [tmp_path], fixture=True)._invoke(
        "fixture.container", {"document_path": str(source), "type_id": type_id}
    )
    objects = {item["name"]: item for item in grouped["objects"]}
    assert objects["Container"]["type_id"] == type_id
    if type_id == "App::LinkGroup":
        assert objects["Container"]["element_members"] == ["GroupedLeaf"]
        assert objects["Container"]["element_visibility"] == [False]
    else:
        assert objects["Container"]["group_members"] == ["GroupedLeaf"]
    assert "Container" in objects["GroupedLeaf"]["incoming_links"]
    target = tmp_path / "published.FCStd"
    target.write_bytes(b"existing synthetic destination")
    before = _files(tmp_path)
    with pytest.raises(BridgeError, match="requires top-level non-container objects") as caught:
        _bridge(executable, [tmp_path]).save_copy(
            str(source),
            str(target),
            overwrite=True,
            visible_objects=selected,
            view="front",
        )
    assert not isinstance(caught.value, WriteVerificationError)
    assert _files(tmp_path) == before
    _unchanged_source(native_source)


@pytest.mark.parametrize("alias", [False, True])
def test_real_gui_presentation_cannot_replace_native_source(
    real_host,
    native_source,
    tmp_path,
    alias,
):
    executable, _version = real_host
    source = native_source[0]
    target = source
    if alias:
        target = tmp_path / "source-alias.FCStd"
        target.symlink_to(source)
    before = _files(tmp_path)
    with pytest.raises(BridgeError, match="must not replace"):
        _bridge(executable, [source.parent, tmp_path]).save_copy(
            str(source),
            str(target),
            overwrite=True,
            visible_objects=["BodyWithPort"],
        )
    assert _files(tmp_path) == before
    _unchanged_source(native_source)


def test_real_gui_existing_native_destination_requires_overwrite(
    real_host,
    native_source,
    tmp_path,
):
    executable, _version = real_host
    source = native_source[0]
    target = tmp_path / "existing.FCStd"
    shutil.copy2(str(source), str(target))
    before = _files(tmp_path)
    with pytest.raises(BridgeError, match="already exists"):
        _bridge(executable, [source.parent, tmp_path]).save_copy(
            str(source),
            str(target),
            visible_objects=["BodyWithPort"],
            view="right",
        )
    assert _files(tmp_path) == before
    _unchanged_source(native_source)


def test_real_gui_view_requires_explicit_selection(real_host, native_source, tmp_path):
    executable, _version = real_host
    source = native_source[0]
    target = tmp_path / "published.FCStd"
    with pytest.raises(BridgeError, match="explicit visible_objects"):
        _bridge(executable, [source.parent, tmp_path]).save_copy(
            str(source),
            str(target),
            view="top",
        )
    assert _files(tmp_path) == {}
    _unchanged_source(native_source)
