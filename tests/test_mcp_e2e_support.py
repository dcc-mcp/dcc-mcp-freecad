"""Contract checks only; these are not native execution or transport proof."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from e2e_support.artifacts import Evidence, public_value
from e2e_support.client import canonical, select_tools


def catalogs():
    root = (
        Path(__file__).resolve().parents[1]
        / "src/dcc_mcp_freecad/skills/freecad-session/tools.yaml"
    )
    declarations = {x["name"]: x for x in yaml.safe_load(root.read_text())["tools"]}
    full = declarations["save_copy"]["input_schema"]
    output = declarations["save_copy"]["output_schema"]
    anchor = {
        "name": "freecad_session__save_copy",
        "skill_name": "freecad-session",
        "inputSchema": deepcopy(full),
        "outputSchema": output,
    }
    wire = {
        "name": "save_copy",
        "inputSchema": {k: v for k, v in full.items() if k not in {"if", "then"}},
        "outputSchema": output,
    }
    return declarations, anchor, wire


def test_real_advertised_bare_name_is_used_with_full_source_schema():
    declarations, anchor, wire = catalogs()
    selected = select_tools([wire], [anchor], [("freecad-session", "save_copy")], declarations)
    assert selected["save_copy"][:2] == ("save_copy", "freecad_session__save_copy")
    assert "if" in selected["save_copy"][2]


@pytest.mark.parametrize("surface", ["anchor", "wire"])
def test_boolean_numeric_schema_drift_cannot_pass(surface):
    declarations, anchor, wire = catalogs()
    target = anchor if surface == "anchor" else wire
    target["inputSchema"]["additionalProperties"] = 0
    with pytest.raises(AssertionError):
        select_tools([wire], [anchor], [("freecad-session", "save_copy")], declarations)


def test_any_other_wire_omission_is_rejected():
    declarations, anchor, wire = catalogs()
    wire["inputSchema"].pop("dependencies")
    with pytest.raises(AssertionError):
        select_tools([wire], [anchor], [("freecad-session", "save_copy")], declarations)


def test_nonfinite_schema_is_not_silently_serialized():
    with pytest.raises(ValueError):
        canonical({"minimum": float("nan")})


def test_public_artifact_omits_diagnostics_and_machine_paths(tmp_path):
    output = public_value(
        {
            "source_path": "/home/private/model.FCStd",
            "other": "C:\\Users\\Someone\\m.FCStd",
            "url": "http://127.0.0.1:3214/mcp",
            "stdout": "private detail",
            "stderr": "secret-like payload",
            "tool": "save_copy",
            "opacity": 1.0,
        }
    )
    assert "stdout" not in output and "stderr" not in output and "url" not in output
    assert output["source_path"] == output["other"] == "<owned-file-or-endpoint>"
    evidence = Evidence(tmp_path / "evidence")
    evidence.record("request", "save_copy", output)
    evidence.report({"status": "FAIL", "error_type": "AssertionError", "failed_stage": "save_copy"})
    assert json.loads((tmp_path / "evidence/report.json").read_text())["status"] == "FAIL"
    assert "private" not in (tmp_path / "evidence/mcp-events.ndjson").read_text()


def test_cleanup_failure_still_closes_other_owned_handles_and_reports_terminals():
    from types import SimpleNamespace

    from e2e_support.cleanup import close_owned

    events = []

    def fail():
        events.append("signal")
        raise RuntimeError("synthetic cleanup failure")

    handle = SimpleNamespace(signal_shutdown=fail, shutdown=lambda: events.append("raw"))
    server = SimpleNamespace(stop=lambda: events.append("facade"), is_running=False)
    process = SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    cleanup, children = close_owned(server, handle, [("system.status", process)])
    assert events == ["signal", "raw", "facade"]
    assert cleanup["signal_shutdown"] is False and cleanup["raw_shutdown"] is True
    assert children == [{"method": "system.status", "returncode": 0, "cleanup_termination": False}]


def test_public_junit_retains_counts_but_removes_private_diagnostics(tmp_path):
    import importlib.util
    import xml.etree.ElementTree as ET

    path = Path(__file__).parents[1] / ".github/scripts/public-e2e-junit.py"
    spec = importlib.util.spec_from_file_location("public_junit", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "raw.xml"
    target = tmp_path / "public.xml"
    source.write_text(
        '<testsuites><testsuite tests="1" failures="1" hostname="private-host">'
        '<testcase name="case" file="/private/code.py"><failure message="secret">'
        "/private/path</failure><system-out>secret console</system-out>"
        "</testcase></testsuite></testsuites>"
    )
    module.public_report(source, target)
    text = target.read_text()
    assert "private" not in text and "secret" not in text
    assert ET.fromstring(text).find("testsuite").get("failures") == "1"
