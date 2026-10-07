from pathlib import Path

import capability_checks
import pytest
import yaml
from dcc_mcp_core import validate_skill

from dcc_mcp_freecad import capabilities, compat
from dcc_mcp_freecad.server import FreecadMcpServer

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "src" / "dcc_mcp_freecad" / "skills"


SKILL_NAMES = (
    "freecad-session",
    "freecad-modeling",
    "freecad-modify",
    "freecad-parts",
    "freecad-sketch",
)


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

    assert len(tools) == 33
    assert len({tool["name"] for tool in tools}) == 33
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


def test_declared_primitives_match_the_driver_table():
    from dcc_mcp_freecad import freecad_driver

    payload = yaml.safe_load(
        (SKILLS / "freecad-modeling" / "tools.yaml").read_text(encoding="utf-8")
    )
    add_primitive = next(tool for tool in payload["tools"] if tool["name"] == "add_primitive")

    assert sorted(add_primitive["input_schema"]["properties"]["primitive"]["enum"]) == sorted(
        freecad_driver._PRIMITIVE_TYPES
    )


def test_declared_dimensions_cover_every_primitive_property():
    """A primitive property with no schema entry is a dimension nobody can pass."""
    from dcc_mcp_freecad import freecad_driver

    payload = yaml.safe_load(
        (SKILLS / "freecad-modeling" / "tools.yaml").read_text(encoding="utf-8")
    )
    needed = set()
    for mapping in freecad_driver._DIMENSION_PROPERTIES.values():
        needed.update(mapping)
    for name in ("add_primitive", "update_primitive"):
        tool = next(item for item in payload["tools"] if item["name"] == name)
        declared = set(tool["input_schema"]["properties"]["dimensions"]["properties"])
        assert needed <= declared, "%s is missing %s" % (name, sorted(needed - declared))


def test_modeling_declares_document_dependency():
    frontmatter = (SKILLS / "freecad-modeling" / "SKILL.md").read_text(encoding="utf-8")
    assert "depends: [freecad-session]" in frontmatter


def test_sketch_declares_document_dependency():
    frontmatter = (SKILLS / "freecad-sketch" / "SKILL.md").read_text(encoding="utf-8")
    assert "depends: [freecad-session]" in frontmatter


def test_sketch_tools_accept_only_declared_union_members():
    """A discriminated union is only real if a wrong member is rejected."""
    from dcc_mcp_core.skills_helper import ToolValidator

    payload = yaml.safe_load((SKILLS / "freecad-sketch" / "tools.yaml").read_text(encoding="utf-8"))
    tools = {tool["name"]: tool for tool in payload["tools"]}
    import json

    geometry = ToolValidator(_schema=tools["add_sketch_geometry"]["input_schema"])
    assert geometry.validate(
        json.dumps(
            {
                "document_path": "a.FCStd",
                "sketch_name": "S",
                "geometry": [{"type": "circle", "cx": 0, "cy": 0, "radius": 2}],
            }
        )
    )[0]
    assert not geometry.validate(
        json.dumps(
            {
                "document_path": "a.FCStd",
                "sketch_name": "S",
                "geometry": [{"type": "circle", "cx": 0, "cy": 0}],
            }
        )
    )[0]
    assert not geometry.validate(
        json.dumps({"document_path": "a.FCStd", "sketch_name": "S", "geometry": [{"type": "blob"}]})
    )[0]

    constraints = ToolValidator(_schema=tools["add_sketch_constraint"]["input_schema"])
    assert constraints.validate(
        json.dumps(
            {
                "document_path": "a.FCStd",
                "sketch_name": "S",
                "constraints": [{"type": "radius", "first": {"element": 0}, "value": 4}],
            }
        )
    )[0]
    assert not constraints.validate(
        json.dumps(
            {
                "document_path": "a.FCStd",
                "sketch_name": "S",
                "constraints": [
                    {"type": "angle", "first": {"element": 0}, "second": {"element": 1}}
                ],
            }
        )
    )[0]


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


