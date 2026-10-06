import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcc_mcp_freecad import freecad_driver, presentation, write_contract
from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge


@pytest.mark.parametrize(
    "names", [[], ["A", "A"], [3], ["A" * 65], ["A"] * 1001, ["invalid name"], ["1Invalid"]]
)
def test_invalid_selection_is_rejected(names):
    with pytest.raises(ValueError):
        presentation.validate_selection(names, "isometric")


def test_unknown_object_does_not_change_existing_visibility():
    view = SimpleNamespace(Visibility=True)
    obj = SimpleNamespace(Name="Existing", ViewObject=view)
    document = SimpleNamespace(Objects=[obj])
    with pytest.raises(ValueError, match="does not exist"):
        presentation.apply(document, None, ["Missing"], "top")
    assert view.Visibility is True


def test_invalid_view_does_not_change_existing_visibility():
    view = SimpleNamespace(Visibility=True)
    document = SimpleNamespace(Objects=[SimpleNamespace(Name="A", ViewObject=view)])
    with pytest.raises(ValueError, match="view must"):
        presentation.apply(document, None, ["A"], "arbitrary")
    assert view.Visibility is True


def test_camera_comparison_allows_native_float_rounding_but_not_changes():
    first = {
        "visible_objects": ["A"],
        "camera_type": "Orthographic",
        "camera_orientation": [0, 0, 0, 1],
        "camera": {"height": [0.74290556], "orientation": [0, 0, 1, 0]},
    }
    second = deepcopy(first)
    second["camera"]["height"][0] += 0.00000041
    assert presentation.matches(first, second)
    second["camera"]["height"][0] += 0.001
    assert not presentation.matches(first, second)
    second = deepcopy(first)
    second["camera_orientation"] = [0, 0, 0, -1]
    second["camera"].pop("orientation")
    assert presentation.matches(first, second)
    second["camera_orientation"] = [0.5, 0.5, 0.5, 0.5]
    assert not presentation.matches(first, second)
    second = deepcopy(first)
    second["visible_objects"] = []
    assert not presentation.matches(first, second)


def test_geometry_comparison_keeps_topology_and_precision():
    first = {"solids": 1, "volume": 100.0, "links": ["A", "B"]}
    second = deepcopy(first)
    second["volume"] += 1e-12
    assert presentation.geometry_matches(first, second)
    second["volume"] += 1e-5
    assert not presentation.geometry_matches(first, second)
    second = deepcopy(first)
    second["solids"] = 2
    assert not presentation.geometry_matches(first, second)


def test_presentation_cannot_replace_source_even_with_overwrite(tmp_path):
    source = tmp_path / "source.FCStd"
    source.write_bytes(b"original")
    bridge = FreecadBridge(allowed_roots=[tmp_path])
    with pytest.raises(BridgeError, match="must not replace"):
        bridge.save_copy(str(source), str(source), overwrite=True, visible_objects=["A"])
    assert source.read_bytes() == b"original"


def test_cancel_after_native_staging_keeps_original_and_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.FCStd"
    target = tmp_path / "target.FCStd"
    source.write_bytes(b"source")
    target.write_bytes(b"existing-target")
    bridge = FreecadBridge(allowed_roots=[tmp_path])

    def native(_method, params, _timeout):
        from pathlib import Path

        Path(params["output_path"]).write_bytes(b"native-staged-copy")
        return {"object_count": 1}

    def cancelled():
        raise InterruptedError("cancelled before publication")

    monkeypatch.setattr(bridge, "_invoke", native)
    monkeypatch.setattr("dcc_mcp_freecad.bridge.check_dcc_cancelled", cancelled)
    with pytest.raises(InterruptedError, match="before publication"):
        bridge.save_copy(str(source), str(target), overwrite=True, visible_objects=["A"])
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"existing-target"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source.FCStd", "target.FCStd"]


@pytest.mark.parametrize("view", ["front", "top", "right"])
def test_nondefault_view_requires_selection_before_native_call(tmp_path, monkeypatch, view):
    source = tmp_path / "source.FCStd"
    source.write_bytes(b"original")
    bridge = FreecadBridge(allowed_roots=[tmp_path])
    monkeypatch.setattr(bridge, "_invoke", lambda *args: pytest.fail("Native call must not start"))
    with pytest.raises(BridgeError, match="explicit visible_objects"):
        bridge.save_copy(str(source), str(tmp_path / "copy.FCStd"), view=view)
    assert source.read_bytes() == b"original"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.FCStd"]


