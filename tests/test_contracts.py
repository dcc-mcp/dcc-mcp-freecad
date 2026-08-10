from pathlib import Path

import yaml
from dcc_mcp_core import validate_skill

from dcc_mcp_freecad.server import FreecadMcpServer

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "src" / "dcc_mcp_freecad" / "skills"


def test_skill_contracts_are_valid():
    for name in ("freecad-session", "freecad-modeling"):
        report = validate_skill(str(SKILLS / name))
        errors = [issue.message for issue in report.issues if issue.severity == "error"]
        assert errors == [], name


def test_all_tools_are_typed_bounded_and_affinity_explicit():
    tools = []
    for name in ("freecad-session", "freecad-modeling"):
        payload = yaml.safe_load((SKILLS / name / "tools.yaml").read_text(encoding="utf-8"))
        tools.extend(payload["tools"])

    assert len(tools) == 13
    assert len({tool["name"] for tool in tools}) == 13
    for tool in tools:
        assert tool["input_schema"]["type"] == "object"
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["output_schema"]["type"] == "object"
        assert tool["affinity"] == "any"
        assert tool["enforce_thread_affinity"] is True
        assert "timeout_hint_secs" in tool
        assert set(tool["annotations"]) == {
            "read_only_hint",
            "destructive_hint",
            "idempotent_hint",
            "open_world_hint",
            "deferred_hint",
        }


def test_modeling_declares_document_dependency():
    frontmatter = (SKILLS / "freecad-modeling" / "SKILL.md").read_text(encoding="utf-8")
    assert "depends: [freecad-session]" in frontmatter


def test_driver_exposes_only_a_method_whitelist():
    driver = (ROOT / "src" / "dcc_mcp_freecad" / "freecad_driver.py").read_text(encoding="utf-8")
    assert "_METHODS = {" in driver
    assert "eval(" not in driver
    assert "exec(" not in driver


def test_server_declares_standalone_lifetime():
    server = FreecadMcpServer(port=0)
    options = next(value for value in vars(server).values() if hasattr(value, "instance_type"))
    assert options.instance_type == "standalone"
