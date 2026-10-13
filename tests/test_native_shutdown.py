"""Pure lifecycle controls; real shutdown proof comes from the native CI gate."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcc_mcp_freecad import bridge as bridge_module
from dcc_mcp_freecad import freecad_driver as driver
from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge


@pytest.mark.parametrize("gui_up", [False, True])
def test_no_gui_window_requires_no_qt_import(monkeypatch, gui_up):
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(GuiUp=gui_up))
    monkeypatch.setitem(sys.modules, "FreeCADGui", SimpleNamespace(getMainWindow=lambda: None))
    monkeypatch.setitem(sys.modules, "PySide", None)
    driver._close_owned_gui()


@pytest.mark.parametrize(
    "fault", [None, "document", "refused", "not_destroyed", "destroyed_on_close"]
)
def test_owned_window_closes_and_is_deleted_before_interpreter_teardown(monkeypatch, fault):
    events, callbacks = [], []

    def close():
        assert len(callbacks) == 1
        events.append("close")
        if fault == "destroyed_on_close":
            callbacks[0](None)
        return fault != "refused"

    def flush(receiver, kind):
        assert receiver is None and kind == "DeferredDelete"
        events.append("flush")
        if fault != "not_destroyed":
            for callback in callbacks:
                callback(None)

    window = SimpleNamespace(
        close=close,
        destroyed=SimpleNamespace(connect=callbacks.append),
        deleteLater=lambda: events.append("deleteLater"),
    )
    monkeypatch.setitem(
        sys.modules,
        "FreeCAD",
        SimpleNamespace(
            GuiUp=True,
            listDocuments=lambda: {"Unexpected": object()} if fault == "document" else {},
        ),
    )
    monkeypatch.setitem(sys.modules, "FreeCADGui", SimpleNamespace(getMainWindow=lambda: window))
    monkeypatch.setitem(
        sys.modules,
        "PySide",
        SimpleNamespace(
            QtCore=SimpleNamespace(
                QCoreApplication=SimpleNamespace(sendPostedEvents=flush),
                QEvent=SimpleNamespace(DeferredDelete="DeferredDelete"),
            )
        ),
    )
    if fault in ("document", "refused", "not_destroyed"):
        with pytest.raises(RuntimeError):
            driver._close_owned_gui()
    else:
        driver._close_owned_gui()
    expected = [] if fault == "document" else ["close"]
    if fault not in ("document", "refused", "destroyed_on_close"):
        expected += ["deleteLater", "flush"]
    assert events == expected


@pytest.mark.parametrize("operation_error", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
def test_driver_serializes_result_only_after_cleanup_and_preserves_primary_failure(
    tmp_path, monkeypatch, operation_error, cleanup_error
):
    request, result = tmp_path / "request.json", tmp_path / "result.json"
    request.write_text(json.dumps({"method": "system.status"}))
    monkeypatch.setattr(sys, "argv", ["driver", "--pass", str(request), str(result)])
    events = []

    def operation(params):
        events.append("operation")
        if operation_error:
            raise ValueError("primary operation failure")
        return {"ready": True}

    def cleanup():
        events.append("cleanup")
        assert not result.exists()
        if cleanup_error:
            raise RuntimeError("secondary cleanup failure")

    monkeypatch.setitem(driver._METHODS, "system.status", operation)
    monkeypatch.setattr(driver, "_close_owned_gui", cleanup)
    driver.main()
    value = json.loads(result.read_text())
    assert events == ["operation", "cleanup"]
    assert value["ok"] is (not operation_error and not cleanup_error)
    if operation_error:
        assert value["error"]["type"] == "ValueError"
        assert value["error"]["message"] == "primary operation failure"
        assert ("gui_cleanup_error" in value["error"]) is cleanup_error
    elif cleanup_error:
        assert value["error"]["type"] == "RuntimeError"


@pytest.mark.parametrize("returncode", [1, -11])
def test_success_json_cannot_publish_a_mutation_after_nonzero_native_exit(
    tmp_path, monkeypatch, returncode
):
    executable = tmp_path / "FreeCADCmd"
    executable.write_bytes(b"synthetic executable, never run")
    model = tmp_path / "model.FCStd"
    model.write_bytes(b"immutable source")
    bridge = FreecadBridge(str(executable), allowed_roots=[tmp_path])

    def popen(command, **kwargs):
        request = json.loads(Path(command[-2]).read_text())
        Path(request["params"]["document_path"]).write_bytes(b"partial staged change")
        Path(command[-1]).write_text(json.dumps({"ok": True, "result": {"verified": True}}))
        return SimpleNamespace(poll=lambda: returncode, returncode=returncode)

    process_module = SimpleNamespace(**vars(bridge_module.subprocess))
    process_module.Popen = popen
    monkeypatch.setattr(bridge_module, "subprocess", process_module)
    with pytest.raises(BridgeError, match="exited unsuccessfully"):
        bridge._mutate_document("model.add_primitive", str(model), {}, 30)
    assert model.read_bytes() == b"immutable source"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["FreeCADCmd", "model.FCStd"]