@pytest.mark.parametrize("members_property", ["Group", "ElementList"])
@pytest.mark.parametrize("selected", [["Leaf"], ["Container"], ["Container", "Leaf"]])
def test_container_profile_is_rejected_before_visibility_mutation(members_property, selected):
    leaf = SimpleNamespace(Name="Leaf", ViewObject=SimpleNamespace(Visibility=True))
    container = SimpleNamespace(
        Name="Container",
        ViewObject=SimpleNamespace(Visibility=True),
        **{members_property: [leaf]},
    )
    document = SimpleNamespace(Objects=[leaf, container])
    with pytest.raises(ValueError, match="top-level non-container"):
        presentation.apply(document, None, selected, "top")
    assert leaf.ViewObject.Visibility is True
    assert container.ViewObject.Visibility is True


@pytest.mark.parametrize("members_property", ["Group", "ElementList"])
def test_empty_container_profile_is_rejected_before_visibility_mutation(members_property):
    unrelated = SimpleNamespace(Name="Unrelated", ViewObject=SimpleNamespace(Visibility=True))
    container = SimpleNamespace(
        Name="Container",
        ViewObject=SimpleNamespace(Visibility=False),
        **{members_property: []},
    )
    document = SimpleNamespace(Objects=[unrelated, container])
    with pytest.raises(ValueError, match="top-level non-container"):
        presentation.apply(document, None, ["Container"], "top")
    assert unrelated.ViewObject.Visibility is True
    assert container.ViewObject.Visibility is False


@pytest.mark.parametrize("dropped", ["visibility", "camera_type", "orientation"])
def test_driver_rejects_a_dropped_requested_effect_before_save(tmp_path, monkeypatch, dropped):
    original = tmp_path / "source.FCStd"
    original.write_bytes(b"original")
    saved = []
    closed = []
    document = SimpleNamespace(
        Objects=[SimpleNamespace(Name="A")],
        recompute=lambda: None,
        saveAs=lambda path: saved.append(path),
    )
    actual = {
        "visible_objects": ["A"],
        "camera_type": "Orthographic",
        "camera_orientation": [0, 0, 0, 1],
        "camera": {"height": [1.0]},
    }
    if dropped == "visibility":
        actual["visible_objects"] = []
    elif dropped == "camera_type":
        actual["camera_type"] = "Perspective"
    else:
        actual["camera_orientation"] = [0.5, 0.5, 0.5, 0.5]
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace())
    monkeypatch.setitem(
        freecad_driver._SIBLING_MODULES, "dcc_mcp_freecad_write_contract", write_contract
    )
    monkeypatch.setitem(
        freecad_driver._SIBLING_MODULES, "dcc_mcp_freecad_presentation", presentation
    )
    monkeypatch.setattr(freecad_driver, "_host_version", lambda: "1.1.4")
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    monkeypatch.setattr(freecad_driver, "_open_document", lambda app, path: document)
    monkeypatch.setattr(freecad_driver, "_close_document", lambda app, doc: closed.append(doc))
    monkeypatch.setattr(freecad_driver, "_presentation_geometry", lambda doc: [])
    monkeypatch.setattr(presentation, "initialize", lambda: object())
    monkeypatch.setattr(presentation, "apply", lambda *args: actual)
    with pytest.raises(write_contract.WriteVerificationError) as failure:
        freecad_driver.document_save_copy(
            {
                "document_path": str(original),
                "output_path": str(tmp_path / "copy.FCStd"),
                "visible_objects": ["A"],
                "view": "top",
            }
        )
    assert failure.value.payload["check"] == "copy.presentation_request"
    assert saved == []
    assert closed == [document]
    assert original.read_bytes() == b"original"


@pytest.mark.parametrize("overwrite", [False, True])
def test_destination_created_during_native_call_respects_overwrite(
    tmp_path, monkeypatch, overwrite
):
    from pathlib import Path

    source = tmp_path / "source.FCStd"
    target = tmp_path / "target.FCStd"
    source.write_bytes(b"original")
    bridge = FreecadBridge(allowed_roots=[tmp_path])

    def native(method, params, timeout):
        Path(params["output_path"]).write_bytes(b"complete-native-copy")
        target.write_bytes(b"concurrent-destination")
        return {"object_count": 1}

    monkeypatch.setattr(bridge, "_invoke", native)
    if overwrite:
        bridge.save_copy(str(source), str(target), overwrite=True, visible_objects=["A"])
        assert target.read_bytes() == b"complete-native-copy"
    else:
        with pytest.raises(BridgeError, match="Output already exists"):
            bridge.save_copy(str(source), str(target), visible_objects=["A"])
        assert target.read_bytes() == b"concurrent-destination"
    assert source.read_bytes() == b"original"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.FCStd", "target.FCStd"]


