"""Contract locks for ``run_script``, the adapter's script-file escape hatch.

The tool deliberately loosens one thing the rest of the adapter never does - it
runs code the adapter did not write - so what is *still* enforced has to be
nailed down harder than usual, not softer. Each test here maps to one clause of
the A-1 decision:

* a path is required and must be a ``.py`` file inside allowed roots;
* a symlink is judged by the file it points at, not by where it is named;
* the timeout is capped by the script ceiling, not the document ceiling;
* a hang is killed and reported as a timeout rather than returning success;
* output is truncated with an explicit flag instead of silently cut;
* ``exit_code`` is reported on success as well as on failure;
* the child gets ``--safe-mode`` (vacuum mode: no user workbenches, plugins or
  macros) and a throwaway user config.

The last group is the "not a sandbox" clause made testable: these tests assert
the containment that exists, and deliberately assert no more than that.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dcc_mcp_freecad import bridge as bridge_module
from dcc_mcp_freecad.bridge import (
    DEFAULT_MAX_SCRIPT_TIMEOUT_SECS,
    BridgeError,
    BridgeTimeoutError,
    FreecadBridge,
)


def make_bridge(root: Path, **kwargs) -> FreecadBridge:
    bridge = FreecadBridge(allowed_roots=[root], **kwargs)
    bridge.executable = "fake-freecadcmd"
    return bridge


class FakeProcess:
    """A child that writes the given streams and reports the given exit status."""

    def __init__(self, stdout="", stderr="", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.terminated = False
        self.killed = False

    def __call__(self, command, **kwargs):
        FakeProcess.last = (command, kwargs)
        stream_out = kwargs["stdout"]
        stream_err = kwargs["stderr"]
        stream_out.write(self._stdout)
        stream_err.write(self._stderr)
        return self

    def poll(self):
        if self._hang:
            return None
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def install(monkeypatch, process) -> None:
    monkeypatch.setattr("dcc_mcp_freecad.bridge.subprocess.Popen", process)
    monkeypatch.setattr("dcc_mcp_freecad.bridge.time.sleep", lambda _seconds: None)


def write_script(root: Path, name="task.py", body="print('ran')\n") -> Path:
    script = root / name
    script.write_text(body, encoding="utf-8")
    return script


def test_run_script_reports_exit_code_and_hashes_the_script(tmp_path, monkeypatch):
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess(stdout="hello\n", stderr="", returncode=0))
    result = make_bridge(tmp_path).run_script(str(script))

    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["stderr"] == ""
    assert result["timed_out"] is False
    assert result["script_path"] == str(script)
    assert len(result["script_sha256"]) == 64
    assert result["stdout_truncated"] is False
    assert result["stderr_truncated"] is False
    assert result["duration_secs"] >= 0


def test_run_script_reports_a_non_zero_exit_code(tmp_path, monkeypatch):
    """A failing script is a result, not an exception.

    The script ran to completion and decided to fail; only the transport (the
    child never started, the runner went missing, the path was refused) is a
    BridgeError. Conflating the two would make every script bug look like an
    adapter bug.
    """
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess(stdout="partial\n", stderr="boom\n", returncode=3))
    result = make_bridge(tmp_path).run_script(str(script))

    assert result["exit_code"] == 3
    assert result["stdout"] == "partial\n"
    assert result["stderr"] == "boom\n"
    assert result["timed_out"] is False


@pytest.mark.parametrize(
    "body, message",
    [
        ("x = 1\n", "must end with .py"),
    ],
)
def test_run_script_refuses_a_non_python_suffix(tmp_path, body, message):
    target = tmp_path / "task.txt"
    target.write_text(body, encoding="utf-8")

    with pytest.raises(BridgeError, match=message):
        make_bridge(tmp_path).run_script(str(target))


def test_run_script_refuses_a_path_outside_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('nope')\n", encoding="utf-8")

    with pytest.raises(BridgeError, match="outside DCC_MCP_FREECAD_ALLOWED_ROOTS"):
        make_bridge(allowed).run_script(str(outside))


def test_run_script_refuses_a_symlink_that_escapes_the_roots(tmp_path, monkeypatch):
    """Containment is judged on the resolved target, not on the link's name.

    A link parked inside an allowed root is the obvious way around a containment
    check written against the path as passed, so resolution has to happen before
    the check rather than after it.
    """
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('escaped')\n", encoding="utf-8")
    link = allowed / "inside.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this filesystem or for this user")

    with pytest.raises(BridgeError, match="outside DCC_MCP_FREECAD_ALLOWED_ROOTS"):
        make_bridge(allowed).run_script(str(link))


def test_run_script_refuses_a_missing_file(tmp_path):
    with pytest.raises(BridgeError, match="Script does not exist"):
        make_bridge(tmp_path).run_script(str(tmp_path / "gone.py"))


def test_script_timeout_is_capped_below_the_document_ceiling(tmp_path, monkeypatch):
    """The escape hatch gets a shorter leash than a document round-trip.

    A script that blocks on a modal dialog is the failure being bounded, and
    1800 seconds of that is not a recoverable wait. 300s is the default and the
    request is refused outright above the ceiling rather than silently clamped,
    because a caller who asked for 900s and got 300s would be told a different
    contract than it was held to.
    """
    script = write_script(tmp_path)
    bridge = make_bridge(tmp_path)
    assert bridge.max_script_timeout_secs == DEFAULT_MAX_SCRIPT_TIMEOUT_SECS
    assert bridge.max_script_timeout_secs < bridge.max_timeout_secs

    install(monkeypatch, FakeProcess())
    with pytest.raises(BridgeError, match="no more than 300"):
        bridge.run_script(str(script), timeout_secs=900)


def test_script_timeout_env_var_widens_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("DCC_MCP_FREECAD_MAX_SCRIPT_TIMEOUT_SECS", "600")
    monkeypatch.setenv("DCC_MCP_FREECAD_ALLOWED_ROOTS", str(tmp_path))
    bridge = FreecadBridge.from_env()
    assert bridge.max_script_timeout_secs == 600


def test_script_ceiling_never_exceeds_the_hard_upper_bound():
    """A generous env var widens the leash but cannot remove it."""
    bridge = FreecadBridge(allowed_roots=[Path.cwd()], max_script_timeout_secs=100_000)
    assert bridge.max_script_timeout_secs == bridge_module.MAX_SCRIPT_TIMEOUT_SECS


def test_a_hanging_script_is_killed_and_reported_as_a_timeout(tmp_path, monkeypatch):
    """A hang must not degrade into a silent success.

    The child is terminated (and killed if it ignores SIGTERM) and the caller is
    told it timed out, so an agent can retry or raise instead of believing the
    script finished.
    """
    script = write_script(tmp_path)
    process = FakeProcess(hang=True)
    install(monkeypatch, process)

    class Clock:
        """Advance past the deadline on the third poll so the loop can end."""

        calls = 0

        @classmethod
        def monotonic(cls):
            cls.calls += 1
            return 0.0 if cls.calls < 3 else 10_000.0

    monkeypatch.setattr("dcc_mcp_freecad.bridge.time.monotonic", Clock.monotonic)

    with pytest.raises(BridgeTimeoutError, match="exceeded"):
        make_bridge(tmp_path).run_script(str(script), timeout_secs=1)

    assert process.terminated is True


def test_a_timeout_carries_what_the_script_printed_before_it_was_killed(tmp_path, monkeypatch):
    """``timed_out`` must be reachable, and it must carry the pre-hang output.

    The flag used to be dead code: it was set and then the exception was raised,
    so the return block that reported it never ran. That left the single worst
    failure mode - a script blocked on a modal dialog - identifiable only by
    matching the error's message text, and threw away the trace of how far the
    script got, which is the only clue to what blocked it.

    The regression this guards against is subtler than "field missing": it is
    "field present but always False". Asserting on the exception payload rather
    than a return value is what makes the difference observable.
    """
    script = write_script(tmp_path, body="print('reached-stage-2')\n")
    process = FakeProcess(hang=True, stdout="reached-stage-2\n", stderr="warn: blocking\n")
    install(monkeypatch, process)

    class Clock:
        calls = 0

        @classmethod
        def monotonic(cls):
            cls.calls += 1
            return 0.0 if cls.calls < 3 else 10_000.0

    monkeypatch.setattr("dcc_mcp_freecad.bridge.time.monotonic", Clock.monotonic)

    with pytest.raises(BridgeTimeoutError) as raised:
        make_bridge(tmp_path).run_script(str(script), timeout_secs=1)

    partial = raised.value.partial
    assert raised.value.timed_out is True
    assert partial["timed_out"] is True
    assert partial["stdout"] == "reached-stage-2\n"
    assert partial["stderr"] == "warn: blocking\n"
    # The child was killed, so there is no exit status to report - and reporting
    # one would let a caller read the kill as a completed run.
    assert partial["exit_code"] is None
    assert partial["script_path"] == str(script)
    assert len(partial["script_sha256"]) == 64


def test_the_skill_layer_reports_a_timeout_as_an_error_not_a_success(monkeypatch):
    """A timeout must never travel as a success.

    ``bridge_success`` reports success unconditionally, so returning a dict with
    ``timed_out: True`` would tell a caller that ignores the flag that the script
    finished - which is the exact lie this tool must not tell. It is therefore an
    error carrying the partial payload, with a stable ``error_code`` so the
    caller branches on a key instead of matching prose.
    """
    import dcc_mcp_freecad.skill_tools as skill_tools

    class TimedOutBridge:
        def run_script(self, **_kwargs):
            raise BridgeTimeoutError(
                "exceeded", partial={"timed_out": True, "stdout": "partial", "exit_code": None}
            )

    monkeypatch.setattr(skill_tools, "get_bridge", lambda: TimedOutBridge())
    result = skill_tools.script_main("run_script", "FreeCAD script executed.")(script_path="x.py")

    assert result["success"] is False
    assert result["error"] == "script_timeout"
    context = result["context"]
    assert context["timed_out"] is True
    assert context["stdout"] == "partial"
    assert context["exit_code"] is None


def test_a_non_zero_exit_is_still_a_result_not_an_error(monkeypatch):
    """A script that ran and failed is not an adapter fault.

    This is the other half of the timeout rule and stops it being over-applied:
    only a *timeout* is an error outcome. A script that completed with a non-zero
    status is reported the same way a successful one is, so the caller reads
    ``exit_code`` and decides.
    """
    import dcc_mcp_freecad.skill_tools as skill_tools

    class FailingBridge:
        def run_script(self, **_kwargs):
            return {"exit_code": 3, "stdout": "", "stderr": "boom", "timed_out": False}

    monkeypatch.setattr(skill_tools, "get_bridge", lambda: FailingBridge())
    result = skill_tools.script_main("run_script", "FreeCAD script executed.")(script_path="x.py")

    assert result["success"] is True
    assert result["context"]["exit_code"] == 3


def test_long_output_is_truncated_with_an_explicit_flag(tmp_path, monkeypatch):
    """A cut stream must say it was cut.

    Silently dropping the tail would make "the script printed nothing useful"
    indistinguishable from "there were 200KB we did not show you", which sends
    the caller debugging the wrong thing.
    """
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess(stdout="A" * 70_000, stderr="B" * 70_000, returncode=0))
    result = make_bridge(tmp_path).run_script(str(script))

    assert len(result["stdout"]) == 65_536
    assert len(result["stderr"]) == 65_536
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True


def test_the_child_runs_in_safe_mode_with_a_temporary_user_config(tmp_path, monkeypatch):
    """Vacuum mode: no user workbench, plugin, or macro is loaded.

    ``--safe-mode`` plus a throwaway ``--user-cfg`` is what keeps the upstream
    hangs traced to user add-ons (a modal dialog nothing can close, a broken
    FeaturePython object from an earlier run) unreachable through this door. The
    user config therefore has to be inside the call's temp directory, never the
    operator's home.
    """
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess())
    make_bridge(tmp_path).run_script(str(script))

    command, kwargs = FakeProcess.last
    assert command[0] == "fake-freecadcmd"
    assert "--safe-mode" in command
    assert "--user-cfg" in command
    config = Path(command[command.index("--user-cfg") + 1])
    assert config.parent == Path(kwargs["cwd"])
    assert command[-2:] == ["--script", str(script)]


def test_the_packaged_runner_is_used_not_a_caller_path(tmp_path, monkeypatch):
    """The code the child executes first is package-owned.

    Only *the script* is caller-supplied. The runner between FreeCAD and that
    script is shipped in the wheel and resolved from the driver's own directory,
    so a file named ``script_runner.py`` sitting next to the script cannot
    substitute itself.
    """
    script = write_script(tmp_path)
    decoy = tmp_path / "script_runner.py"
    decoy.write_text("raise SystemExit('decoy')\n", encoding="utf-8")
    install(monkeypatch, FakeProcess())
    bridge = make_bridge(tmp_path)
    bridge.run_script(str(script))

    command, _kwargs = FakeProcess.last
    assert Path(command[-3]) == bridge.driver_path.with_name("script_runner.py")


def test_run_script_needs_an_executable(tmp_path):
    bridge = FreecadBridge(allowed_roots=[tmp_path])
    bridge.executable = None
    script = write_script(tmp_path)

    with pytest.raises(BridgeError, match="FreeCADCmd was not found"):
        bridge.run_script(str(script))


def test_run_script_refuses_when_the_packaged_runner_is_absent(tmp_path, monkeypatch):
    """A half-installed wheel fails loudly instead of running something else.

    Both the driver and the runner are resolved from the package directory, so a
    wheel missing the runner must not fall back to a same-named file the caller
    happens to have - that would silently change what the child executes.
    """
    script = write_script(tmp_path)
    fake_package = tmp_path / "pkg"
    fake_package.mkdir()
    (fake_package / "freecad_driver.py").write_text("", encoding="utf-8")
    bridge = make_bridge(tmp_path)
    monkeypatch.setattr(bridge, "driver_path", fake_package / "freecad_driver.py")

    with pytest.raises(BridgeError, match="script runner is missing"):
        bridge.run_script(str(script))


def test_the_python_module_backend_launches_the_runner_isolated(tmp_path, monkeypatch):
    """The escape hatch is contained on the alternate backend too.

    ``-I`` keeps user site and PYTHONPATH out, and HOME/XDG/FREECAD_USER_HOME
    are redirected into the call's temp directory so nothing a script writes
    lands in the operator's home.
    """
    directory = tmp_path / "mod"
    directory.mkdir()
    (directory / "FreeCAD.so").write_bytes(b"")
    bridge = FreecadBridge(
        executable=sys.executable,
        module_directory=str(directory),
        backend="python-module",
        allowed_roots=[tmp_path],
    )
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess())
    monkeypatch.setenv("PYTHONPATH", "/not-inherited")

    result = bridge.run_script(str(script))

    command, kwargs = FakeProcess.last
    assert command[:2] == [str(Path(sys.executable).resolve()), "-I"]
    assert Path(command[2]) == bridge.driver_path.with_name("script_runner.py")
    assert command[3] == str(directory)
    assert command[4] == str(script)
    assert kwargs["env"].get("PYTHONPATH") is None
    for name in ["HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "FREECAD_USER_HOME"]:
        assert Path(kwargs["env"][name]).parent == Path(kwargs["cwd"])
    assert result["exit_code"] == 0


def test_the_runner_executes_a_real_script_end_to_end(tmp_path):
    """The runner works against a real interpreter, not only a faked child.

    This is the only test here that does not fake the child: it proves the argv
    contract the wrapper builds is one ``script_runner.py`` actually accepts,
    and that a script's own exit code and stdout survive the round trip.
    """
    runner = Path(bridge_module.__file__).with_name("script_runner.py")
    script = write_script(
        tmp_path,
        body="import sys\nsys.stdout.write('ran-ok')\nsys.exit(4)\n",
    )
    completed = subprocess.run(
        [sys.executable, str(runner), "--script", str(script)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 4
    assert completed.stdout == "ran-ok"


def test_the_runner_keeps_the_module_directory_behind_stdlib(tmp_path):
    """A script directory must not shadow the modules the runner itself uses.

    Inserting at ``sys.path[0]`` would let a ``json.py`` or ``io.py`` shipped
    next to a script be imported *by the runner*, breaking it before the script
    ever runs - and making the failure look like an adapter bug.
    """
    runner = Path(bridge_module.__file__).with_name("script_runner.py")
    directory = tmp_path / "mod"
    directory.mkdir()
    (directory / "json.py").write_text("raise SystemExit('shadowed')\n", encoding="utf-8")
    script = write_script(tmp_path, body="import json\nprint(json.dumps({'ok': True}))\n")

    completed = subprocess.run(
        [sys.executable, str(runner), str(directory), "--script", str(script)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == '{"ok": true}'


def test_the_runner_honours_a_pep_263_encoding_declaration(tmp_path):
    """A legacy-encoded script must run, not fail to decode.

    The escape hatch exists for the long tail the typed tools do not cover, and
    older GBK/Big5/latin-1 scripts with a coding cookie are squarely in it. A
    hardcoded UTF-8 read rejects them before one line executes, and reports a
    script's encoding as an adapter-side ``UnicodeDecodeError`` - which reads as
    "the runner is broken" rather than "your file declares an encoding we
    ignored". Plain ``python x.py`` accepts these files, so the difference is
    observable to anyone who tries the same script both ways.
    """
    runner = Path(bridge_module.__file__).with_name("script_runner.py")
    # The script writes its literal to a file as UTF-8 bytes rather than printing
    # it. Printing would make this test depend on the *child's console encoding*,
    # which is cp1252 on Windows CI and cannot represent these characters - the
    # script would fail with an encode error even though its source decoded
    # perfectly, so the test would be failing for the wrong reason. Writing to a
    # file pins the one thing under test: did the source decode at all?
    latin1 = tmp_path / "legacy.py"
    latin1.write_bytes(
        (
            "# -*- coding: latin-1 -*-\n"
            "open(%r, 'wb').write('caf\xe9'.encode('utf-8'))\n" % str(tmp_path / "l1.out")
        ).encode("latin-1")
    )
    gbk = tmp_path / "legacy_gbk.py"
    gbk.write_bytes(
        (
            "# -*- coding: gbk -*-\n"
            "open(%r, 'wb').write('\u4e2d\u6587'.encode('utf-8'))\n" % str(tmp_path / "gbk.out")
        ).encode("gbk")
    )

    latin1_run = subprocess.run(
        [sys.executable, str(runner), "--script", str(latin1)],
        capture_output=True,
    )
    assert latin1_run.returncode == 0, latin1_run.stderr.decode("utf-8", "replace")
    assert (tmp_path / "l1.out").read_bytes().decode("utf-8") == "caf\u00e9"

    gbk_run = subprocess.run(
        [sys.executable, str(runner), "--script", str(gbk)],
        capture_output=True,
    )
    assert gbk_run.returncode == 0, gbk_run.stderr.decode("utf-8", "replace")
    assert (tmp_path / "gbk.out").read_bytes().decode("utf-8") == "\u4e2d\u6587"


def test_script_sha256_is_the_content_that_ran_not_the_file_afterwards(tmp_path, monkeypatch):
    """The audit hash is taken before the child starts.

    A digest computed after execution fingerprints whatever the file now holds,
    so a script that rewrites itself is reported under the hash of its
    replacement - an audit field that points at the wrong bytes is worse than no
    hash, because it looks like evidence.
    """
    script = write_script(tmp_path, body="print('first')\n")
    before = hashlib.sha256(script.read_bytes()).hexdigest()

    def rewrite_then_return(command, **kwargs):
        script.write_text("print('rewritten')\n", encoding="utf-8")
        return FakeProcess(stdout="ok\n")(command, **kwargs)

    install(monkeypatch, rewrite_then_return)
    result = make_bridge(tmp_path).run_script(str(script))

    assert result["script_sha256"] == before
    assert result["script_sha256"] != hashlib.sha256(script.read_bytes()).hexdigest()


def test_the_child_inherits_no_stdin(tmp_path, monkeypatch):
    """The child must not inherit this process's stdin.

    Under MCP stdio the adapter's own stdin *is* the protocol stream; a child
    that inherited it could consume bytes belonging to the protocol, or block
    waiting on input the protocol will never send.
    """
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess())
    make_bridge(tmp_path).run_script(str(script))

    _command, kwargs = FakeProcess.last
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_the_child_is_forced_offscreen(tmp_path, monkeypatch):
    """QT_QPA_PLATFORM=offscreen keeps a script from opening a real window.

    A script that touched platform input would otherwise be able to raise a
    window on the operator's desktop - or hang on one in a headless session.
    """
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess())
    make_bridge(tmp_path).run_script(str(script))

    _command, kwargs = FakeProcess.last
    assert kwargs["env"]["QT_QPA_PLATFORM"] == "offscreen"


def test_the_python_module_backend_drops_pythonhome(tmp_path, monkeypatch):
    """PYTHONHOME must not reach the child on the python-module backend.

    Dropping PYTHONPATH alone leaves PYTHONHOME able to redirect the interpreter
    at a different stdlib - the same caller-controlled-interpreter problem the
    PATH removal is there to prevent, via a variable that is easy to overlook.
    """
    directory = tmp_path / "mod"
    directory.mkdir()
    (directory / "FreeCAD.so").write_bytes(b"")
    bridge = FreecadBridge(
        executable=sys.executable,
        module_directory=str(directory),
        backend="python-module",
        allowed_roots=[tmp_path],
    )
    script = write_script(tmp_path)
    install(monkeypatch, FakeProcess())
    monkeypatch.setenv("PYTHONPATH", "/not-inherited")
    monkeypatch.setenv("PYTHONHOME", "/not-inherited")

    bridge.run_script(str(script))

    _command, kwargs = FakeProcess.last
    assert kwargs["env"].get("PYTHONPATH") is None
    assert kwargs["env"].get("PYTHONHOME") is None


def test_the_module_directory_outranks_site_packages(tmp_path):
    """The native library must be found before any installed copy.

    Two directions both matter. Too early and a ``json.py`` next to the script is
    imported by the runner itself, so a script bug reports as an adapter crash;
    too late and a differently-ABI FreeCAD installed in site-packages wins. The
    first direction already had a test; this covers the second by putting a
    decoy where site-packages would be and asserting the runner still imports the
    real stdlib, which only happens if the splice landed ahead of it.
    """
    runner = Path(bridge_module.__file__).with_name("script_runner.py")
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "probe_marker.py").write_text("value = 'site'\n", encoding="utf-8")
    module = tmp_path / "mod"
    module.mkdir()
    (module / "probe_marker.py").write_text("value = 'module'\n", encoding="utf-8")
    script = write_script(
        tmp_path,
        body="import probe_marker\nprint(probe_marker.value)\n",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(site)
    completed = subprocess.run(
        [sys.executable, str(runner), str(module), "--script", str(script)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "module"


def test_the_runner_rejects_a_bad_argv():
    runner = Path(bridge_module.__file__).with_name("script_runner.py")
    completed = subprocess.run(
        [sys.executable, str(runner)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "usage" in (completed.stderr + completed.stdout)


def test_the_runner_reports_a_missing_script(tmp_path):
    runner = Path(bridge_module.__file__).with_name("script_runner.py")
    completed = subprocess.run(
        [sys.executable, str(runner), "--script", str(tmp_path / "gone.py")],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2


def test_script_execution_is_declared_and_not_advertised_as_a_sandbox():
    """The capability block must be explicit that this is not a sandbox.

    An agent reading ``get_capabilities`` decides from this block whether the
    tool is safe for untrusted input. Omitting the flag would let it assume the
    containment is stronger than it is, which is the single most dangerous thing
    this declaration could do.
    """
    from dcc_mcp_freecad.capabilities import (
        SCRIPT_EXECUTION,
        SCRIPT_EXECUTION_BOUNDS,
        build_capabilities,
    )

    served = build_capabilities()
    assert served["arbitrary_python"] is False
    assert served["script_execution"] == SCRIPT_EXECUTION == "script_file"
    assert "run_script" in served["methods"]
    assert served["script_execution_bounds"]["sandboxed"] is False
    assert served["script_execution_bounds"]["isolated_process"] is True
    assert served["script_execution_bounds"]["accepts"] == ["script_path"]
    for key in ("loads_user_workbenches", "loads_user_plugins", "loads_user_macros"):
        assert served["script_execution_bounds"][key] is False
    assert served["script_execution_bounds"]["script_management"] is False
    assert SCRIPT_EXECUTION_BOUNDS == served["script_execution_bounds"]


def test_typed_calls_also_report_exit_code_on_success(tmp_path, monkeypatch):
    """``exit_code`` is a transport field, not a script-only field.

    ``process.returncode`` previously appeared only in the failure branch of
    ``_invoke``, so a child that wrote a valid result file and *then* exited
    non-zero was reported as a clean success. The same block now covers both
    paths, which is what makes the script tool's exit code trustworthy rather
    than a second, divergent implementation.
    """
    bridge = make_bridge(tmp_path)

    class Process:
        returncode = 7

        def __init__(self, command, **kwargs):
            Path(command[-1]).write_text(
                json.dumps({"ok": True, "result": {"version": "1.0.0"}}), encoding="utf-8"
            )

        def poll(self):
            return 0

    monkeypatch.setattr("dcc_mcp_freecad.bridge.subprocess.Popen", Process)

    result = bridge.status()

    assert result["exit_code"] == 7
    assert result["version"] == "1.0.0"
    assert "stdout_truncated" in result
    assert result["stderr_truncated"] is False
    assert bridge_module.OUTPUT_LIMIT == 65_536


def test_run_script_is_declared_in_the_session_catalog():
    """The tool the capabilities advertise is the tool the catalog registers.

    Both sides come from ``tools.yaml``, so this is really a lock against the
    declaration losing the clauses that make the escape hatch safe: a path (never
    source text) and a timeout bounded at the script ceiling.
    """
    import yaml

    skills = Path(bridge_module.__file__).parent / "skills"
    payload = yaml.safe_load(
        (skills / "freecad-session" / "tools.yaml").read_text(encoding="utf-8")
    )
    tool = next(item for item in payload["tools"] if item["name"] == "run_script")
    schema = tool["input_schema"]
    assert schema["required"] == ["script_path"]
    assert set(schema["properties"]) == {"script_path", "timeout_secs"}
    assert schema["properties"]["timeout_secs"]["maximum"] == 300
    assert schema["additionalProperties"] is False
    assert tool["source_file"] == "scripts/run_script.py"
    assert (skills / "freecad-session" / "scripts" / "run_script.py").is_file()
    assert "not a sandbox" in tool["description"]


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_script_runs_in_the_vacuum_child(tmp_path: Path):
    """The escape hatch works on a real host, not only against a faked child.

    Every other test here asserts on the argv the wrapper builds; this one
    asserts the host actually accepts it. Two things are only observable here:

    * FreeCADCmd really does accept ``--user-cfg <call temp dir>`` pointing at a
      directory that does not exist yet, which is the whole basis of vacuum mode.
    * A script really can import ``FreeCAD`` and report the host version back,
      proving the child is a working FreeCAD interpreter and not a shell that
      happened to start.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    script = tmp_path / "probe.py"
    script.write_text(
        "import FreeCAD\n"
        "print('version=' + '.'.join(str(part) for part in FreeCAD.Version()[:3]))\n",
        encoding="utf-8",
    )

    result = bridge.run_script(str(script))

    assert result["exit_code"] == 0, result["stderr"]
    assert result["timed_out"] is False
    assert "version=" in result["stdout"]
    # The child is a real FreeCAD, so it must report the same version the typed
    # path sees - otherwise the script ran in some other interpreter.
    assert result["stdout"].split("version=")[1].split()[0] == bridge.status()["version"]
    assert len(result["script_sha256"]) == 64


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_script_exit_code_and_failure_survive(tmp_path: Path):
    """A failing script is a result carrying its status, not an adapter error.

    On a real host this also confirms FreeCAD's own exit path is what reports the
    status: the child writes no result file for the script path, so a non-zero
    exit is the only signal - and it has to arrive intact.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    ok_script = tmp_path / "ok.py"
    ok_script.write_text("print('done')\n", encoding="utf-8")
    fail_script = tmp_path / "fail.py"
    fail_script.write_text(
        "import sys\nsys.stderr.write('deliberate')\nsys.exit(3)\n", encoding="utf-8"
    )

    ok = bridge.run_script(str(ok_script))
    assert ok["exit_code"] == 0
    assert ok["stdout"] == "done\n"

    failed = bridge.run_script(str(fail_script))
    assert failed["exit_code"] == 3
    assert failed["stderr"] == "deliberate"
    assert failed["timed_out"] is False
