from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import FreecadBridge


def module_dir(tmp_path):
    directory = tmp_path / "installed-library"
    directory.mkdir()
    (directory / "FreeCAD.so").write_bytes(b"unit-test native library placeholder")
    return directory


def test_cli_remains_default(tmp_path):
    bridge = FreecadBridge(executable=sys.executable, allowed_roots=[tmp_path])
    assert bridge.backend == "freecadcmd"
    assert bridge.module_directory is None


@pytest.mark.parametrize("backend", ["auto", "python", "", None])
def test_unknown_backend_rejected(tmp_path, backend):
    with pytest.raises(ValueError, match="backend"):
        FreecadBridge(backend=backend, allowed_roots=[tmp_path])


@pytest.mark.parametrize("executable,module", [(None, "/missing"), (sys.executable, None)])
def test_module_backend_requires_explicit_operator_configuration(tmp_path, executable, module):
    with pytest.raises(ValueError, match="explicit"):
        FreecadBridge(executable=executable, module_directory=module, backend="python-module")


def test_missing_native_library_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="native FreeCAD library"):
        FreecadBridge(
            executable=sys.executable, module_directory=str(tmp_path), backend="python-module"
        )


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_module_backend_rejects_invalid_interpreter(tmp_path, kind):
    interpreter = tmp_path / "interpreter"
    if kind == "directory":
        interpreter.mkdir()
    with pytest.raises(ValueError, match="python-module.*DCC_MCP_FREECAD_PYTHON"):
        FreecadBridge(
            executable=str(interpreter),
            module_directory=str(module_dir(tmp_path)),
            backend="python-module",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission")
def test_module_backend_rejects_nonexecutable_interpreter(tmp_path):
    interpreter = tmp_path / "interpreter"
    interpreter.write_bytes(b"synthetic nonexecutable file")
    interpreter.chmod(0o600)
    with pytest.raises(ValueError, match="python-module.*DCC_MCP_FREECAD_PYTHON"):
        FreecadBridge(
            executable=str(interpreter),
            module_directory=str(module_dir(tmp_path)),
            backend="python-module",
        )


def test_environment_selects_only_explicit_backend(tmp_path, monkeypatch):
    directory = module_dir(tmp_path)
    monkeypatch.setenv("DCC_MCP_FREECAD_BACKEND", "python-module")
    monkeypatch.setenv("DCC_MCP_FREECAD_PYTHON", sys.executable)
    monkeypatch.setenv("DCC_MCP_FREECAD_MODULE_DIRECTORY", str(directory))
    monkeypatch.setenv("DCC_MCP_FREECAD_ALLOWED_ROOTS", str(tmp_path))
    bridge = FreecadBridge.from_env()
    assert bridge.executable == str(Path(sys.executable).resolve())
    assert bridge.module_directory == directory


def test_module_command_is_package_owned_and_preferences_are_temporary(tmp_path, monkeypatch):
    directory = module_dir(tmp_path)
    bridge = FreecadBridge(
        executable=sys.executable,
        module_directory=str(directory),
        backend="python-module",
        allowed_roots=[tmp_path],
    )
    observed = {}

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            observed.update(command=command, **kwargs)
            Path(command[-1]).write_text(json.dumps({"ok": True, "result": {"version": "1.0.0"}}))

        def poll(self):
            return 0

    monkeypatch.setenv("PYTHONPATH", "/not-inherited")
    monkeypatch.setenv("PYTHONHOME", "/not-inherited")
    monkeypatch.setattr("dcc_mcp_freecad.bridge.subprocess.Popen", Process)
    result = bridge.status()
    command = observed["command"]
    assert command[:2] == [str(Path(sys.executable).resolve()), "-I"]
    assert Path(command[2]) == bridge.driver_path.with_name("module_runner.py")
    assert command[3] == str(directory)
    assert "--safe-mode" not in command
    assert observed["env"].get("PYTHONPATH") is None
    assert observed["env"].get("PYTHONHOME") is None
    for name in ["HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "FREECAD_USER_HOME"]:
        assert Path(observed["env"][name]).parent == Path(observed["cwd"])
        assert not Path(observed["env"][name]).exists()
    assert result["backend"] == "python-module"
    assert result["module_directory"] == str(directory)
    assert result["ready"] is True
