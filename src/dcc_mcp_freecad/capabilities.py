"""Capability declarations derived from the shipped skill tool catalog.

``get_capabilities`` is the contract an agent reads before it calls anything
else in this adapter, so what it declares has to come from the same place MCP
tool registration comes from: ``skills/*/tools.yaml``. A second, hand-written
capability list is a list that drifts, and a drifted capability list is worse
than no list at all - the agent follows it, calls a tool with arguments the
schema never accepted, and gets back an error that does not explain why.

Three sources feed :func:`build_capabilities`, and nothing else:

* ``skills/*/tools.yaml`` - the file core loads when it registers the MCP
  tools, read here through core's own YAML codec so both sides parse it the
  same way.
* The path-enforcement suffix sets in :mod:`dcc_mcp_freecad.bridge` - the sets
  that actually accept or reject a path at call time. They are passed in
  rather than imported, so this module stays free of an import cycle and the
  suffix sets keep exactly one owner.
* The host status report, whose ``host_matrix`` block is produced by
  :mod:`dcc_mcp_freecad.compat` from ``compat_matrix.json`` - which host
  version breaks limit which tool on the host that is running right now.

:func:`capability_drift` is the other half of the lock. It re-reads the
catalog and reports every field where a served payload disagrees with it, in
both directions, so the lock can be executed in CI and again on real hardware.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from dcc_mcp_core import yaml_loads

from . import compat, sketch_rules

SKILLS_DIR = Path(__file__).parent / "skills"

# Declaration order also fixes the order capabilities are reported in:
# freecad-modeling declares ``depends: [freecad-session]``, so the session
# skill that owns the document lifecycle is listed first, freecad-modify
# depends on both, and freecad-parts is self-contained. ``load_tool_catalog``
# refuses to run when the directory disagrees with this tuple, so a new skill
# can never be silently dropped from the capability list.
SKILL_NAMES = (
    "freecad-session",
    "freecad-modeling",
    "freecad-modify",
    "freecad-parts",
    "freecad-sketch",
)

# The entry points an agent is already holding when it asks what this adapter
# can do. They are real tools with real schemas and are still declared under
# ``tools``; they are only kept out of ``methods``, which advertises what can
# be *done* to a document.
INTROSPECTION_TOOLS = ("get_status", "get_capabilities")

# Adapter-wide invariants rather than per-tool schema claims. Each names the
# code that enforces it, so changing that code has to bring this along.
ATOMIC_DOCUMENT_MUTATIONS = True  # FreecadBridge._mutate_document stages, then os.replace.
ARBITRARY_PYTHON = False  # freecad_driver._METHODS whitelist; the driver never eval/exec.
DOCUMENT_SNAPSHOTS = True  # FreecadBridge create/list/restore/delete_snapshot via SnapshotStore.
# The typed surface is the whole contract: an under-constrained sketch is refused
# rather than reported as usable, because there is no script escape hatch to
# repair a silently under-constrained profile afterwards. Enforced by
# sketch_rules.assert_feature_ready, which the profile-based feature tools call.
SKETCH_UNDERCONSTRAINED_REJECTED = True

# Where the driver's enforced limits are declared. Each entry names the tools
# that take the property and the schema keyword that carries the bound, so the
# advertised number and the number the driver rejects on cannot drift apart.
DERIVED_BOUNDS = (
    ("max_edge_refs", ("fillet_edges", "chamfer_edges"), "edge_refs", "maxItems"),
    ("max_pattern_instances", ("linear_pattern", "polar_pattern"), "count", "maximum"),
)
MIRROR_PLANES_TOOL = ("mirror_feature", "plane")

# Fields of a tool declaration that are compared field by field by
# :func:`capability_drift`. ``input_schema`` carries ``required``,
# ``properties``, ``enum``, ``dependencies`` and ``if``/``then`` with it.
COMPARED_TOOL_FIELDS = (
    "description",
    "input_schema",
    "output_schema",
    "annotations",
    "execution",
    "timeout_hint_secs",
)


# Sentinel for "the bound could not be read, so comparing it is meaningless".
# A bound of ``None`` is a real possibility only if the schema loses the
# keyword, which is already reported as its own drift line.
_MISSING = object()


class CapabilityError(RuntimeError):
    """A capability declaration could not be derived from the tool catalog."""


def load_tool_catalog(skills_dir: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Load every tool declaration from ``skills/*/tools.yaml``.

    ``skills_dir`` accepts a copy of the tree so tests can point the lock at a
    modified catalog without touching the shipped one.
    """
    root = Path(skills_dir) if skills_dir is not None else SKILLS_DIR
    if not root.is_dir():
        raise CapabilityError("skill directory does not exist: %s" % root)
    discovered = tuple(
        sorted(entry.name for entry in root.iterdir() if (entry / "tools.yaml").is_file())
    )
    if set(discovered) != set(SKILL_NAMES):
        raise CapabilityError(
            "the skill directory declares %s but the capability catalog knows %s; "
            "update SKILL_NAMES so the new skill is not dropped from get_capabilities"
            % (sorted(discovered), sorted(SKILL_NAMES))
        )
    catalog: List[Dict[str, Any]] = []
    for skill in SKILL_NAMES:
        path = root / skill / "tools.yaml"
        payload = yaml_loads(path.read_text(encoding="utf-8")) or {}
        tools = payload.get("tools")
        if not isinstance(tools, list) or not tools:
            raise CapabilityError("%s declares no tools" % path)
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                raise CapabilityError("%s contains a tool entry without a name" % path)
            record = dict(tool)
            record["skill"] = skill
            catalog.append(record)
    names = [record["name"] for record in catalog]
    if len(set(names)) != len(names):
        raise CapabilityError("duplicate tool names in the catalog: %s" % sorted(names))
    return catalog