# --------------------------------------------------------------------------
# get_capabilities contract locks
#
# An agent reads get_capabilities before it calls anything else, so a
# declaration that disagrees with the implementation produces no error - the
# agent just keeps calling the tool wrong. These locks compare the served
# payload, the catalog, and the callables that actually run, field by field.
# --------------------------------------------------------------------------

_HOST_VERSION = "1.1.4"


def _host_status():
    """A host report shaped like the one a real FreeCAD returns."""
    return {
        "ready": True,
        "version": _HOST_VERSION,
        "host_matrix": compat.classify_host(_HOST_VERSION),
    }


def _served_capabilities(skills_dir=None):
    from dcc_mcp_freecad import parts_library
    from dcc_mcp_freecad.bridge import _EXPORT_SUFFIXES, _IMPORT_SUFFIXES

    return capabilities.build_capabilities(
        host_status=_host_status(),
        import_extensions=_IMPORT_SUFFIXES,
        export_extensions=_EXPORT_SUFFIXES,
        parts={"part_extensions": sorted(parts_library.PART_SUFFIXES)},
        skills_dir=skills_dir,
    )


def test_capability_declarations_match_the_tool_catalog():
    """Every tool's declared schema survives the trip into get_capabilities."""
    payload = _served_capabilities()
    catalog = capability_checks.tool_catalog()

    assert len(payload["tools"]) == len(catalog) == 33
    problems = capability_checks.capability_problems(payload)
    assert problems == [], "get_capabilities drifted from tools.yaml:\n%s" % "\n".join(problems)


def test_advertised_limits_match_what_the_driver_enforces():
    """The limits get_capabilities reports are the limits the driver refuses on.

    These are the keys a caller reads before it decides how to batch work, so a
    number that disagrees with the driver sends the caller into a rejection it
    was told it could not hit. They are derived from the same schemas the
    driver is held to, and derived independently of the driver constants so a
    change on either side has to show up here.
    """
    driver = (ROOT / "src" / "dcc_mcp_freecad" / "freecad_driver.py").read_text(encoding="utf-8")
    payload = _served_capabilities()

    assert "MAX_EDGE_REFS = %d" % payload["max_edge_refs"] in driver
    assert "MAX_PATTERN_INSTANCES = %d" % payload["max_pattern_instances"] in driver
    assert payload["mirror_planes"] == ["xy", "xz", "yz"]
    assert payload["document_snapshots"] is True


def test_capabilities_are_derived_from_the_catalog_not_restated(tmp_path: Path):
    """Adding a tool to tools.yaml has to change get_capabilities by itself."""
    skills = capability_checks.copy_skills(tmp_path)
    before = _served_capabilities(skills)
    assert "probe_tool" not in before["methods"]

    catalog = skills / "freecad-session" / "tools.yaml"
    text = catalog.read_text(encoding="utf-8")
    catalog.write_text(
        text
        + "\n"
        + "  - name: probe_tool\n"
        + "    description: Temporary derivation probe.\n"
        + "    source_file: scripts/get_status.py\n"
        + "    input_schema: {type: object, properties: {}, additionalProperties: false}\n"
        + "    output_schema: {type: object}\n"
        + "    execution: sync\n"
        + "    affinity: any\n"
        + "    enforce_thread_affinity: true\n"
        + "    timeout_hint_secs: 30\n"
        + "    annotations: {read_only_hint: true, destructive_hint: false, "
        + "idempotent_hint: true, open_world_hint: false, deferred_hint: false}\n",
        encoding="utf-8",
    )

    after = _served_capabilities(skills)
    assert "probe_tool" in after["methods"]
    assert len(after["tools"]) == len(before["tools"]) + 1
    assert capability_checks.all_problems(after, skills) == []


def test_call_examples_satisfy_their_input_schemas():
    problems = capability_checks.call_example_problems(capability_checks.tool_catalog())
    assert problems == [], "a documented example is not callable:\n%s" % "\n".join(problems)


def test_read_only_tools_never_route_into_a_write_path():
    problems = capability_checks.read_only_path_problems(capability_checks.tool_catalog())
    assert problems == [], "a read-only claim breaks on its own chain:\n%s" % "\n".join(problems)