@pytest.mark.parametrize("existing", [False, True])
def test_copy_metadata_failure_is_before_publication(tmp_path, monkeypatch, existing):
    from pathlib import Path

    source = tmp_path / "source.FCStd"
    target = tmp_path / "target.FCStd"
    source.write_bytes(b"original")
    if existing:
        target.write_bytes(b"existing destination")
    bridge = FreecadBridge(allowed_roots=[tmp_path])

    def native(_method, params, _timeout):
        Path(params["output_path"]).write_bytes(b"native copy")
        return {"object_count": 1}

    def unreadable(_path):
        raise PermissionError("stage metadata unavailable")

    monkeypatch.setattr(bridge, "_invoke", native)
    monkeypatch.setattr("dcc_mcp_freecad.bridge.sha256_file", unreadable)
    with pytest.raises(PermissionError, match="stage metadata"):
        bridge.save_copy(str(source), str(target), overwrite=existing, visible_objects=["A"])
    assert source.read_bytes() == b"original"
    if existing:
        assert target.read_bytes() == b"existing destination"
    else:
        assert not target.exists()
    assert {path.name for path in tmp_path.iterdir()} == (
        {"source.FCStd", "target.FCStd"} if existing else {"source.FCStd"}
    )


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("cleanup", ["stage", "backup"])
def test_copy_cleanup_error_preserves_publication_or_native_failure(
    tmp_path, monkeypatch, failure, cleanup
):
    from pathlib import Path

    source = tmp_path / "source.FCStd"
    target = tmp_path / "target.FCStd"
    source.write_bytes(b"original")
    bridge = FreecadBridge(allowed_roots=[tmp_path])
    original_unlink = Path.unlink
    cleanup_attempted = []
    native_error = BridgeError("original native failure")

    def failed_unlink(path, *args, **kwargs):
        if (
            path.name.startswith(".target.")
            and path.exists()
            and path.read_bytes() == b"native copy"
        ):
            cleanup_attempted.append(path.name)
            raise PermissionError("stage cleanup unavailable")
        return original_unlink(path, *args, **kwargs)

    def failed_backup_cleanup(_stage):
        cleanup_attempted.append("backup")
        raise PermissionError("backup cleanup unavailable")

    def native(_method, params, _timeout):
        Path(params["output_path"]).write_bytes(b"native copy")
        if cleanup == "stage":
            monkeypatch.setattr(Path, "unlink", failed_unlink)
        else:
            monkeypatch.setattr(
                "dcc_mcp_freecad.bridge._remove_staged_backups", failed_backup_cleanup
            )
        if failure:
            raise native_error
        return {"object_count": 1}

    monkeypatch.setattr(bridge, "_invoke", native)
    if failure:
        with pytest.raises(BridgeError) as caught:
            bridge.save_copy(str(source), str(target), visible_objects=["A"])
        assert caught.value is native_error
        assert not target.exists()
    else:
        result = bridge.save_copy(str(source), str(target), visible_objects=["A"])
        assert result["bytes"] == len(b"native copy")
        assert target.read_bytes() == b"native copy"
    assert cleanup_attempted
    assert source.read_bytes() == b"original"


def test_capture_passes_only_positional_arguments(tmp_path):
    """``saveImage`` is METH_VARARGS; there is no keyword spelling to use."""
    calls = []

    class View:
        def saveImage(self, *args, **kwargs):
            calls.append((args, kwargs))
            Path(args[0]).write_bytes(b"png-bytes")

    target = tmp_path / "frame.png"
    presentation.capture(View(), target, 640, 360)
    assert calls == [((str(target), 640, 360), {})]
    assert target.read_bytes() == b"png-bytes"


@pytest.mark.parametrize("missing,empty", [(True, False), (False, True)])
def test_capture_refuses_a_file_the_host_did_not_write(tmp_path, missing, empty):
    class View:
        def saveImage(self, *args, **kwargs):
            if not missing:
                Path(args[0]).write_bytes(b"" if empty else b"png-bytes")

    target = tmp_path / "frame.png"
    with pytest.raises(RuntimeError, match="no image" if missing else "empty image"):
        presentation.capture(View(), target, 8, 8)


