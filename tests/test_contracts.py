from pathlib import Path

import yaml
from dcc_mcp_core import validate_skill

from dcc_mcp_freecad.server import FreecadMcpServer

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "src" / "dcc_mcp_freecad" / "skills"


SKILL_NAMES = ("freecad-session", "freecad-modeling", "freecad-modify")


def test_skill_contracts_are_valid():
    for name in SKILL_NAMES:
        report = validate_skill(str(SKILLS / name))
        errors = [issue.message for issue in report.issues if issue.severity == "error"]
        assert errors == [], name


def test_all_tools_are_typed_bounded_and_affinity_explicit():
    tools = []
    for name in SKILL_NAMES:
        payload = yaml.safe_load((SKILLS / name / "tools.yaml").read_text(encoding="utf-8"))
        tools.extend(payload["tools"])

    assert len(tools) == 22
    assert len({tool["name"] for tool in tools}) == 22
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


def test_geometry_bounds_in_the_skill_match_the_driver():
    """The typed limits and the enforced limits are the same numbers.

    A schema that advertises 200 edges while the driver accepts 500 is worse
    than either number alone: the caller is told one contract and held to
    another. The constants live in the driver because that is what enforces
    them, and this test is what keeps the advertised copy honest.
    """
    driver = (ROOT / "src" / "dcc_mcp_freecad" / "freecad_driver.py").read_text(encoding="utf-8")
    payload = yaml.safe_load((SKILLS / "freecad-modify" / "tools.yaml").read_text(encoding="utf-8"))
    tools = dict((tool["name"], tool) for tool in payload["tools"])

    assert "MAX_EDGE_REFS = 200" in driver
    assert "MAX_PATTERN_INSTANCES = 1000" in driver

    for name in ("fillet_edges", "chamfer_edges"):
        edge_refs = tools[name]["input_schema"]["properties"]["edge_refs"]
        assert edge_refs["maxItems"] == 200
        assert edge_refs["uniqueItems"] is True
        assert edge_refs["items"]["minimum"] == 1

    for name in ("linear_pattern", "polar_pattern"):
        assert tools[name]["input_schema"]["properties"]["count"]["maximum"] == 1000
        assert tools[name]["input_schema"]["properties"]["count"]["minimum"] == 1

    # A polar pattern must be given an angle, but only one way of expressing it.
    assert len(tools["polar_pattern"]["input_schema"]["oneOf"]) == 2


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


def test_explicit_standalone_gateway_options_are_preserved():
    server = FreecadMcpServer(port=0, gateway_port=0, enable_gateway_failover=False)
    options = next(value for value in vars(server).values() if hasattr(value, "instance_type"))
    assert options.gateway.port == 0
    assert options.gateway.enable_failover is False