def test_declared_arguments_exist_on_the_implementation():
    """No tool may advertise a parameter the callable that runs never accepts."""
    catalog = capability_checks.tool_catalog()
    problems = capability_checks.declaration_problems(catalog)
    assert problems == [], "a declaration promises an argument that does not exist:\n%s" % (
        "\n".join(problems)
    )
    # Guard against the locks drifting apart: every tool must resolve to a
    # bridge method, so a renamed script cannot silently skip the check.
    assert len(catalog) == 33


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, "the injected drift no longer matches the catalog: %r" % old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


_DRIFT_CASES = [
    (
        "a primitive enum value disappears",
        "capability",
        lambda skills: _replace(
            skills / "freecad-modeling" / "tools.yaml",
            "enum: [box, cone, cylinder, sphere, torus, wedge, helix]",
            "enum: [box, cone, cylinder, sphere, wedge, helix]",
        ),
    ),
    (
        "get_capabilities advertises a parameter it does not have",
        "declaration",
        lambda skills: _replace(
            skills / "freecad-session" / "tools.yaml",
            "    source_file: scripts/get_capabilities.py\n"
            "    input_schema: {type: object, properties: {}, additionalProperties: false}",
            "    source_file: scripts/get_capabilities.py\n"
            "    input_schema: {type: object, properties: {file_path: {type: string}}, "
            "additionalProperties: false}",
        ),
    ),
    (
        "a call example drops a required argument",
        "examples",
        lambda skills: _replace(
            skills / "freecad-modeling" / "tools.yaml",
            "primitive: box, name: Body, dimensions:",
            "primitive: box, dimensions:",
        ),
    ),
    (
        "the advertised edge-ref limit stops matching the driver's limit",
        "capability",
        lambda skills: _replace(
            skills / "freecad-modify" / "tools.yaml",
            "maxItems: 200",
            "maxItems: 199",
        ),
    ),
    (
        "the advertised pattern limit stops matching the driver's limit",
        "capability",
        lambda skills: _replace(
            skills / "freecad-modify" / "tools.yaml",
            "maximum: 1000",
            "maximum: 500",
        ),
    ),
    (
        "a mirror plane disappears from the advertised set",
        "capability",
        lambda skills: _replace(
            skills / "freecad-modify" / "tools.yaml",
            "enum: [xy, xz, yz]",
            "enum: [xy, xz]",
        ),
    ),
    (
        "a read-only tool's failure chain routes into a writing tool",
        "read_only",
        lambda skills: _replace(
            skills / "freecad-session" / "tools.yaml",
            "next-tools: {on-failure: [inspect_document, get_status]}",
            "next-tools: {on-failure: [inspect_document, get_status, remove_object]}",
        ),
    ),
    (
        "next-tools names a tool the catalog has never heard of",
        "read_only",
        lambda skills: _replace(
            skills / "freecad-session" / "tools.yaml",
            "next-tools: {on-failure: [inspect_document, get_status]}",
            "next-tools: {on-failure: [inspect_document, get_status], "
            "on-success: [get_screenshot]}",
        ),
    ),
]


@pytest.mark.parametrize(
    "label, lock, mutate", _DRIFT_CASES, ids=[case[1] + ": " + case[0] for case in _DRIFT_CASES]
)
def test_each_capability_lock_goes_red_on_injected_drift(
    tmp_path: Path, label: str, lock: str, mutate
):
    """A lock that has never been seen failing is not a lock."""
    skills = capability_checks.copy_skills(tmp_path)
    # The payload a running server would still be serving: built from the
    # catalog before it drifted.
    served = _served_capabilities(skills)
    baseline = capability_checks.all_problems(served, skills)
    assert baseline == [], "the undrifted catalog already fails a lock: %r" % (baseline,)

    mutate(skills)

    catalog = capability_checks.tool_catalog(skills)
    reported = {
        "capability": capability_checks.capability_problems(served, skills),
        "read_only": capability_checks.read_only_path_problems(catalog),
        "examples": capability_checks.call_example_problems(catalog),
        "declaration": capability_checks.declaration_problems(catalog, skills),
    }
    assert reported[lock], "%s, but the %s lock stayed green" % (label, lock)
