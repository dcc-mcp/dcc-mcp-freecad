import sys
from copy import deepcopy
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