def _tool_by_name(catalog: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any]:
    for tool in catalog:
        if tool.get("name") == name:
            return tool
    raise CapabilityError("tools.yaml no longer declares %r" % name)


def _enum_for(
    catalog: Sequence[Mapping[str, Any]], tool_name: str, property_name: str
) -> List[Any]:
    """Read a declared enum straight out of a tool's input schema."""
    schema = _tool_by_name(catalog, tool_name).get("input_schema") or {}
    declared = (schema.get("properties") or {}).get(property_name)
    if not isinstance(declared, dict) or "enum" not in declared:
        raise CapabilityError(
            "tools.yaml no longer declares an enum for %s.%s, so capabilities cannot "
            "be derived; declare it or update the capability derivation"
            % (tool_name, property_name)
        )
    return list(declared["enum"])


def _bound_for(
    catalog: Sequence[Mapping[str, Any]],
    tool_names: Sequence[str],
    property_name: str,
    keyword: str,
) -> Any:
    """Read a numeric bound out of every tool that declares the property.

    The bound is what the driver enforces at call time, so it is read from the
    schema the caller is held to rather than restated here. It is collected from
    *every* tool that takes the property, and they must agree: a limit that two
    tools advertise differently is a limit the caller cannot reason about, and
    silently taking the first would let one of them drift.
    """
    values = set()
    for tool_name in tool_names:
        schema = _tool_by_name(catalog, tool_name).get("input_schema") or {}
        declared = (schema.get("properties") or {}).get(property_name)
        if not isinstance(declared, dict) or keyword not in declared:
            raise CapabilityError(
                "tools.yaml no longer declares %s for %s.%s, so capabilities cannot "
                "be derived; declare it or update the capability derivation"
                % (keyword, tool_name, property_name)
            )
        values.add(declared[keyword])
    if len(values) != 1:
        raise CapabilityError(
            "%s disagree on %s.%s (%s), so capabilities cannot be derived"
            % (", ".join(tool_names), property_name, keyword, sorted(values))
        )
    return values.pop()