def test_initialize_disables_the_notification_area_before_starting_gui(monkeypatch):
    """The offscreen notification area can deadlock the render call."""
    events = []

    def param_get(group):
        def set_bool(key, value):
            events.append((group.split("/")[-1], key, value))

        return SimpleNamespace(SetBool=set_bool)

    app = SimpleNamespace(ParamGet=param_get, GuiUp=True)
    gui = SimpleNamespace(showMainWindow=lambda: events.append(("Gui", "showMainWindow", None)))
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "FreeCADGui", gui)
    assert presentation.initialize() is gui
    keys = [entry[1] for entry in events]
    assert "NotificationAreaEnabled" in keys
    assert "NonIntrusiveNotificationsEnabled" in keys
    # Both are disabled before the GUI is started, which is the point.
    assert keys.index("showMainWindow") > keys.index("NotificationAreaEnabled")
    assert keys.index("showMainWindow") > keys.index("NonIntrusiveNotificationsEnabled")


def test_initialize_refuses_a_host_that_never_came_up(monkeypatch):
    app = SimpleNamespace(
        ParamGet=lambda group: SimpleNamespace(SetBool=lambda key, value: None), GuiUp=False
    )
    gui = SimpleNamespace(showMainWindow=lambda: None)
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "FreeCADGui", gui)
    with pytest.raises(RuntimeError, match="GUI view providers are unavailable"):
        presentation.initialize()


def test_renderable_names_excludes_containers_and_their_members():
    leaf = SimpleNamespace(Name="Leaf", ViewObject=SimpleNamespace(Visibility=True))
    container = SimpleNamespace(
        Name="Container", ViewObject=SimpleNamespace(Visibility=True), Group=[leaf]
    )
    link_group = SimpleNamespace(
        Name="Links", ViewObject=SimpleNamespace(Visibility=True), ElementList=[leaf]
    )
    plain = SimpleNamespace(Name="Plain", ViewObject=SimpleNamespace(Visibility=True))
    headless = SimpleNamespace(Name="Headless", ViewObject=None)
    document = SimpleNamespace(Objects=[leaf, container, link_group, plain, headless])
    assert presentation.renderable_names(document) == ["Plain"]


def test_hide_all_hides_every_provider_without_touching_the_camera():
    first = SimpleNamespace(Name="A", ViewObject=SimpleNamespace(Visibility=True))
    second = SimpleNamespace(Name="B", ViewObject=SimpleNamespace(Visibility=True))
    document = SimpleNamespace(Objects=[first, second, SimpleNamespace(Name="C", ViewObject=None)])
    presentation.hide_all(document)
    assert first.ViewObject.Visibility is False
    assert second.ViewObject.Visibility is False


def test_view_state_records_the_camera_as_the_host_serialized_it():
    active = SimpleNamespace(
        getCamera=lambda: "OrthographicCamera { height 12.5 }",
        getCameraType=lambda: "Orthographic",
    )
    gui = SimpleNamespace(
        getDocument=lambda name: SimpleNamespace(activeView=lambda: active),
        Selection=SimpleNamespace(getSelection=lambda: [SimpleNamespace(Name="A")]),
    )
    document = SimpleNamespace(
        Name="Doc",
        Objects=[
            SimpleNamespace(Name="A", ViewObject=SimpleNamespace(Visibility=True)),
            SimpleNamespace(Name="B", ViewObject=SimpleNamespace(Visibility=False)),
        ],
    )
    state = presentation.view_state(document, gui)
    assert state["camera"] == "OrthographicCamera { height 12.5 }"
    assert state["camera_type"] == "Orthographic"
    assert state["visibility"] == {"A": True, "B": False}
    assert state["selection"] == ["A"]


def test_view_state_records_no_selection_rather_than_an_empty_one():
    active = SimpleNamespace(getCamera=lambda: "camera", getCameraType=lambda: "Orthographic")
    gui = SimpleNamespace(
        getDocument=lambda name: SimpleNamespace(activeView=lambda: active),
        Selection=SimpleNamespace(getSelection=lambda: (_ for _ in ()).throw(OSError("no gui"))),
    )
    document = SimpleNamespace(Name="Doc", Objects=[])
    # An unrecorded state must not be restored as "nothing was selected".
    assert presentation.view_state(document, gui)["selection"] is None


def test_states_match_has_no_tolerance():
    first = {
        "camera": "OrthographicCamera { height 12.5 }",
        "camera_type": "Orthographic",
        "visibility": {"A": True},
        "selection": ["A"],
    }
    second = dict(first)
    assert presentation.states_match(first, second)
    drifted = dict(first, camera="OrthographicCamera { height 12.5000001 }")
    # The camera round-trips byte for byte, so any difference is a real move.
    assert not presentation.states_match(first, drifted)
    assert not presentation.states_match(first, dict(first, visibility={"A": False}))
    assert not presentation.states_match(first, dict(first, selection=[]))
    assert not presentation.states_match(first, dict(first, selection=None))