def host_limits(host_status: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Project the host's applicable matrix breaks onto the tools they limit.

    A break with ``adapter_usage: unused`` names no tool, because the adapter
    does not call the moved API; only a ``guarded`` break limits a tool. That
    mapping is declared once, in ``compat_matrix.json``, and read here rather
    than restated, so the capability list and the guard cannot disagree.
    """
    host_status = dict(host_status or {})
    matrix_status = (host_status.get("host_matrix") or {}).get("status")
    version = host_status.get("version")
    limited: Dict[str, List[str]] = {}
    changes: List[Dict[str, Any]] = []
    # Applied straight from the matrix for the reported version rather than
    # echoed from the driver's own report, so a host whose status payload omits
    # the matrix block still gets the limits its version implies.
    for entry in compat.breaking_changes_for(version or ""):
        tools = [name for name in (entry.get("affected_tools") or ()) if name]
        changes.append(
            {
                "id": entry.get("id"),
                "adapter_usage": entry.get("adapter_usage"),
                "enforcement": entry.get("enforcement"),
                "affected_tools": tools,
                "remediation": entry.get("remediation"),
            }
        )
        for name in tools:
            limited.setdefault(name, []).append(str(entry.get("id")))
    return {
        "host_version": version,
        "matrix_status": matrix_status,
        "breaking_changes": changes,
        "tools": {name: sorted(ids) for name, ids in sorted(limited.items())},
    }


def build_capabilities(
    host_status: Optional[Mapping[str, Any]] = None,
    import_extensions: Sequence[str] = (),
    export_extensions: Sequence[str] = (),
    parts: Optional[Mapping[str, Any]] = None,
    skills_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """Derive the whole ``get_capabilities`` payload from the tool catalog.

    ``parts`` carries the standard-parts declarations that describe the host
    process rather than a tool schema: which suffixes the library accepts and
    how the library is configured right now. It is passed in rather than
    imported so this module stays free of a dependency on the parts library and
    the bridge keeps ownership of the runtime half of the report.
    """
    catalog = load_tool_catalog(skills_dir)
    limits = host_limits(host_status)
    tools = []
    for tool in catalog:
        tools.append(
            {
                "name": tool["name"],
                "skill": tool["skill"],
                "description": tool.get("description"),
                "execution": tool.get("execution"),
                "timeout_hint_secs": tool.get("timeout_hint_secs"),
                "input_schema": tool.get("input_schema"),
                "output_schema": tool.get("output_schema"),
                "annotations": dict(tool.get("annotations") or {}),
                "next_tools": {
                    str(phase): list(targets)
                    for phase, targets in (tool.get("next-tools") or {}).items()
                },
                "call_examples": [dict(example) for example in (tool.get("call_examples") or ())],
                "host_limited": list(limits["tools"].get(tool["name"], ())),
            }
        )
    payload = {
        "status": dict(host_status or {}),
        "tools": tools,
        "methods": [tool["name"] for tool in tools if tool["name"] not in INTROSPECTION_TOOLS],
        "primitives": _enum_for(catalog, "add_primitive", "primitive"),
        "boolean_operations": _enum_for(catalog, "boolean_operation", "operation"),
        "import_extensions": sorted(import_extensions),
        "export_extensions": sorted(export_extensions),
        "atomic_document_mutations": ATOMIC_DOCUMENT_MUTATIONS,
        "arbitrary_python": ARBITRARY_PYTHON,
        "document_snapshots": DOCUMENT_SNAPSHOTS,
        "host_limited": limits,
    }
    payload["sketch_planes"] = _enum_for(catalog, "create_sketch", "plane")
    # The geometry and constraint vocabularies are rule-level: a type is what
    # sketch_rules accepts, and every declared type is reachable, so the two
    # lists are read from the module that enforces them rather than restated.
    payload["sketch_geometry_types"] = list(sketch_rules.GEOMETRY_TYPES)
    payload["sketch_constraint_types"] = list(sketch_rules.CONSTRAINT_TYPES)
    payload["sketch_underconstrained_rejected"] = SKETCH_UNDERCONSTRAINED_REJECTED
    tool_name, property_name = MIRROR_PLANES_TOOL
    payload["mirror_planes"] = _enum_for(catalog, tool_name, property_name)
    for key, tool_names, property_name, keyword in DERIVED_BOUNDS:
        payload[key] = _bound_for(catalog, tool_names, property_name, keyword)
    for key, value in (parts or {}).items():
        payload[key] = value
    return payload


def _served_tool_index(payload: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    index: Dict[str, Mapping[str, Any]] = {}
    for entry in payload.get("tools") or ():
        if isinstance(entry, dict) and entry.get("name"):
            index[str(entry["name"])] = entry
    return index


def capability_drift(
    payload: Mapping[str, Any],
    skills_dir: Optional[Any] = None,
    import_extensions: Sequence[str] = (),
    export_extensions: Sequence[str] = (),
) -> List[str]:
    """Return every field where a capability payload disagrees with tools.yaml.

    The comparison is made against the catalog on disk, never against a second
    call into :func:`build_capabilities`, so a bug in the derivation cannot
    agree with itself and hide the drift it was meant to catch.
    """
    catalog = load_tool_catalog(skills_dir)
    by_name = {str(tool["name"]): tool for tool in catalog}
    served = _served_tool_index(payload)
    drift: List[str] = []

    if not served:
        drift.append("tools: the payload declares no tools")

    for name in sorted(set(served) | set(by_name)):
        tool = by_name.get(name)
        entry = served.get(name)
        if tool is None:
            drift.append("%s: declared by capabilities but absent from tools.yaml" % name)
            continue
        if entry is None:
            drift.append("%s: declared by tools.yaml but absent from capabilities" % name)
            continue
        for field in COMPARED_TOOL_FIELDS:
            if entry.get(field) != tool.get(field):
                drift.append(
                    "%s.%s: capabilities %r != tools.yaml %r"
                    % (name, field, entry.get(field), tool.get(field))
                )
        expected_next = {
            str(phase): list(targets) for phase, targets in (tool.get("next-tools") or {}).items()
        }
        if entry.get("next_tools") != expected_next:
            drift.append(
                "%s.next_tools: capabilities %r != tools.yaml %r"
                % (name, entry.get("next_tools"), expected_next)
            )
        expected_examples = [dict(example) for example in (tool.get("call_examples") or ())]
        if entry.get("call_examples") != expected_examples:
            drift.append(
                "%s.call_examples: capabilities %r != tools.yaml %r"
                % (name, entry.get("call_examples"), expected_examples)
            )
        expected_limited = sorted(expected_host_limited(payload).get(name, ()))
        if sorted(entry.get("host_limited") or ()) != expected_limited:
            drift.append(
                "%s.host_limited: capabilities %r != compatibility matrix %r"
                % (name, sorted(entry.get("host_limited") or ()), expected_limited)
            )

    expected_methods = [name for name in by_name if name not in INTROSPECTION_TOOLS]
    if payload.get("methods") != expected_methods:
        drift.append(
            "methods: capabilities %r != tools.yaml %r" % (payload.get("methods"), expected_methods)
        )
    for key, tool_name, property_name in (
        ("primitives", "add_primitive", "primitive"),
        ("boolean_operations", "boolean_operation", "operation"),
        ("mirror_planes",) + tuple(MIRROR_PLANES_TOOL),
    ):
        if tool_name not in by_name:
            drift.append("%s: tools.yaml no longer declares %s" % (key, tool_name))
            continue
        schema = by_name[tool_name].get("input_schema") or {}
        expected = ((schema.get("properties") or {}).get(property_name) or {}).get("enum")
        if payload.get(key) != expected:
            drift.append(
                "%s: capabilities %r != tools.yaml %s.%s.enum %r"
                % (key, payload.get(key), tool_name, property_name, expected)
            )
    # The limits are what the driver refuses on, so a wrong advertised number
    # sends a caller into a rejection it was told it could not hit. Each is
    # compared against every tool that declares the property, and a tool that
    # has stopped declaring it at all is drift rather than a value to accept.
    for key, tool_names, property_name, keyword in DERIVED_BOUNDS:
        expected: Any = None
        for tool_name in tool_names:
            if tool_name not in by_name:
                drift.append("%s: tools.yaml no longer declares %s" % (key, tool_name))
                expected = _MISSING
                continue
            schema = by_name[tool_name].get("input_schema") or {}
            declared = ((schema.get("properties") or {}).get(property_name) or {}).get(keyword)
            if declared is None:
                drift.append(
                    "%s: tools.yaml no longer declares %s for %s.%s"
                    % (key, keyword, tool_name, property_name)
                )
                expected = _MISSING
                continue
            if expected is None:
                expected = declared
            elif declared != expected:
                drift.append(
                    "%s: %s declares %r but %s declares %r"
                    % (key, tool_name, declared, tool_names[0], expected)
                )
        if expected is not _MISSING and payload.get(key) != expected:
            drift.append(
                "%s: capabilities %r != tools.yaml %s %r"
                % (key, payload.get(key), keyword, expected)
            )
    for key, declared in (
        ("import_extensions", import_extensions),
        ("export_extensions", export_extensions),
    ):
        if payload.get(key) != sorted(declared):
            drift.append(
                "%s: capabilities %r != path enforcement %r"
                % (key, payload.get(key), sorted(declared))
            )

    status = payload.get("status") or {}
    expected_limits = {
        "host_version": status.get("version"),
        "matrix_status": (status.get("host_matrix") or {}).get("status"),
        "breaking_changes": [
            {
                "id": entry.get("id"),
                "adapter_usage": entry.get("adapter_usage"),
                "enforcement": entry.get("enforcement"),
                "affected_tools": list(entry.get("affected_tools") or ()),
                "remediation": entry.get("remediation"),
            }
            for entry in compat.breaking_changes_for(status.get("version") or "")
        ],
        "tools": expected_host_limited(payload),
    }
    if payload.get("host_limited") != expected_limits:
        drift.append(
            "host_limited: capabilities %r != compatibility matrix %r"
            % (payload.get("host_limited"), expected_limits)
        )
    return drift


def expected_host_limited(payload: Mapping[str, Any]) -> Dict[str, List[str]]:
    """Return the host-limited tool map the compatibility matrix implies.

    Recomputed straight from :mod:`dcc_mcp_freecad.compat` so the check does
    not reuse :func:`host_limits`, which is the code under test.
    """
    status = payload.get("status") or {}
    version = status.get("version")
    limited: Dict[str, List[str]] = {}
    for entry in compat.breaking_changes_for(version or ""):
        for name in entry.get("affected_tools") or ():
            limited.setdefault(str(name), []).append(str(entry.get("id")))
    return {name: sorted(ids) for name, ids in limited.items()}
